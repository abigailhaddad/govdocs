#!/bin/bash
# Collect on a loop until a time limit. Start under caffeinate:
#
#   nohup caffeinate -i ./collect_loop.sh > /tmp/govdocs.log 2>&1 &
#
# Every source is idempotent -- documents are deduplicated by content hash -- so
# a cycle that finds nothing new costs a little bandwidth and moves on.

set -u
cd "$(dirname "$0")" || exit 1
STOP_AT="${STOP_AT:-$(date -v+10H +%s 2>/dev/null || date -d '+10 hours' +%s)}"
CYCLE=0
log() { echo "[$(date '+%H:%M:%S')] $*"; }

while [ "$(date +%s)" -lt "$STOP_AT" ]; do
  CYCLE=$((CYCLE + 1))
  log "=== cycle $CYCLE ==="
  # The SAM key is shared with other work, so the per-cycle search budget stays
  # small. It is not the bottleneck anyway: one call returns up to 1,000
  # notices, and a recent run found 2,913 documents using 11 calls. Downloading
  # is the slow part and does not touch the key.
  .venv/bin/python -m govdocs.collect --source sam --since 2025-01-20 --limit 4000 --max-calls 120 || log "sam failed"
  .venv/bin/python -m govdocs.collect --source oversight --since 2025-01-20 --limit 900 || log "oversight failed"
  .venv/bin/python -m govdocs.collect --source foia_rooms --since 2025-01-20 --limit 6000 --max-calls 2500 || log "foia_rooms failed"
  log "cycle $CYCLE done"
done
log "stopping after $CYCLE cycles"
