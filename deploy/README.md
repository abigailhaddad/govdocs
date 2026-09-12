# Running the reading-room crawl on a box instead of Actions

The crawl does not fit in a GitHub job. It walks 400+ hosts with a real
browser, and the step was cut at its timeout mid-crawl on two consecutive days
before the budgets were rebalanced -- and even then it is bounded by a clock
rather than by the rooms. SAM is a short API job and stays in Actions; the
reading rooms come here.

Only one collector may write a collection. Two writers on `foia` is what left
the dataset holding 9,797 files with a manifest listing 1,504 of them, so
`--source foia_rooms` is gone from the workflow. It runs here or nowhere.

## Setup

**Once, in the Hetzner console:** a project, then Security → API tokens →
Generate with read/write. Put it in `deploy/.hcloud.env` (gitignored):

```
HCLOUD_TOKEN=...
```

**Once, on your laptop:** `brew install hcloud`, and register your key if you
have not:

```bash
hcloud ssh-key create --name laptop --public-key-from-file ~/.ssh/id_ed25519.pub
```

**Then:**

```bash
./deploy/server.sh create
ssh root@$IP "printf 'HF_TOKEN=%s\nDATAGOV_API_KEY=%s\n' HF DATAGOV > /etc/govdocs.env && chmod 600 /etc/govdocs.env"
ssh root@$IP 'bash -s' < deploy/install-collect.sh
```

## Day to day

```bash
./deploy/server.sh status     # what exists, and what it has collected
./deploy/server.sh logs       # follow the crawl
./deploy/server.sh update     # pull main and restart
./deploy/server.sh destroy    # stop paying (prompts; --yes to skip)
```

## Cold start

A fresh box has no `seen.jsonl` and would re-fetch the entire corpus, discard
every file as a duplicate by sha256, and collect nothing -- all of the
bandwidth and none of the documents. `run-collect.sh` seeds from the published
manifests before the first pass; `scripts/seed_seen.py --dry-run` shows what it
would add. The manifest is the shared record of what exists, and a row's
`doc_id` plus `source` reconstruct the collector's key exactly.

## Cost and size

`cx22` (2 vCPU / 4 GB) at roughly €0.007/hour. The crawl is politeness-limited
rather than CPU-bound, so the point of a box is hours, not speed, and the
binding constraint is Chromium's memory rather than cores. Billed hourly, so
destroy it when the rooms are exhausted.

Do not carry performance numbers over from a laptop. A shared vCPU is around
four times slower per core, and every timing in the usajobs_historical repo
measured on a dev machine turned out wrong on the box. Re-measure here.

## What to expect

Yield per search call is falling as the productive rooms empty: 1,352 documents
from 2,500 calls on 2026-09-09, and 395 from 3,500 calls on 09-12 with 796
duplicates re-fetched. A sample of the 308 rooms that have never produced
anything found 84% still hold something, median 21 documents on a front page --
but the distribution is savage, and one room in the sample had 461. Expect a
few thousand more documents, arriving unevenly.

Sub-listings look like more than they are: following two per room across ten
rooms, the median yield was zero. Most are navigation, not document pages.
