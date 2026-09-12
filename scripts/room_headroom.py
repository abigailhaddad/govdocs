"""Estimate how much FOIA reading-room material is still uncollected.

There is no published total: reading rooms do not declare how many documents
they hold, so the only way to size what is left is to go and look. This takes
the rooms that have never produced a document, samples them, fetches each with
the same browser path the collector uses, and counts document links.

    .venv/bin/python scripts/room_headroom.py --sample 25
"""
import argparse
import collections
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from govdocs import store
from govdocs.sources.foia_rooms import FoiaRooms, refresh_directory
from govdocs.sources.room_overrides import resolve


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    store.load_env()
    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq

    p = hf_hub_download("abigailhaddad/foia-reading-room-documents",
                        "metadata.parquet", repo_type="dataset",
                        token=os.environ["HF_TOKEN"])
    rows = pq.read_table(p).to_pylist()
    produced = collections.Counter(
        r.get("agency") for r in rows if r.get("source") == "foia_rooms")

    rooms = refresh_directory()
    silent = [r for r in rooms if produced.get(r.get("abbreviation"), 0) == 0]
    print(f"{len(rooms)} rooms; {len(rooms)-len(silent)} have produced documents, "
          f"{len(silent)} never have")

    random.seed(a.seed)
    sample = random.sample(silent, min(a.sample, len(silent)))
    src = FoiaRooms(max_calls=len(sample) * 3)

    tally = collections.Counter()
    docs_found = []
    try:
        for i, room in enumerate(sample, 1):
            abbr = room.get("abbreviation") or "?"
            try:
                start = resolve(room["url"])
                html = src._get(start)
            except Exception as exc:
                tally["error"] += 1
                print(f"  {i:3}/{len(sample)}  {abbr:12} error: {str(exc)[:50]}")
                continue
            if not html:
                tally["no response"] += 1
                print(f"  {i:3}/{len(sample)}  {abbr:12} no response")
                continue
            docs, listings = src._links(html, start)
            if docs:
                tally["has documents"] += 1
                docs_found.append(len(docs))
                print(f"  {i:3}/{len(sample)}  {abbr:12} {len(docs):4} documents, "
                      f"{len(listings)} sub-listings")
            elif listings:
                tally["sub-listings only"] += 1
                print(f"  {i:3}/{len(sample)}  {abbr:12} 0 documents, "
                      f"{len(listings)} sub-listings to follow")
            else:
                tally["empty"] += 1
                print(f"  {i:3}/{len(sample)}  {abbr:12} nothing")
    finally:
        src.close()

    print("\nsample outcome:")
    for k, v in tally.most_common():
        print(f"  {k:20} {v:3}  ({v/len(sample):.0%})")
    if docs_found:
        docs_found.sort()
        med = docs_found[len(docs_found)//2]
        print(f"\n  documents on a productive page: median {med}, "
              f"mean {sum(docs_found)/len(docs_found):.0f}, max {max(docs_found)}")
        rate = len(docs_found) / len(sample)
        print(f"\n  extrapolating {rate:.0%} x {len(silent)} silent rooms "
              f"x {med} median = ~{int(rate*len(silent)*med):,} documents on "
              f"first pages alone (sub-listings not counted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
