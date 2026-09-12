"""Rebuild data/seen.jsonl from what is already published.

A fresh collector -- a new box, a cleared cache -- starts with no seen.jsonl and
so believes nothing has ever been collected. It would re-fetch the whole corpus,
recognise each file by sha256 as a duplicate, and throw it away: all of the
bandwidth and none of the documents.

The published manifest is the shared record of what exists, and it carries
everything _seen() needs. A manifest row's doc_id is notice_id_index, and the
collector's key is source/doc_id, so the key reconstructs exactly.

    .venv/bin/python scripts/seed_seen.py --dry-run
    .venv/bin/python scripts/seed_seen.py
"""
import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from govdocs import collect, publish, store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=None, help="default: the collector's data/seen.jsonl")
    a = ap.parse_args()

    store.load_env()
    out = Path(a.out) if a.out else collect.SEEN

    existing = set()
    if out.exists():
        for line in out.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("key"):
                existing.add(row["key"])
    print(f"{out}: {len(existing):,} keys already recorded")

    rows, by_source = [], collections.Counter()
    for collection in sorted(set(publish.DATASETS)):
        manifest = collect._published_manifest(collection)
        if manifest is None:
            print(f"  {collection:10} manifest unreadable -- refusing to seed from a "
                  f"partial view")
            return 1
        print(f"  {collection:10} {len(manifest):,} rows published")
        for r in manifest:
            doc_id, source = r.get("doc_id"), r.get("source")
            if not doc_id or not source:
                continue
            key = f"{source}/{doc_id}"
            if key in existing:
                continue
            existing.add(key)
            by_source[source] += 1
            # The whole manifest row, not a summary of it. A seeded row goes
            # on to be a local row in build_metadata's union, and a sparse one
            # there blanked title, agency, notice_type and posted_date across
            # 16,714 SAM rows. Carry everything the manifest knew.
            seeded = dict(r)
            seeded.update({"key": key, "collection": collection,
                           "date": r.get("posted_date") or r.get("date") or "",
                           "seeded_from": "published manifest"})
            rows.append(seeded)

    print(f"\n{len(rows):,} keys to add:")
    for s, n in by_source.most_common():
        print(f"  {s:16} {n:7,}")

    if a.dry_run:
        print("\n(dry run; nothing written)")
        return 0
    if not rows:
        print("\nnothing to add")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    print(f"\nappended to {out}")

    keys, hashes = collect._seen()
    print(f"_seen() now holds {len(keys):,} settled keys and {len(hashes):,} hashes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
