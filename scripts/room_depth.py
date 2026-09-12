"""How many documents sit one level below a room's front page.

room_headroom.py counts documents on the listing the directory points at, and
for half the untouched rooms that is zero -- the documents are behind
sub-listings. BPA and WIPP both showed no direct links and then produced 341
and 123 once the crawler followed them, so the front page is not the corpus.
This follows a couple of sub-listings per room and counts what is there.
"""
import collections
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from govdocs import store
from govdocs.sources.foia_rooms import FoiaRooms, refresh_directory
from govdocs.sources.room_overrides import resolve

ROOMS = 10          # rooms to open
PER_ROOM = 2        # sub-listings to follow in each


def main() -> int:
    store.load_env()
    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq
    p = hf_hub_download("abigailhaddad/foia-reading-room-documents",
                        "metadata.parquet", repo_type="dataset",
                        token=os.environ["HF_TOKEN"])
    rows = pq.read_table(p).to_pylist()
    produced = collections.Counter(
        r.get("agency") for r in rows if r.get("source") == "foia_rooms")
    silent = [r for r in refresh_directory()
              if produced.get(r.get("abbreviation"), 0) == 0]

    random.seed(1)
    random.shuffle(silent)
    src = FoiaRooms(max_calls=ROOMS * (PER_ROOM + 2))
    per_sublisting = []
    opened = 0
    try:
        for room in silent:
            if opened >= ROOMS:
                break
            abbr = room.get("abbreviation") or "?"
            try:
                start = resolve(room["url"])
                html = src._get(start)
                if not html:
                    continue
                docs, listings = src._links(html, start)
            except Exception:
                continue
            if not listings:
                continue
            opened += 1
            counts = []
            for sub in listings[:PER_ROOM]:
                try:
                    sub_html = src._get(sub)
                    if not sub_html:
                        counts.append(0)
                        continue
                    sdocs, _ = src._links(sub_html, sub)
                    counts.append(len(sdocs))
                    per_sublisting.append(len(sdocs))
                except Exception:
                    counts.append(0)
            print(f"  {abbr:14} front page {len(docs):3} docs, "
                  f"{len(listings):3} sub-listings; first {len(counts)} held {counts}")
    finally:
        src.close()

    if per_sublisting:
        per_sublisting.sort()
        med = per_sublisting[len(per_sublisting)//2]
        mean = sum(per_sublisting)/len(per_sublisting)
        print(f"\n  documents per sub-listing: median {med}, mean {mean:.1f}, "
              f"max {max(per_sublisting)} (n={len(per_sublisting)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
