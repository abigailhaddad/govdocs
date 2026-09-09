"""collect.py — fetch documents, keep them in R2, stage them for publication.

  python -m govdocs.collect --source sam --since 2025-01-20 --limit 500
  python -m govdocs.collect --publish sam

Collection and publication are separate steps on purpose. Fetching is slow and
rate-limited and wants to run often in small bites; pushing to Hugging Face is a
single large commit and wants to run rarely.

Hugging Face is the store. Documents are staged in batches and pushed as a
single commit each, then deleted locally, so peak disk is one batch rather than
the whole archive -- which matters when the archive is heading for a hundred
gigabytes and the laptop is not.

One commit per batch, never per file: a commit carrying five hundred documents
costs the API about what a commit carrying one does, and the limits are on
requests rather than bytes.

R2 is optional and off by default. It was useful while Hugging Face was not yet
the destination; keeping both means paying to store the same bytes twice.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import time
import urllib.parse
from pathlib import Path

from . import publish, store
from .agencies import canonical
from .sources.documentcloud import DocumentCloud
from .sources.foia_rooms import FoiaRooms
from .sources.governmentattic import GovernmentAttic
from .sources.govinfo import GovInfo
from .sources.oversight import Oversight
from .sources.sam import Sam

SOURCES = {"documentcloud": DocumentCloud, "foia_rooms": FoiaRooms,
           "governmentattic": GovernmentAttic, "govinfo": GovInfo,
           "oversight": Oversight, "sam": Sam}

# Sources whose documents are deliberately NOT mirrored. govinfo is the clear
# case: GPO is a statutory permanent-access institution and govinfo IS the
# system of record, so a copy protects nothing and cost 170GB. The files were
# deleted from the dataset and the manifest kept, each row naming the URL its
# PDF is still served from.
#
# The decision was made on 2026-09-07 and written into the README, the dataset
# card and publish.py -- but not here, so a cron running `--source govinfo`
# quietly re-uploaded PDFs into the emptied dataset for two days and nothing
# complained. Documentation is not enforcement. This is the enforcement.
#
# Passing --mirror-anyway overrides it, for the case publish.py anticipates:
# something genuinely needs the bytes in bulk.
INDEX_ONLY = {"govinfo"}

SEEN = Path("data/seen.jsonl")
PUBLISHED = Path("data/published.jsonl")
STAGE = Path("data/stage")
# Two gigabytes. The old 200 MB cap silently dropped real documents -- a 339 MB
# National Park Service FOIA release among them -- and nothing in the dataset
# showed they were missing.
MAX_BYTES = 2 * 1024 * 1024 * 1024

# Give up on a host after this many failures in a row, and carry on with the
# others. There is deliberately no global counter beside it.
#
# There was one, and it was wrong twice over. foia_rooms visits 223 hosts and 59
# refuse a plain request, so a run that opened on walled agencies collected the
# DOT family's 403s -- FAA, FHWA, FRA, FMCSA, NHTSA, MARAD, PHMSA, Seaway, a
# few each, none reaching this limit alone -- until twenty-five had accumulated
# across them and the run aborted at Seaway, before reaching a single one of the
# rooms it was reordered to visit. 747 seconds, 329 requests, nothing collected.
#
# And it was never needed. A source with one host -- govinfo during its outage --
# reaches this limit on that host, is skipped from then on, and the run ends on
# its own having spent ten requests. The per-host rule already does the job the
# global one was added for.
HOST_FAILURE_LIMIT = 10

# How many documents to hold on disk at once while publishing. 300 averages a
# few hundred megabytes and keeps each Hugging Face commit a sane size.
BATCH = 500          # documents per commit

# Hugging Face rejects any commit that would leave more than 10,000 files in a
# single directory, and documents/<source>/ hit that at 9,997 with a 400 that
# no retry can clear. Files are foldered by the first byte of their checksum:
# 256 shards, evenly filled because a hash is evenly distributed, good for 2.5M
# documents per source. The shard is recorded per document rather than
# recomputed, so the manifest keeps pointing at the older flat files.
SHARDS = 2           # hex characters of the sha256


def _doc_path(source: str, digest: str, doc_id: str, ext: str) -> str:
    return f"documents/{source}/{digest[:SHARDS]}/{doc_id}{ext}"

EXT_CONTENT_TYPE = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


# A failure that will still be a failure next time. Anything else -- 5xx,
# timeouts, dropped connections, an empty body, a file over the current cap --
# is worth another go on a later run. Only "gone" is permanent.
PERMANENT_ERROR = re.compile(r"\b(404|410)\b", re.I)


def _seen() -> tuple[set[str], set[str]]:
    """Keys not worth trying again, and content hashes already held.

    A failed attempt used to count as seen, which meant one bad afternoon was
    permanent: govinfo returned 502 for 1,634 packages during a content-tier
    outage, every one of them was written to seen.jsonl, and no later run would
    ever have retried them. The corpus would have carried a hole with nothing to
    show it was there.

    So only settled outcomes count -- a document collected, a duplicate
    recognised, or an error that says the thing is genuinely gone. The log still
    records every attempt; this is only about which of them close the door.
    """
    keys, hashes = set(), set()
    if SEEN.exists():
        for line in SEEN.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            err = row.get("error") or ""
            settled = (row.get("doc_id") or row.get("duplicate")
                       or (err and PERMANENT_ERROR.search(err)))
            if settled and row.get("key"):
                keys.add(row["key"])
            if row.get("sha256"):
                hashes.add(row["sha256"])
    return keys, hashes


def _record(row: dict) -> None:
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    with SEEN.open("a") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def _pdf_facts(path: Path) -> tuple[int, str]:
    """Page count and creation date, read in one open.

    Reading rooms list documents without dates -- every one of the 2,126
    collected came back blank -- but the PDF itself usually carries a
    CreationDate. It is not authoritative (re-exporting an old file restamps
    it), which is why it is only used when the listing gave nothing.
    """
    try:
        import pymupdf
        with pymupdf.open(path) as d:
            meta = d.metadata or {}
            m = re.search(r"D:(\d{4})(\d{2})(\d{2})", meta.get("creationDate") or "")
            made = ""
            if m:
                y, mo, dy = (int(x) for x in m.groups())
                if 1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= dy <= 31:
                    made = f"{y:04d}-{mo:02d}-{dy:02d}"
            return d.page_count, made
    except Exception:
        return 0, ""


def _http_date(value: str) -> str:
    """An RFC 7231 Last-Modified header as an ISO date, or blank."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except Exception:
        return ""


def _pick_date(listed: str, pdf_date: str, last_modified: str) -> tuple[str, str]:
    """The best date available, and which one it was.

    Three sources, none of them authoritative on its own. What the listing said
    wins when a listing said anything, because it is the only one the agency
    wrote down deliberately. The PDF's CreationDate comes next. A server's
    Last-Modified is the fallback, and it is what rescues FOIA reading rooms:
    3,441 of their documents carried no date at all, and every one of a sampled
    ten had the header.

    Which one was used is recorded beside the date. A date that means "when the
    file was last written to that web server" should not be silently
    indistinguishable from one the agency published.
    """
    today = time.strftime("%Y-%m-%d")
    for value, origin in ((listed, "listing"), (pdf_date, "pdf"),
                          (last_modified, "http")):
        v = (value or "").strip()[:10]
        # A document released in the future is a bad date, not a scoop.
        if v and v <= today:
            return v, origin
    return "", ""


def collect(source_name: str, since: str, limit: int, max_calls: int,
            use_r2: bool = False, mirror_anyway: bool = False) -> None:
    if source_name in INDEX_ONLY and not mirror_anyway:
        raise SystemExit(
            f"{source_name} is an index, not an archive: its documents are not "
            f"mirrored (see INDEX_ONLY). Collecting would re-upload files that "
            f"were deliberately deleted. Pass --mirror-anyway if you really "
            f"need the bytes in bulk.")
    store.load_env()
    s3 = None
    if use_r2:
        s3 = store.client()
        store.ensure_bucket(s3)
    src = SOURCES[source_name](max_calls=max_calls)
    collection = src.collection

    keys, hashes = _seen()
    scratch = Path(tempfile.mkdtemp(prefix="govdocs-"))
    pending: list[dict] = []
    got = dupes = 0
    host_fails: dict[str, int] = {}
    skipped_hosts: set[str] = set()
    t0 = time.time()

    # `limit` counts documents actually collected, not records discovered.
    #
    # It used to cap discovery instead, which meant a second run walked the
    # same first N records, found every one of them already in seen.jsonl and
    # collected nothing -- a nightly job on a fixed corpus made no progress
    # after its first run. Discovery is left unbounded and the limit applied
    # below, after the already-seen check.
    #
    # This is cheap because no source does per-record HTTP in discover(): they
    # page through listings, and that paging costs the same either way. Yielding
    # a record already held costs a dict. `max_calls` still bounds the paging.
    for rec in src.discover(since=since, limit=None):
        key = f"{source_name}/{rec['notice_id']}_{rec['index']}"
        if key in keys:
            continue
        host = urllib.parse.urlparse(rec.get("url") or "").netloc
        if host in skipped_hosts:
            # Left unrecorded on purpose, so a later run tries it again.
            continue
        try:
            data, filename = src.fetch(rec)
        except Exception as exc:
            _record({"key": key, "url": rec["url"], "error": f"fetch: {exc}"})
            host_fails[host] = host_fails.get(host, 0) + 1
            if host_fails[host] >= HOST_FAILURE_LIMIT:
                skipped_hosts.add(host)
                print(f"  giving up on {host} after {host_fails[host]} failures",
                      flush=True)
            continue
        host_fails[host] = 0
        if not data:
            # A zero-byte response is a bad moment, not a verdict on the file.
            _record({"key": key, "url": rec["url"], "error": "empty response"})
            host_fails[host] = host_fails.get(host, 0) + 1
            if host_fails[host] >= HOST_FAILURE_LIMIT:
                skipped_hosts.add(host)
                print(f"  giving up on {host} after {host_fails[host]} empty replies",
                      flush=True)
            continue
        if len(data) > MAX_BYTES:
            # Recorded, not settled: raising the cap should bring these back
            # rather than leaving them invisible.
            _record({"key": key, "url": rec["url"],
                     "error": f"too large: {len(data)} bytes"})
            continue

        digest = store.sha256(data)
        if digest in hashes:
            # Solicitations re-post identical attachments across amendments.
            dupes += 1
            _record({"key": key, "sha256": digest, "duplicate": True})
            continue
        hashes.add(digest)
        keys.add(key)

        ext = Path(filename).suffix.lower() or ".pdf"
        doc_id = f"{rec['notice_id']}_{rec['index']}"
        r2_key = f"{source_name}/{doc_id}{ext}"
        if s3 is not None:
            store.put(s3, r2_key, data,
                      EXT_CONTENT_TYPE.get(ext, "application/octet-stream"))

        # Staged for the next commit, and removed once it lands.
        rel = _doc_path(source_name, digest, doc_id, ext)
        out = STAGE / collection / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        pages, pdf_date = (_pdf_facts(out) if ext == ".pdf" else (0, ""))
        rec["date"], rec["date_source"] = _pick_date(
            rec.get("date", ""), pdf_date, _http_date(rec.get("last_modified", "")))

        # Held, not written. A collected document closes the door in _seen(),
        # so recording it before its file is pushed means a run killed
        # mid-batch leaves the manifest claiming documents that exist nowhere
        # -- and no later run retries them. The GitHub job hit its timeout two
        # days running, so this is the normal case, not the rare one. Writing
        # after the push costs at most a re-fetch of one batch, which the
        # sha256 check then recognises as a duplicate.
        pending.append({"key": key, "sha256": digest, "r2_key": r2_key,
                        "doc_id": doc_id, "ext": ext, "bytes": len(data),
                        "pages": pages, "path": rel,
                        "filename": filename, "collection": collection, **rec})
        got += 1

        staged_dir = STAGE / collection / "documents" / source_name
        if sum(1 for _ in staged_dir.rglob("*") if _.is_file()) >= BATCH:
            _flush(collection, got, pending)

        if limit and got >= limit:
            break

    shutil.rmtree(scratch, ignore_errors=True)
    _flush(collection, got, pending)
    print(f"collected {got} documents in {time.time()-t0:.0f}s "
          f"({dupes} duplicate files skipped, {src.calls} search calls)")
    if skipped_hosts:
        print(f"  skipped {len(skipped_hosts)} unresponsive host(s)")



def _flush(collection: str, n_so_far: int, pending: list[dict] | None = None) -> None:
    """Push whatever is staged, record it as collected, then clear it.

    The order matters and is the whole point: upload, then write the manifest
    rows, then delete staging. A crash before the upload loses nothing but
    time -- the documents are simply re-fetched. A crash after it costs one
    duplicate batch, which sha256 catches. The reverse order, which this
    replaced, turned every killed run into a permanent hole.
    """
    root = STAGE / collection
    files = [p for p in root.rglob("*") if p.is_file()]
    if not files:
        # Rows waiting with nothing staged behind them is not a batch to
        # record, it is inconsistent state -- the files are gone and the rows
        # would claim documents the dataset does not hold. Drop them: the
        # documents get re-fetched, which costs bandwidth, not a hole.
        if pending:
            print(f"  {len(pending)} recorded rows had no staged files; "
                  f"dropping them so they are collected again", flush=True)
            pending.clear()
        return
    meta_dir = root
    build_metadata(collection, meta_dir)
    publish.ensure_dataset(collection)
    publish.upload_folder(collection, root,
                          f"Add {len(files)} documents ({time.strftime('%Y-%m-%d')})")
    for row in (pending or []):
        _record(row)
    n_rows = len(pending or [])
    if pending:
        pending.clear()
    print(f"  pushed {len(files)} files, recorded {n_rows} "
          f"({n_so_far} collected so far)", flush=True)
    shutil.rmtree(root, ignore_errors=True)


def _rows_for(collection: str) -> list[dict]:
    rows = []
    if SEEN.exists():
        for line in SEEN.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("collection") == collection and r.get("doc_id"):
                rows.append(r)
    return rows


def _published() -> set[str]:
    if not PUBLISHED.exists():
        return set()
    return {l.strip() for l in PUBLISHED.read_text().splitlines() if l.strip()}


def publish_collection(collection: str) -> str:
    """Stream documents that have not been pushed yet into Hugging Face.

    Only what is new. A daily job that re-uploaded the archive every run would
    spend hours moving bytes that are already there and would eventually exceed
    any CI time limit.
    """
    s3 = store.client()
    done = _published()
    rows = [r for r in _rows_for(collection) if r["r2_key"] not in done]
    total_known = len(_rows_for(collection))
    print(f"{len(rows)} new of {total_known} in {collection}")
    repo_id = publish.ensure_dataset(collection)
    if not rows:
        return repo_id

    meta_dir = Path(tempfile.mkdtemp(prefix="govdocs-meta-"))
    if build_metadata(collection, meta_dir) is not None:
        publish.upload_folder(collection, meta_dir, "Update metadata")
    shutil.rmtree(meta_dir, ignore_errors=True)

    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        work = Path(tempfile.mkdtemp(prefix="govdocs-batch-"))
        try:
            for r in chunk:
                dest = work / (r.get("path") or
                               f"documents/{r.get('source','')}/{r['doc_id']}{r.get('ext','')}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    body = s3.get_object(Bucket=store.bucket(), Key=r["r2_key"])["Body"].read()
                except Exception:
                    continue
                dest.write_bytes(body)
            publish.upload_folder(
                collection, work,
                f"Add documents {start + 1}-{start + len(chunk)}")
            PUBLISHED.parent.mkdir(parents=True, exist_ok=True)
            with PUBLISHED.open("a") as fh:
                for r in chunk:
                    fh.write(r["r2_key"] + "\n")
            print(f"  uploaded {start + len(chunk)}/{len(rows)}", flush=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return repo_id


def _published_manifest(collection: str) -> list[dict] | None:
    """The manifest already in the dataset, or None if it could not be read.

    None matters. This machine is not the only writer -- a scheduled Action
    collects the same sources on its own cached state -- and each writer's
    seen.jsonl is a different, partial history. Rebuilding the manifest from one
    of them alone is how the foia dataset ended up with 9,797 files and a
    manifest listing 1,504 of them: 8,300 documents present in the repo and
    absent from the index that is supposed to describe it.

    So the manifest is a union, and when the published copy cannot be fetched
    the answer is to leave it alone rather than overwrite it with less.
    """
    import io
    import requests
    import pyarrow.parquet as pq

    repo = publish.DATASETS[collection]
    url = f"https://huggingface.co/datasets/{repo}/resolve/main/metadata.parquet"
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 404:
                return []          # a new dataset genuinely has no manifest
            r.raise_for_status()
            return pq.read_table(io.BytesIO(r.content)).to_pylist()
        except Exception:
            if attempt == 2:
                return None
            time.sleep(3)
    return None


def build_metadata(collection: str, out_dir: Path) -> Path | None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    published = _published_manifest(collection)
    if published is None:
        print("  could not read the published manifest; leaving it as it is "
              "rather than replacing it with only what this machine knows")
        return None

    rows = []
    if True:
        for r in _rows_for(collection):
            if True:
                rows.append({
                    "doc_id": r["doc_id"], "source": r.get("source", ""),
                    "title": r.get("title", ""),
                    # One spelling per agency for filtering, and the collected
                    # value beside it so nothing is lost to a bad guess.
                    "agency": canonical(r.get("agency", "")),
                    "agency_raw": r.get("agency", ""),
                    "office": r.get("office", ""), "notice_type": r.get("notice_type", ""),
                    "posted_date": r.get("date", ""),
                    # Which of listing / pdf / http the date came from, so a
                    # filter on it can be honest about what it is filtering on.
                    "date_source": r.get("date_source", ""),
                    "url": r.get("url", ""),
                    "landing_url": r.get("landing_url", ""),
                    "filename": r.get("filename", ""), "ext": r.get("ext", ""),
                    "bytes": int(r.get("bytes", 0)), "pages": int(r.get("pages", 0)),
                    "sha256": r.get("sha256", ""),
                    # Older rows predate sharding and are still flat.
                    "path": r.get("path") or
                            f"documents/{r.get('source','')}/{r['doc_id']}{r.get('ext','')}",
                })
    # Union by doc_id. This machine's row wins where both have one: it was
    # written by the code running now, so it carries whatever fields the
    # published copy predates.
    merged = {r.get("doc_id"): r for r in published if r.get("doc_id")}
    mine = {r["doc_id"] for r in rows}
    merged.update({r["doc_id"]: r for r in rows})
    out = out_dir / "metadata.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(list(merged.values())), out)
    kept = len(merged) - len(mine)
    print(f"metadata.parquet: {len(merged)} rows "
          f"({len(rows)} from here, {kept} kept from the published manifest)")
    return out


def _parser() -> argparse.ArgumentParser:
    """Built separately from main() so the checks can inspect the real one."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="sam", choices=sorted(SOURCES))
    ap.add_argument("--since", default="2025-01-20")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--max-calls", type=int, default=200)
    ap.add_argument("--mirror-anyway", action="store_true",
                    help="collect an INDEX_ONLY source anyway (see INDEX_ONLY)")
    ap.add_argument("--r2", action="store_true",
                    help="also keep a copy in R2 (off by default)")
    ap.add_argument("--backfill-dates", metavar="COLLECTION", default=None,
                    help="ask each undated document's server when it was last "
                         "modified; refuses to run while collecting")
    ap.add_argument("--flush", metavar="COLLECTION", default=None,
                    help="push whatever is staged on disk and clear it; use "
                         "after a failed upload left documents behind")
    ap.add_argument("--publish", metavar="COLLECTION", default=None,
                    help="build metadata and push that collection to Hugging Face")
    return ap


def backfill_dates(collection: str, workers: int = 8, delay: float = 0.5) -> None:
    """Give already-collected documents a date from their server.

    Reading rooms mostly do not date their listings, so 3,441 documents were
    collected with no date at all and no way to tell a release from last month
    apart from one from 2014. The server nearly always knows when the file was
    put there. This asks, and records the answer as a `http` date rather than
    pretending it is the same thing as a published one.

    It rewrites seen.jsonl, so it refuses to run while anything is collecting:
    a rewrite would drop whatever a running collector appended in the meantime.
    """
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    if subprocess.run(["pgrep", "-f", "govdocs.collect --source"],
                      capture_output=True).returncode == 0:
        raise SystemExit("a collection is running; seen.jsonl would lose its "
                         "appends. Wait for it to finish.")

    rows = [json.loads(l) for l in SEEN.read_text().splitlines() if l.strip()]
    todo = [r for r in rows
            if r.get("doc_id") and r.get("url")
            and r.get("collection") == collection
            and not (r.get("date") or "").strip()]
    print(f"{len(todo)} undated documents in {collection}")
    if not todo:
        return

    import urllib.parse as _up
    by_host: dict[str, list[dict]] = {}
    for r in todo:
        by_host.setdefault(_up.urlparse(r["url"]).netloc, []).append(r)
    print(f"across {len(by_host)} hosts")

    import requests
    ua = {"User-Agent": "govdocs/0.1 (federal document archive; "
                        "contact: abigail.haddad@gmail.com)"}
    found = {"n": 0}

    def do_host(item):
        host, items = item
        sess = requests.Session()
        sess.headers.update(ua)
        for r in items:
            time.sleep(delay)          # one host, one request at a time
            try:
                h = sess.head(r["url"], timeout=25, allow_redirects=True)
                lm = h.headers.get("Last-Modified", "")
            except Exception:
                continue
            iso = _http_date(lm)
            if iso and iso <= time.strftime("%Y-%m-%d"):
                r["date"], r["date_source"] = iso, "http"
                found["n"] += 1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(do_host, by_host.items()))

    SEEN.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
    print(f"dated {found['n']} of {len(todo)}; seen.jsonl rewritten")
    print("run --publish to push the updated manifest")


def main() -> None:
    a = _parser().parse_args()

    store.load_env()
    if a.backfill_dates:
        backfill_dates(a.backfill_dates)
        return
    if a.flush:
        # A failed push leaves staging intact on purpose: the documents are
        # already recorded as collected, so if they were dropped here they
        # would never be fetched again.
        _flush(a.flush, 0)
        return
    if a.publish:
        repo = publish_collection(a.publish)
        print(f"published to https://huggingface.co/datasets/{repo}")
        return
    collect(a.source, a.since, a.limit, a.max_calls, use_r2=a.r2,
            mirror_anyway=a.mirror_anyway)


if __name__ == "__main__":
    main()
