#!/bin/bash
# resume.sh -- collect a round if the source is actually answering.
#
# govinfo's content tier can 502 for hours while its sitemaps and homepage stay
# at 200, so "the site is up" is not the question; "does a document download"
# is. This probes one known-good PDF and does nothing at all unless it gets a
# 200, which keeps a cron entry from walking a dead host every few hours.
#
# Meant for cron:
#   0 */2 * * * /path/to/govdocs/resume.sh
#
# One round per invocation. The collector stops itself after
# MAX_CONSECUTIVE_FAILURES if the host dies mid-round, and skips what it already
# has, so running this more often than necessary is harmless.

set -u
cd "$(dirname "$0")" || exit 1

PROBE="https://www.govinfo.gov/content/pkg/CHRG-119hhrg62435/pdf/CHRG-119hhrg62435.pdf"
UA="govdocs/0.1 (federal document archive; contact: abigail.haddad@gmail.com)"
LOG="data/resume.log"
MIN_FREE_GB=5
LIMIT=100

say() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" >> "$LOG"; }

code=$(curl -s -o /dev/null -m 60 -w '%{http_code}' -A "$UA" --range 0-1024 "$PROBE" 2>/dev/null)
if [ "$code" != "200" ] && [ "$code" != "206" ]; then
  say "skip: probe returned $code"
  exit 0
fi

free=$(df -g . | tail -1 | awk '{print $4}')
if [ "$free" -lt "$MIN_FREE_GB" ]; then
  say "skip: only ${free}Gi free (need ${MIN_FREE_GB})"
  exit 0
fi

say "probe $code, ${free}Gi free -- collecting $LIMIT"
out=$(.venv/bin/python -m govdocs.collect --source govinfo --since 2020-01-01 --limit "$LIMIT" 2>&1)
say "$(echo "$out" | grep -E 'collected|stopped after|Error' | tr '\n' ' ')"
