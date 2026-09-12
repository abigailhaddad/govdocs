"""run_checks.py — offline checks for the wiring that only breaks at runtime.

Every check here exists because something was actually broken, and because the
break was invisible until a collection had already been running for a while.
No network: these are all import-and-inspect, and run in well under a second.

    python run_checks.py
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re

from govdocs import collect, publish

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail and not ok else ''}")
    if not ok:
        FAILURES.append(name)


def check_cli_args_exist() -> None:
    """Every attribute main() reads off the parsed args must be defined.

    `--r2` was referenced as `a.r2` and never added to the parser, so every
    --source run died with AttributeError before fetching a single document.
    Publishing returned earlier and was unaffected, which is why it went unseen.

    The real parser is inspected, not a copy: a copy would drift and quietly
    stop catching this.
    """
    ns = collect._parser().parse_args([])
    read = set(re.findall(r"\ba\.([A-Za-z_][A-Za-z0-9_]*)",
                          inspect.getsource(collect.main)))
    missing = sorted(a for a in read if not hasattr(ns, a))
    check("main() reads only args the parser defines", not missing,
          f"undefined: {missing}")


def check_sources_shape() -> None:
    bad = []
    for name, cls in collect.SOURCES.items():
        for attr in ("name", "collection", "discover", "fetch"):
            if not hasattr(cls, attr):
                bad.append(f"{name}.{attr}")
    check("every source has name/collection/discover/fetch", not bad, str(bad))


def check_source_collections_have_datasets() -> None:
    """A source whose collection has no dataset fails only at the first flush.

    By then it has already fetched up to BATCH documents, so the failure lands
    after the slow part rather than before it.
    """
    missing = sorted({cls.collection for cls in collect.SOURCES.values()}
                     - set(publish.DATASETS))
    check("every source collection maps to a dataset", not missing,
          f"no dataset for: {missing}")


def check_datasets_have_cards() -> None:
    """ensure_dataset() indexes BLURBS and PROVENANCE by collection."""
    missing = [f"{c}:{which}"
               for c in publish.DATASETS
               for which, d in (("blurb", publish.BLURBS), ("provenance", publish.PROVENANCE))
               if c not in d]
    check("every dataset has a blurb and a provenance line", not missing, str(missing))


def check_limit_counts_collected() -> None:
    """`limit` must cap documents collected, not records discovered.

    Capping discovery meant a second run over a fixed corpus walked the same
    first N records, found them all in seen.jsonl and collected nothing, so a
    nightly job stopped making progress after its first run. The seen-check has
    to come first and the limit second.
    """
    src = inspect.getsource(collect.collect)
    unbounded = "src.discover(since=since, limit=None)" in src
    check("discovery is unbounded; limit is applied after the seen-check",
          unbounded, "collect() still passes limit into discover()")

    # The break must come after the `key in keys` skip, or it caps discovery again.
    body = src.split("for rec in src.discover", 1)[-1]
    seen_at = body.find("if key in keys")
    break_at = body.find("got >= limit")
    check("the limit break follows the already-seen skip",
          seen_at != -1 and break_at != -1 and break_at > seen_at,
          f"seen-check at {seen_at}, limit break at {break_at}")


def check_transient_errors_retry() -> None:
    """A 502 must not permanently blacklist a document.

    Failed attempts used to count as seen, so govinfo's content-tier outage
    wrote 1,634 packages to seen.jsonl as errors and no later run would have
    retried any of them -- a silent hole in the corpus.
    """
    import json, tempfile, os
    from pathlib import Path as _P
    sample = [
        {"key": "s/collected_0", "doc_id": "collected_0", "sha256": "a"},
        {"key": "s/dupe_0", "sha256": "a", "duplicate": True},
        {"key": "s/gone_0", "error": "fetch: 404 Client Error: Not Found"},
        {"key": "s/toobig_0", "error": "too large: 512000000 bytes"},
        {"key": "s/empty_0", "error": "empty response"},
        {"key": "s/blip_0", "error": "fetch: 502 Server Error: Bad Gateway"},
        {"key": "s/slow_0", "error": "fetch: HTTPSConnectionPool read timeout"},
    ]
    fd, path = tempfile.mkstemp(suffix=".jsonl"); os.close(fd)
    _P(path).write_text("\n".join(json.dumps(r) for r in sample))
    orig = collect.SEEN
    try:
        collect.SEEN = _P(path)
        keys, _ = collect._seen()
    finally:
        collect.SEEN = orig
        os.unlink(path)
    check("collected and permanently-gone documents count as seen",
          {"s/collected_0", "s/dupe_0", "s/gone_0"} <= keys,
          f"missing from seen: {{'s/collected_0','s/dupe_0','s/gone_0'}} - {keys}")
    # Oversized and empty are recorded but not settled: raising MAX_BYTES, or
    # the host having a better minute, should bring them back rather than
    # leaving them invisible. Only "gone" closes the door.
    retried = {"s/blip_0", "s/slow_0", "s/toobig_0", "s/empty_0"} & keys
    check("5xx, timeouts, empty bodies and oversized files stay retryable", not retried,
          f"wrongly marked seen: {sorted(retried)}")


def check_circuit_breaker() -> None:
    """A run must give up on a host that is failing every request.

    govinfo's content tier returned 502 to everything for over an hour. Each
    document was handled correctly on its own -- record the error, continue --
    and the run therefore walked 1,634 packages against a dead server.
    """
    import tempfile
    from pathlib import Path as _P

    attempts = {"n": 0}

    class AlwaysFails:
        name = collection = "probe"

        def __init__(self, max_calls=200):
            self.calls = 0

        def discover(self, since, until=None, limit=None):
            for i in range(10_000):
                yield {"source": "probe", "notice_id": f"d{i}", "index": 0,
                       "url": f"https://example.invalid/{i}", "landing_url": "",
                       "title": "", "date": "", "agency": "", "office": "",
                       "notice_type": ""}

        def fetch(self, rec):
            attempts["n"] += 1
            raise RuntimeError("502 Server Error: Bad Gateway")

    tmp = _P(tempfile.mkdtemp())
    orig = (collect.SOURCES, collect.SEEN, collect.STAGE, collect._flush)
    try:
        collect.SOURCES = {"probe": AlwaysFails}
        collect.SEEN = tmp / "seen.jsonl"
        collect.STAGE = tmp / "stage"
        collect._flush = lambda *a, **k: None
        collect.collect("probe", since="2020-01-01", limit=500, max_calls=10)
    finally:
        (collect.SOURCES, collect.SEEN, collect.STAGE, collect._flush) = orig

    n = attempts["n"]
    check("a run stops once every fetch is failing",
          n <= collect.HOST_FAILURE_LIMIT,
          f"kept going for {n} failed fetches "
          f"(limit is {collect.HOST_FAILURE_LIMIT})")


def check_room_overrides() -> None:
    """The corrections to api.foia.gov's directory must still apply to it.

    An override whose key is no longer in the directory is dead weight, and
    silently so: it neither fires nor complains.
    """
    import json
    from pathlib import Path as _P
    from govdocs.sources.room_overrides import OVERRIDES, KNOWN_DEAD

    selfmap = [k for k, v in OVERRIDES.items() if k == v]
    check("no override points a URL at itself", not selfmap, str(selfmap[:3]))

    hosts = {k.split("/")[2] for k in OVERRIDES if "//" in k}
    both = sorted(hosts & set(KNOWN_DEAD))
    check("no host is both overridden and known-dead", not both, str(both))

    d = _P("data/reading_rooms.json")
    if not d.exists():
        print("  SKIP  overrides match the directory (no local copy)")
        return
    listed = {r["url"] for r in json.loads(d.read_text())}
    stale = sorted(set(OVERRIDES) - listed)
    check("every override still matches a listed room", not stale,
          f"{len(stale)} no longer in the directory: {stale[:2]}")


def check_sharded_paths() -> None:
    """Documents must be foldered, and the manifest must follow them.

    Hugging Face rejects a commit leaving over 10,000 files in one directory.
    documents/govinfo/ reached 9,997 and every push after that failed with a
    400 no retry could clear, while collect() went on recording documents as
    collected -- 500 of them existed nowhere but the staging directory.
    """
    a = collect._doc_path("govinfo", "ab12cd", "DOC-1_0", ".pdf")
    check("document paths are sharded by checksum",
          a == "documents/govinfo/ab/DOC-1_0.pdf", f"got {a!r}")

    src = inspect.getsource(collect.build_metadata)
    check("the manifest uses the recorded path, not a recomputed one",
          'r.get("path")' in src,
          "build_metadata recomputes the path and will mislabel older flat files")

    src2 = inspect.getsource(collect._flush)
    idx_up = src2.find("upload_folder")
    idx_rm = src2.find("rmtree")
    check("staging is cleared only after the upload returns",
          idx_up != -1 and idx_rm != -1 and idx_rm > idx_up,
          "a failed push would delete documents already recorded as collected")


def check_one_bad_host_does_not_end_a_run() -> None:
    """A walled host must not end a run that spans hundreds of them.

    foia_rooms visits 223 hosts and 59 refuse a plain request. Counting
    failures globally, 25 consecutive 403s from abmc.gov alone ended a run with
    400 rooms still unvisited.
    """
    import tempfile
    from pathlib import Path as _P
    tried = {"bad": 0, "good": 0}

    class OneBadHost:
        name = collection = "probe"

        def __init__(self, max_calls=200):
            self.calls = 0

        def discover(self, since, until=None, limit=None):
            for i in range(400):        # a long run of one walled host...
                yield {"source": "probe", "notice_id": f"b{i}", "index": 0,
                       "url": f"https://walled.invalid/{i}", "landing_url": "",
                       "title": "", "date": "", "agency": "", "office": "",
                       "notice_type": ""}
            for i in range(5):          # ...and a healthy one after it
                yield {"source": "probe", "notice_id": f"g{i}", "index": 0,
                       "url": f"https://fine.invalid/{i}", "landing_url": "",
                       "title": "", "date": "", "agency": "", "office": "",
                       "notice_type": ""}

        def fetch(self, rec):
            if "walled" in rec["url"]:
                tried["bad"] += 1
                raise RuntimeError("403 Client Error: Forbidden")
            tried["good"] += 1
            return b"%PDF-1.4 ok", "x.pdf"

    tmp = _P(tempfile.mkdtemp())
    orig = (collect.SOURCES, collect.SEEN, collect.STAGE, collect._flush)
    try:
        collect.SOURCES = {"probe": OneBadHost}
        collect.SEEN = tmp / "seen.jsonl"
        collect.STAGE = tmp / "stage"
        collect._flush = lambda *a, **k: None
        collect.collect("probe", since="2020-01-01", limit=50, max_calls=10)
    finally:
        (collect.SOURCES, collect.SEEN, collect.STAGE, collect._flush) = orig

    check("a walled host is dropped, not the whole run",
          tried["bad"] <= collect.HOST_FAILURE_LIMIT,
          f"kept hitting the bad host {tried['bad']} times")
    check("the run continues to healthy hosts afterwards",
          tried["good"] == 5,
          f"only reached the good host {tried['good']} times")


def check_one_collector_per_collection() -> None:
    """Two sources sharing a collection must not be run at the same time.

    documentcloud and foia_rooms both write to `foia`, so they stage into the
    same directory and _flush would push and delete under the other. The same
    shape stranded 500 documents when a second govinfo round started behind the
    first: recorded as collected, present only in staging.

    Nothing enforces this at runtime, so it is at least written down here.
    """
    shared = {}
    for name, cls in collect.SOURCES.items():
        shared.setdefault(cls.collection, []).append(name)
    overlaps = {c: sorted(v) for c, v in shared.items() if len(v) > 1}
    # Not a failure -- it is the design -- but the pairs must be known.
    print(f"  NOTE  sources sharing a collection (never run concurrently): {overlaps}")
    check("every source declares a collection", all(shared.values()), "")


def check_ids_are_stable() -> None:
    """A document's id must not change between runs.

    foia_rooms built notice_id from Python's hash(), which is seeded per
    process. The same URL got a different id every run, so no already-seen key
    ever matched, every document was downloaded again, and the content checksum
    recognised it only after the bytes had crossed the wire: 15,189 needless
    fetches against agency servers, and a three-and-a-half hour run that
    collected nothing.

    Run in a fresh interpreter, because that is the only place the bug lives.
    """
    import subprocess, sys as _sys
    code = (
        "import sys; sys.path.insert(0, '.');"
        "from govdocs.sources.foia_rooms import FoiaRooms;"
        "import inspect, re;"
        "src = inspect.getsource(FoiaRooms);"
        "print('HASH' if re.search(r'notice_id.*\\bhash\\(', src) else 'ok')"
    )
    r = subprocess.run([_sys.executable, "-c", code], capture_output=True, text=True)
    check("ids are not built from Python's per-process hash()",
          "HASH" not in r.stdout, "foia_rooms notice_id uses hash()")

    # And prove the id itself is reproducible in a separate interpreter.
    idcode = ("import hashlib;"
              "print(hashlib.sha1(b'https://example.gov/a.pdf').hexdigest()[:16])")
    a = subprocess.run([_sys.executable, "-c", idcode], capture_output=True, text=True).stdout
    b = subprocess.run([_sys.executable, "-c", idcode], capture_output=True, text=True).stdout
    check("the same url yields the same id in a new process", a == b and a.strip() != "",
          f"{a.strip()!r} vs {b.strip()!r}")


def check_manifest_is_a_union() -> None:
    """The manifest must never lose rows it did not write.

    This machine is not the only writer: a scheduled Action collects the same
    sources against its own cached seen.jsonl. Each rebuilt the manifest from
    its own partial history and overwrote the other's, which left the foia
    dataset holding 9,797 files and a manifest naming 1,504 -- 8,300 documents
    present in the repo and missing from the index describing it.
    """
    import tempfile, json
    from pathlib import Path as _P
    import pyarrow.parquet as pq

    tmp = _P(tempfile.mkdtemp())
    seen = tmp / "seen.jsonl"
    seen.write_text(json.dumps({
        "key": "sam/mine_0", "doc_id": "mine_0", "collection": "sam",
        "source": "sam", "sha256": "a", "bytes": 10, "pages": 1,
        "path": "documents/sam/aa/mine_0.pdf"}) + "\n")

    orig_seen, orig_pub = collect.SEEN, collect._published_manifest
    try:
        collect.SEEN = seen
        # Pretend the dataset already lists a document this machine never saw.
        collect._published_manifest = lambda c: [
            {"doc_id": "theirs_0", "source": "sam", "title": "from the runner",
             "path": "documents/sam/bb/theirs_0.pdf"}]
        out = collect.build_metadata("sam", tmp)
        ids = set(pq.read_table(out).to_pydict()["doc_id"])
        check("a rebuild keeps rows written by the other collector",
              {"mine_0", "theirs_0"} <= ids, f"got {sorted(ids)}")

        # And an unreadable published manifest must not become an empty one.
        collect._published_manifest = lambda c: None
        skipped = collect.build_metadata("sam", tmp)
        check("an unreadable manifest is left alone, not replaced",
              skipped is None, "build_metadata wrote a manifest anyway")
    finally:
        collect.SEEN, collect._published_manifest = orig_seen, orig_pub


def check_rooms_are_ordered_by_need() -> None:
    """Rooms we have taken least from must be visited first.

    In directory order a bounded run walked the same front stretch every time:
    ICE's 4,393 documents re-checked on every pass while 21 hosts holding a
    thousand documents between them -- OGE's 669 among them -- were never
    reached once. Failures break the tie, so a host that has never been tried
    sorts ahead of one that refuses everything.
    """
    import json, tempfile
    from pathlib import Path as _P
    from govdocs.sources import foia_rooms as fr

    tmp = _P(tempfile.mkdtemp()) / "seen.jsonl"
    tmp.write_text("\n".join(json.dumps(r) for r in [
        {"source": "foia_rooms", "doc_id": "a", "url": "https://rich.gov/1.pdf"},
        {"source": "foia_rooms", "doc_id": "b", "url": "https://rich.gov/2.pdf"},
        {"source": "foia_rooms", "doc_id": "c", "url": "https://some.gov/1.pdf"},
        {"key": "foia_rooms/x_0", "error": "fetch: 403", "url": "https://walled.gov/1.pdf"},
        {"key": "foia_rooms/y_0", "error": "fetch: 403", "url": "https://walled.gov/2.pdf"},
    ]))
    rooms = [{"url": f"https://{h}/foia"} for h in
             ("rich.gov", "walled.gov", "fresh.gov", "some.gov")]
    orig = fr.SEEN_LOG
    try:
        fr.SEEN_LOG = tmp
        order = [r["url"].split("/")[2]
                 for r in fr.FoiaRooms(max_calls=1)._least_harvested_first(rooms)]
    finally:
        fr.SEEN_LOG = orig

    check("an untried room comes before one that refuses everything",
          order.index("fresh.gov") < order.index("walled.gov"), str(order))
    check("the most-harvested room goes last",
          order[-1] == "rich.gov", str(order))


def check_many_walled_hosts_do_not_end_a_run() -> None:
    """Failures spread thinly across many hosts must not end a run either.

    A global counter alongside the per-host one looked like a harmless
    backstop. It was not: eight walled DOT hosts contributing a few 403s each,
    none reaching the per-host limit alone, summed past the global one and
    killed a run 329 requests in, before it reached any of the rooms it had
    been reordered to visit.
    """
    import tempfile
    from pathlib import Path as _P
    tried = {"bad": 0, "good": 0}

    class ManyWalledHosts:
        name = collection = "probe"

        def __init__(self, max_calls=200):
            self.calls = 0

        def discover(self, since, until=None, limit=None):
            # Twelve hosts, five failures each -- 60 failures, none hitting the
            # per-host limit of 10 -- then a host that works.
            for h in range(12):
                for i in range(5):
                    yield {"source": "probe", "notice_id": f"w{h}_{i}", "index": 0,
                           "url": f"https://walled{h}.invalid/{i}", "landing_url": "",
                           "title": "", "date": "", "agency": "", "office": "",
                           "notice_type": ""}
            for i in range(4):
                yield {"source": "probe", "notice_id": f"g{i}", "index": 0,
                       "url": f"https://fine.invalid/{i}", "landing_url": "",
                       "title": "", "date": "", "agency": "", "office": "",
                       "notice_type": ""}

        def fetch(self, rec):
            if "walled" in rec["url"]:
                tried["bad"] += 1
                raise RuntimeError("403 Client Error: Forbidden")
            tried["good"] += 1
            return b"%PDF-1.4 ok", "x.pdf"

    tmp = _P(tempfile.mkdtemp())
    orig = (collect.SOURCES, collect.SEEN, collect.STAGE, collect._flush)
    try:
        collect.SOURCES = {"probe": ManyWalledHosts}
        collect.SEEN = tmp / "seen.jsonl"
        collect.STAGE = tmp / "stage"
        collect._flush = lambda *a, **k: None
        collect.collect("probe", since="2020-01-01", limit=50, max_calls=10)
    finally:
        (collect.SOURCES, collect.SEEN, collect.STAGE, collect._flush) = orig

    check("failures spread across many hosts do not end the run",
          tried["good"] == 4,
          f"only reached the working host {tried['good']}/4 times after "
          f"{tried['bad']} failures across 12 hosts")


def check_index_only_is_enforced() -> None:
    """An index-only source must refuse to collect, not merely be documented.

    govinfo was made an index on 2026-09-07 -- README, dataset card and
    publish.py all say the files are not mirrored, and 170GB of PDFs were
    deleted from the dataset. Nothing said so in collect.py, so a cron running
    `--source govinfo` re-uploaded 300 of them over the following two days and
    no step in the pipeline objected. The gap was between what the project said
    and what it enforced.
    """
    check("INDEX_ONLY names govinfo",
          "govinfo" in collect.INDEX_ONLY,
          f"INDEX_ONLY = {collect.INDEX_ONLY!r}")

    # Every index-only source must be refused by collect() itself, not by the
    # CLI -- resume.sh called the module, and a check on the parser would have
    # passed while the cron kept running.
    for name in collect.INDEX_ONLY:
        try:
            collect.collect(name, since="1900-01-01", limit=1, max_calls=1)
        except SystemExit as exc:
            refused = "index" in str(exc).lower()
        except Exception as exc:  # network, credentials, anything else
            refused = False
        else:
            refused = False
        check(f"collect({name!r}) refuses to mirror", refused)

    # ...and the override has to exist, or the refusal is a dead end for the
    # case publish.py explicitly anticipates ("if something needs the bytes").
    sig = inspect.signature(collect.collect)
    check("collect() has a mirror_anyway override",
          "mirror_anyway" in sig.parameters)

    src = inspect.getsource(collect._parser)
    check("--mirror-anyway is wired to the parser",
          "--mirror-anyway" in src)

    # publish.py must not carry an index-only source into a file upload path.
    for name in collect.INDEX_ONLY:
        collection = collect.SOURCES[name].collection
        check(f"{name} is still published as an index ({collection})",
              collection in publish.DATASETS)


def check_batch_stays_within_the_commit_budget() -> None:
    """Documents are pushed in big batches because the API limits commits.

    Hugging Face allows 128 commits an hour, and the limit is on requests, not
    bytes -- one commit per document exhausts it in minutes, and huggingface_hub
    turns a rejection into "retrying in smaller chunks", which spends the budget
    faster still. Measured on a real run: 500 documents take about 110 seconds,
    so BATCH=500 is roughly 32 commits an hour and BATCH=100 would be 161 --
    over the limit. 250 is the floor.

    This exists because recording documents only after their push (see
    check_records_land_after_the_push) invites the idea of flushing more often
    to lose less. It would trade a small loss for a hard API failure.
    """
    check("BATCH is large enough to stay under 128 commits/hour",
          collect.BATCH >= 250, f"BATCH = {collect.BATCH}")


def check_records_land_after_the_push() -> None:
    """A document is recorded as collected only once its file has been pushed.

    _seen() treats a recorded doc_id as settled, so a row written before the
    upload is a promise the run may not keep: the GitHub job hit its 5h30m
    timeout on two consecutive days, and anything staged but unpushed was
    recorded as collected, discarded with the runner, and never retried.

    Ordering inside _flush must be upload -> record -> delete staging.
    """
    src = inspect.getsource(collect._flush)
    i_upload = src.find("upload_folder")
    i_record = src.find("_record(row)")
    i_rmtree = src.find("rmtree")
    check("_flush uploads before it records", 0 <= i_upload < i_record,
          f"upload at {i_upload}, record at {i_record}")
    check("_flush records before it clears staging", 0 <= i_record < i_rmtree,
          f"record at {i_record}, rmtree at {i_rmtree}")

    # The collect loop must hand rows to _flush rather than writing them itself.
    loop = inspect.getsource(collect.collect)
    check("the collect loop defers collected rows to _flush",
          "pending.append(" in loop and 'pending)' in loop)
    check("no collected row is written straight to seen.jsonl",
          '_record({"key": key, "sha256": digest, "r2_key"' not in loop)


def check_a_death_mid_push_leaves_no_hole() -> None:
    """If the push dies, nothing may be recorded as collected.

    This is the failure the GitHub job produced twice: the run is killed part
    way through a batch, the documents were already written to seen.jsonl, and
    _seen() treats them as settled -- so the dataset never gets the files and no
    later run ever asks for them again. Losing the work is fine; losing the
    knowledge that the work is still owed is not.

    Two runs here, over a real _flush with only the upload stubbed: one where
    the push succeeds and one where it raises.
    """
    import tempfile
    from pathlib import Path as _P

    class Tiny:
        name = collection = "probe"

        def __init__(self, max_calls=200):
            self.calls = 0

        def discover(self, since, until=None, limit=None):
            for i in range(6):
                yield {"source": "probe", "notice_id": f"d{i}", "index": 0,
                       "url": f"https://fine.invalid/{i}", "landing_url": "",
                       "title": "", "date": "", "agency": "", "office": "",
                       "notice_type": ""}

        def fetch(self, rec):
            # Distinct bytes per record, or the sha256 check calls them dupes.
            return b"%PDF-1.4 " + rec["notice_id"].encode(), "x.pdf"

    def run(upload_works: bool) -> tuple[int, int]:
        tmp = _P(tempfile.mkdtemp())
        orig = (collect.SOURCES, collect.SEEN, collect.STAGE,
                collect.build_metadata, publish.ensure_dataset,
                publish.upload_folder)
        try:
            collect.SOURCES = {"probe": Tiny}
            collect.SEEN = tmp / "seen.jsonl"
            collect.STAGE = tmp / "stage"
            collect.build_metadata = lambda *a, **k: None
            publish.ensure_dataset = lambda *a, **k: None

            def upload(*a, **k):
                if not upload_works:
                    raise RuntimeError("connection reset mid-push")
            publish.upload_folder = upload

            try:
                collect.collect("probe", since="2020-01-01", limit=6, max_calls=5)
            except RuntimeError:
                pass        # the killed-run case

            rows = 0
            if collect.SEEN.exists():
                rows = sum(1 for line in collect.SEEN.read_text().splitlines()
                           if line.strip() and json.loads(line).get("doc_id"))
            staged = sum(1 for f in (tmp / "stage").rglob("*") if f.is_file())
            return rows, staged
        finally:
            (collect.SOURCES, collect.SEEN, collect.STAGE,
             collect.build_metadata, publish.ensure_dataset,
             publish.upload_folder) = orig

    ok_rows, ok_staged = run(upload_works=True)
    check("a successful push records what it pushed", ok_rows == 6,
          f"recorded {ok_rows} of 6")
    check("a successful push clears staging", ok_staged == 0,
          f"{ok_staged} files left staged")

    dead_rows, dead_staged = run(upload_works=False)
    check("a failed push records nothing as collected", dead_rows == 0,
          f"recorded {dead_rows} documents that were never pushed")
    check("a failed push keeps staging for --flush to retry", dead_staged > 0,
          "staging was cleared, so the batch cannot be retried")


def check_retired_sources_are_enforced() -> None:
    """Only sam and foia_rooms are collected; the rest must refuse.

    Same shape as check_index_only_is_enforced, and for the same reason: a
    scope decision that lives only in prose gets undone by the next cron.
    """
    check("RETIRED covers the exploratory sources",
          {"oversight", "documentcloud"} <= collect.RETIRED,
          f"RETIRED = {collect.RETIRED!r}")
    check("sam and foia_rooms are NOT retired",
          not ({"sam", "foia_rooms"} & (collect.RETIRED | collect.INDEX_ONLY)))

    for name in collect.RETIRED:
        try:
            collect.collect(name, since="1900-01-01", limit=1, max_calls=1)
        except SystemExit as exc:
            refused = "retired" in str(exc).lower()
        except Exception:
            refused = False
        else:
            refused = False
        check(f"collect({name!r}) refuses: retired", refused)

    # The workflow must not still be invoking a retired or relocated source.
    wf = pathlib.Path(".github/workflows/collect.yml")
    if wf.exists():
        body = wf.read_text()
        for name in sorted(collect.RETIRED):
            check(f"the workflow does not collect {name}",
                  f"--source {name}" not in body)
        # The reading rooms run on the box now; two collectors writing one
        # collection is what mis-built the manifest before.
        check("the workflow leaves foia_rooms to the box",
              "--source foia_rooms" not in body)


def main() -> int:
    print("govdocs checks")
    check_sources_shape()
    check_source_collections_have_datasets()
    check_datasets_have_cards()
    check_cli_args_exist()
    check_limit_counts_collected()
    check_transient_errors_retry()
    check_circuit_breaker()
    check_room_overrides()
    check_sharded_paths()
    check_one_bad_host_does_not_end_a_run()
    check_many_walled_hosts_do_not_end_a_run()
    check_one_collector_per_collection()
    check_ids_are_stable()
    check_manifest_is_a_union()
    check_rooms_are_ordered_by_need()
    check_index_only_is_enforced()
    check_retired_sources_are_enforced()
    check_batch_stays_within_the_commit_budget()
    check_records_land_after_the_push()
    check_a_death_mid_push_leaves_no_hole()
    print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
