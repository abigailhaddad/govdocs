"""collect.py — fetch documents, keep them in R2, stage them for publication.

  python -m govdocs.collect --source sam --since 2025-01-20 --limit 500
  python -m govdocs.collect --publish sam

Collection and publication are separate steps on purpose. Fetching is slow and
rate-limited and wants to run often in small bites; pushing to Hugging Face is a
single large commit and wants to run rarely.

R2 is the system of record. The staging directory is a disposable view of what
has not been published yet, so an interrupted upload costs bandwidth and nothing
else.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from . import publish, store
from .sources.oversight import Oversight
from .sources.sam import Sam

SOURCES = {"oversight": Oversight, "sam": Sam}

STAGE = Path("data/stage")
SEEN = Path("data/seen.jsonl")
MAX_BYTES = 200 * 1024 * 1024

EXT_CONTENT_TYPE = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _seen() -> tuple[set[str], set[str]]:
    """Keys and content hashes already collected."""
    keys, hashes = set(), set()
    if SEEN.exists():
        for line in SEEN.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            keys.add(row.get("key", ""))
            if row.get("sha256"):
                hashes.add(row["sha256"])
    return keys, hashes


def _record(row: dict) -> None:
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    with SEEN.open("a") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def _page_count(path: Path) -> int:
    try:
        import pymupdf
        with pymupdf.open(path) as d:
            return d.page_count
    except Exception:
        return 0


def collect(source_name: str, since: str, limit: int, max_calls: int) -> None:
    store.load_env()
    s3 = store.client()
    store.ensure_bucket(s3)
    src = SOURCES[source_name](max_calls=max_calls)
    collection = src.collection

    keys, hashes = _seen()
    staged = STAGE / collection / "documents" / source_name
    staged.mkdir(parents=True, exist_ok=True)
    got = dupes = 0
    t0 = time.time()

    for rec in src.discover(since=since, limit=limit):
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
        store.put(s3, r2_key, data, EXT_CONTENT_TYPE.get(ext, "application/octet-stream"))

        out = staged / f"{doc_id}{ext}"
        out.write_bytes(data)

        _record({"key": key, "sha256": digest, "r2_key": r2_key,
                 "doc_id": doc_id, "ext": ext, "bytes": len(data),
                 "pages": _page_count(out) if ext == ".pdf" else 0,
                 "filename": filename, "collection": collection, **rec})
        got += 1

    print(f"collected {got} documents in {time.time()-t0:.0f}s "
          f"({dupes} duplicate files skipped, {src.calls} search calls)")


def build_metadata(collection: str) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = []
    if SEEN.exists():
        for line in SEEN.read_text().splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("collection") == collection and r.get("doc_id"):
                rows.append({
                    "doc_id": r["doc_id"], "source": r.get("source", ""),
                    "title": r.get("title", ""), "agency": r.get("agency", ""),
                    "office": r.get("office", ""), "notice_type": r.get("notice_type", ""),
                    "posted_date": r.get("date", ""), "url": r.get("url", ""),
                    "landing_url": r.get("landing_url", ""),
                    "filename": r.get("filename", ""), "ext": r.get("ext", ""),
                    "bytes": int(r.get("bytes", 0)), "pages": int(r.get("pages", 0)),
                    "sha256": r.get("sha256", ""),
                    "path": f"documents/{r.get('source','')}/{r['doc_id']}{r.get('ext','')}",
                })
    out = STAGE / collection / "metadata.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), out)
    print(f"metadata.parquet: {len(rows)} rows")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="sam", choices=sorted(SOURCES))
    ap.add_argument("--since", default="2025-01-20")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--max-calls", type=int, default=200)
    ap.add_argument("--publish", metavar="COLLECTION", default=None,
                    help="build metadata and push that collection to Hugging Face")
    a = ap.parse_args()

    store.load_env()
    if a.publish:
        build_metadata(a.publish)
        repo = publish.ensure_dataset(a.publish)
        publish.upload_folder(a.publish, STAGE / a.publish,
                              f"Add documents ({time.strftime('%Y-%m-%d')})")
        print(f"published to https://huggingface.co/datasets/{repo}")
        return
    collect(a.source, a.since, a.limit, a.max_calls)


if __name__ == "__main__":
    main()
