#!/usr/bin/env bash
# One reading-room pass after another, forever.
#
# The whole reason this repo is on a box is that the crawl does not fit in a
# GitHub job: the step was cut at its timeout mid-crawl, every day. Here there
# is no wall clock, so a pass runs until it runs out of rooms rather than out
# of time, and the next one starts after a pause.
#
# Run by govdocs-collect.service. Not meant to be run by hand, though it is
# safe to: the lock means a second copy exits rather than racing the first.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY=.venv/bin/python
SINCE=2025-01-20
# No --max-calls. In Actions that number existed to fit the job inside 5h30m;
# here a pass should end because it has visited the rooms, not because it hit
# an arbitrary budget. --limit still bounds a single pass so the manifest gets
# rebuilt and pushed at a sensible cadence.
LIMIT=4000
PAUSE=1800

# One pass at a time. systemd restarts this unit, and a restart that lands on
# top of a still-running pass would have two collectors staging into the same
# directory and pushing the same collection -- the shape that once left the
# manifest listing 1,504 of 9,797 files.
LOCK=data/.collect.lock
mkdir -p data
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "another pass holds the lock; exiting"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM

# A fresh box has no seen.jsonl and would re-fetch the entire corpus, discard
# every file as a duplicate by sha256, and collect nothing. Seed from what is
# already published before the first pass.
if [ ! -s data/seen.jsonl ]; then
  echo "==> no seen.jsonl; seeding from the published manifests"
  $PY scripts/seed_seen.py || exit 1
fi

while true; do
  echo "==> pass starting $(date -u '+%F %T')"
  # xvfb because about a third of federal FOIA hosts refuse anything but a
  # real browser, and refuse headless too.
  xvfb-run -a $PY -m govdocs.collect \
    --source foia_rooms --since "$SINCE" --limit "$LIMIT"
  echo "==> pass ended $(date -u '+%F %T'); sleeping ${PAUSE}s"
  sleep "$PAUSE"
done
