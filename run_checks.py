"""run_checks.py — offline checks for the wiring that only breaks at runtime.

Every check here exists because something was actually broken, and because the
break was invisible until a collection had already been running for a while.
No network: these are all import-and-inspect, and run in well under a second.

    python run_checks.py
"""

from __future__ import annotations

import inspect
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
          n <= collect.MAX_CONSECUTIVE_FAILURES,
          f"kept going for {n} failed fetches "
          f"(limit is {collect.MAX_CONSECUTIVE_FAILURES})")


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
    print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
