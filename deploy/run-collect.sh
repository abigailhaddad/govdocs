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

# SAM and the reading rooms, in that order, one pass after another.
#
# SAM was starved in Actions. It walks ptype-major -- all 40 windows of one
# notice type, then the next -- and 120 calls against a 360-call minimum meant
# it never got past the fourth of nine types: Solicitation, Award Notice,
# Justification, Intent to Bundle and Sale of Surplus had zero rows in a
# 16,714-row dataset. The cap is the API's daily quota, not a number we pick,
# and _search stops the source cleanly on a 429.
#
# 2019 because SAM serves nothing before 2018 and little before 2020 --
# probed, not guessed. Earlier windows cost one call each and return nothing.
SAM_SINCE=2019-01-01
SAM_MAX_CALLS=5000

# foia_rooms.discover ignores `since` entirely: the crawl has always taken
# whatever a room lists. The floor is passed for the signature's sake.
FOIA_SINCE=1900-01-01
# Explicit, because omitting --max-calls does not mean unbounded: the CLI
# default is 200, and the first pass on the box ended after 200 calls having
# collected nothing while the Action had been using 3,500. A pass here should
# end when it runs out of rooms.
FOIA_MAX_CALLS=50000

# Big enough not to be the thing that stops a pass. A pass should end because
# the source ran out of material or quota, not because of a number here.
LIMIT=100000
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

  echo "--> sam"
  $PY -m govdocs.collect --source sam \
    --since "$SAM_SINCE" --limit "$LIMIT" --max-calls "$SAM_MAX_CALLS"

  echo "--> foia_rooms"
  # xvfb because about a third of federal FOIA hosts refuse anything but a
  # real browser, and refuse headless too.
  xvfb-run -a $PY -m govdocs.collect --source foia_rooms \
    --since "$FOIA_SINCE" --limit "$LIMIT" --max-calls "$FOIA_MAX_CALLS"

  echo "==> pass ended $(date -u '+%F %T'); sleeping ${PAUSE}s"
  sleep "$PAUSE"
done
