# govdocs

Collects documents that US federal agencies publish, keeps them in object
storage, and mirrors them to Hugging Face so they stay available and can be
worked with in bulk.

Two collections, published as two datasets:

- `abigailhaddad/sam-solicitation-documents` — attachments from federal
  solicitation notices on SAM.gov. Statements of work, performance work
  statements, justifications, amendments, wage determinations.
- `abigailhaddad/foia-reading-room-documents` — Inspector General reports and
  FOIA reading room records.

They are separate because they are different things and people want one or the
other, not both.

## Why

Agencies move, re-number and delete these. SAM.gov attachments disappear when a
notice archives. A copy keeps them citable, and having the corpus in one place
means you can look at ten thousand documents instead of one.

## Running it

    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python boto3 requests huggingface_hub pymupdf pyarrow

    python -m govdocs.collect --source sam --since 2025-01-20 --limit 500
    python -m govdocs.collect --source oversight --since 2025-01-20
    python -m govdocs.collect --publish sam
    python -m govdocs.collect --publish foia

Collecting and publishing are separate. Collecting is slow and rate-limited and
should run often in small bites; publishing is one large commit and should run
rarely.

R2 is the system of record. `data/stage/` is a disposable view of what has not
been pushed yet, so an interrupted upload costs bandwidth and nothing else.
`data/seen.jsonl` records every attempt, including failures and duplicates.

## Sources

`sam` walks the SAM.gov Opportunities API across all nine notice types and every
department. There is no department filter because SAM accepts `deptname` and
ignores it, returning the same total either way — a filter there would look like
it worked and do nothing. Volume is bounded by notice type and date window,
which the API does honour.

`foia_rooms` walks the reading rooms listed in api.foia.gov's own component
directory: 615 FOIA offices, 311 reading rooms, 223 distinct hosts. Their
layouts have nothing in common, so it does not parse them -- it takes every
document link on a reading-room page and follows same-host links that look like
further listings one level deep. Crude, but it works everywhere and degrades to
finding nothing rather than breaking. robots.txt is honoured per host and every
host gets its own rate limiter.

### Hosts that refuse

About a third of federal FOIA hosts answer a plain request with 403 -- 33 of the
first 112 probed, including ATF, DEA, FAA, FCC, FERC, DoD IG and most .mil
sites. A headed browser gets through where nothing else does, so rather than
writing seventy profiles that all say the same thing, the crawler notices the
refusal and switches that host to a browser for the rest of the run.

Two things made this invisible for a while. `RobotFileParser.read()` treats a
403 on robots.txt as disallow-all, and these hosts refuse robots.txt too, so a
site that blocked our fetcher looked like a site that forbade crawling. And
headless is not enough: headless chromium gets "Access Denied" where the same
launch headed returns the page.

### Adding a department

`govdocs/sources/profiles.py` holds what is known about individual agencies:
how to page through a listing, how to recognise a document link, how to pull a
title out of a row, and whether the host needs a proxy. Profiles are keyed by
HOST rather than by agency, because departments share platforms -- one entry
covers all 23 justice.gov reading rooms.

Adding a department means adding an entry there. Anything without one still gets
the generic treatment.

Two things worth knowing before writing one. DOJ serves files from
`/<component>/media/<id>/dl` rather than as `.pdf` links, so a naive PDF scrape
of a DOJ page finds nothing. And justice.gov serves its front page happily while
returning 403 to every `?page=N` request, browser headers and all, which is why
that host is marked `via_proxy` and goes through ScraperAPI. Those requests cost
credits, so the flag is per host rather than a global fallback.

`oversight` reads oversight.gov's api/v2/reports, the government-wide feed of
Inspector General work. One call covers 47 OIGs. It ignores pagination —
`page`, `offset` and `items_per_page` all return the same 1,063 rows — so it is
a rolling window of recent reports and coverage grows by running it regularly.

## Running it on a schedule

`.github/workflows/collect.yml` collects daily and pushes to Hugging Face.

It needs seven secrets: the four `CF_R2_*` values, `SAM_API_KEY`,
`DATAGOV_API_KEY` (both api.data.gov keys) and `HF_TOKEN`.

The one non-obvious bit is `xvfb-run` around the reading-room step. Some
agencies serve their front page to anything and 403 every paginated request;
only a headed browser gets through and headless is refused outright, so the
runner needs a virtual display to run a headed browser on a machine with no
screen.

`data/seen.jsonl` is cached between runs. It is what stops each run re-fetching
the archive; a cache miss costs bandwidth rather than data, since R2 holds the
documents.

## Layout in each dataset

    documents/<source>/<doc_id>.pdf
    metadata.parquet

PDFs are files rather than parquet binary columns, so they stay directly
downloadable. Hugging Face gets slow past roughly 100k files in a repo, so
documents are foldered by source and `metadata.parquet` is what you query:
doc_id, source, agency, office, title, notice type, posted date, source URL,
landing URL, filename, byte size, page count, and a SHA-256 of the original
bytes.

Files are byte-identical to what the agency published. Nothing is converted,
recompressed or edited.
