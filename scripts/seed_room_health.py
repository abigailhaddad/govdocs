"""Seed room_health.json from what seen.jsonl already records.

Without this the backoff has to learn from scratch, and the first pass after
deploy spends its whole budget re-reading the rooms we already know give
nothing. The evidence is in the log: every fetch attempt is recorded with the
host it went to, and a host with attempts and no documents has been answering
badly for a while.

Seeded conservatively -- two misses, so a two-day wait rather than a month.
A room that turns out to be alive gets picked up on the next visit and its
record clears.
"""
import collections
import json
import sys
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from govdocs.sources.foia_rooms import HEALTH, refresh_directory
from govdocs.sources.room_overrides import resolve

SEEN = Path("data/seen.jsonl")


def main() -> int:
    got, tried = collections.Counter(), collections.Counter()
    for line in SEEN.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("source") != "foia_rooms" and not str(
                r.get("key", "")).startswith("foia_rooms/"):
            continue
        url = r.get("url") or ""
        if not url:
            continue
        host = urllib.parse.urlsplit(url).netloc
        tried[host] += 1
        if r.get("doc_id"):
            got[host] += 1

    health = {}
    if HEALTH.exists():
        health = json.loads(HEALTH.read_text())

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    seeded = live = 0
    for room in refresh_directory():
        url = room.get("url") or ""
        start = resolve(url)
        host = urllib.parse.urlsplit(start or url).netloc
        if url in health:
            continue
        if got.get(host):
            live += 1
            continue                      # productive: leave it always due
        if tried.get(host, 0) >= 5:
            health[url] = {"last": yesterday, "last_found": 0,
                           "empty_runs": 2, "total": 0,
                           "note": "seeded: attempts recorded, no documents"}
            seeded += 1

    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps(health, indent=1, sort_keys=True))
    print(f"{seeded} rooms seeded as recently empty, {live} left always-due")
    print(f"wrote {HEALTH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
