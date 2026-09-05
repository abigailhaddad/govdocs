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

SEEN = Path("data/seen.jsonl")
PUBLISHED = Path("data/published.jsonl")
STAGE = Path("data/stage")
MAX_BYTES = 200 * 1024 * 1024

# How many documents to hold on disk at once while publishing. 300 averages a
# few hundred megabytes and keeps each Hugging Face commit a sane size.
BATCH = 500          # documents per commit

EXT_CONTENT_TYPE = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


# A failure that will still be a failure next time. Anything else -- 5xx,
# timeouts, dropped connections -- is worth another go on a later run.
PERMANENT_ERROR = re.compile(r"\b(404|410)\b|empty or too large", re.I)


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


def collect(source_name: str, since: str, limit: int, max_calls: int,
            use_r2: bool = False) -> None:
    store.load_env()
    s3 = None
    if use_r2:
        s3 = store.client()
        store.ensure_bucket(s3)
    src = SOURCES[source_name](max_calls=max_calls)
    collection = src.collection

    keys, hashes = _seen()
    scratch = Path(tempfile.mkdtemp(prefix="govdocs-"))
    got = dupes = 0
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
        try:
            data, filename = src.fetch(rec)
        except Exception as exc:
            _record({"key": key, "url": rec["url"], "error": f"fetch: {exc}"})
            continue
        if not data or len(data) > MAX_BYTES:
            _record({"key": key, "url": rec["url"], "error": "empty or too large"})
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
        staged = STAGE / collection / "documents" / source_name
        staged.mkdir(parents=True, exist_ok=True)
        out = staged / f"{doc_id}{ext}"
        out.write_bytes(data)
        pages, pdf_date = (_pdf_facts(out) if ext == ".pdf" else (0, ""))
        if not (rec.get("date") or "").strip():
            rec["date"] = pdf_date

        _record({"key": key, "sha256": digest, "r2_key": r2_key,
                 "doc_id": doc_id, "ext": ext, "bytes": len(data),
                 "pages": pages,
                 "filename": filename, "collection": collection, **rec})
        got += 1

        staged_dir = STAGE / collection / "documents" / source_name
        if len(list(staged_dir.glob("*"))) >= BATCH:
            _flush(collection, got)

        if limit and got >= limit:
            break

    shutil.rmtree(scratch, ignore_errors=True)
    _flush(collection, got)
    print(f"collected {got} documents in {time.time()-t0:.0f}s "
          f"({dupes} duplicate files skipped, {src.calls} search calls)")


def _flush(collection: str, n_so_far: int) -> None:
    """Push whatever is staged, then clear it."""
    root = STAGE / collection
    files = [p for p in root.rglob("*") if p.is_file()]
    if not files:
        return
    meta_dir = root
    build_metadata(collection, meta_dir)
    publish.ensure_dataset(collection)
    publish.upload_folder(collection, root,
                          f"Add {len(files)} documents ({time.strftime('%Y-%m-%d')})")
    print(f"  pushed {len(files)} files ({n_so_far} collected so far)", flush=True)
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
    build_metadata(collection, meta_dir)
    publish.upload_folder(collection, meta_dir, "Update metadata")
    shutil.rmtree(meta_dir, ignore_errors=True)

    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        work = Path(tempfile.mkdtemp(prefix="govdocs-batch-"))
        try:
            for r in chunk:
                dest = work / "documents" / r.get("source", "") / f"{r['doc_id']}{r.get('ext','')}"
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


def build_metadata(collection: str, out_dir: Path) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

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
                    "posted_date": r.get("date", ""), "url": r.get("url", ""),
                    "landing_url": r.get("landing_url", ""),
                    "filename": r.get("filename", ""), "ext": r.get("ext", ""),
                    "bytes": int(r.get("bytes", 0)), "pages": int(r.get("pages", 0)),
                    "sha256": r.get("sha256", ""),
                    "path": f"documents/{r.get('source','')}/{r['doc_id']}{r.get('ext','')}",
                })
    out = out_dir / "metadata.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), out)
    print(f"metadata.parquet: {len(rows)} rows")
    return out


def _parser() -> argparse.ArgumentParser:
    """Built separately from main() so the checks can inspect the real one."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="sam", choices=sorted(SOURCES))
    ap.add_argument("--since", default="2025-01-20")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--max-calls", type=int, default=200)
    ap.add_argument("--r2", action="store_true",
                    help="also keep a copy in R2 (off by default)")
    ap.add_argument("--publish", metavar="COLLECTION", default=None,
                    help="build metadata and push that collection to Hugging Face")
    return ap


def main() -> None:
    a = _parser().parse_args()

    store.load_env()
    if a.publish:
        repo = publish_collection(a.publish)
        print(f"published to https://huggingface.co/datasets/{repo}")
        return
    collect(a.source, a.since, a.limit, a.max_calls, use_r2=a.r2)


if __name__ == "__main__":
    main()
