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
- `abigailhaddad/govinfo-documents` — what GPO publishes on govinfo.gov:
  congressional hearings, committee reports and prints, congressional
  documents, GAO reports, agency publications and presidential documents.

They are separate because they are different things and people want one, not
all three. govinfo in particular is not a FOIA corpus: these documents are
published outright rather than released on request, and filing them under a
FOIA dataset would mislabel every row.

## Why

Agencies move, re-number and delete these. SAM.gov attachments disappear when a
notice archives. A copy keeps them citable, and having the corpus in one place
means you can look at ten thousand documents instead of one.

## Running it

    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python boto3 requests huggingface_hub pymupdf pyarrow

    python -m govdocs.collect --source sam --since 2025-01-20 --limit 500
    python -m govdocs.collect --source oversight --since 2025-01-20
    python -m govdocs.collect --source govinfo --since 2020-01-01 --limit 500
    python -m govdocs.collect --publish sam
    python -m govdocs.collect --publish foia

Collecting and publishing are separate. Collecting is slow and rate-limited and
should run often in small bites; publishing is one large commit and should run
rarely.

`--limit` counts documents actually collected, not records discovered, so the
same command run repeatedly keeps making progress instead of re-walking the
records it already has. `data/seen.jsonl` is what it checks against. Discovery
itself is unbounded and `--max-calls` bounds the paging.

govinfo is crawled at one request a second. robots.txt declares no crawl
delay, but 0.2s against files averaging 20 MB drew 502s across the whole content
tier, so the spacing is set by what the host tolerated rather than by what it
permits. A run also stops after `MAX_CONSECUTIVE_FAILURES` failed fetches in a
row: when that tier went down the collector did the locally-correct thing on
each document and so walked 1,634 packages against a server returning 502 to
everything. Crawling slower does not help there, it only spreads the same
pointless traffic over more hours.

govinfo documents are large -- congressional hearings average around 20 MB
against one or two for the other sources -- so `BATCH` documents stage to disk
before the first push frees anything. Keep `--limit` well under `BATCH` there,
or lower `BATCH`, unless there is room for the whole batch at once.

Hugging Face is the store. Documents are staged in batches of 500 and pushed as
a single commit each, then deleted locally, so peak disk is one batch rather
than the whole archive -- which matters when the archive is heading for a
hundred gigabytes.

One commit per batch, never per file: a commit carrying five hundred documents
costs the API about what a commit carrying one does, and the limits are on
requests rather than bytes.

`--r2` additionally keeps a copy in object storage. Off by default; it was
useful before Hugging Face was the destination, and keeping both means paying to
store the same bytes twice.

`data/seen.jsonl` records every attempt, including failures and duplicates, and
is what stops a rerun re-fetching the archive. Only settled outcomes close the
door, though: a document collected, a duplicate recognised, or a 404. A 502 or a
timeout stays retryable, because govinfo returned 502 for 1,634 packages during
one content-tier outage and treating those as done would have left a hole in the
corpus with nothing to show it was there.

## What is collected, and what is not

The test is whether a credential is needed. Endpoints that serve public
documents to anyone -- no key, no account, no agreement entered -- are collected
from; the documents are US government works and carry no copyright. Anything
requiring an account is not, because signing up means accepting terms, and those
terms are not something to work around. MuckRock's API returns 401 without an
account, so it is left alone.

Rate limits and robots.txt are honoured everywhere regardless.

## Sources

`sam` walks the SAM.gov Opportunities API across all nine notice types and every
department. There is no department filter because SAM accepts `deptname` and
ignores it, returning the same total either way — a filter there would look like
it worked and do nothing. Volume is bounded by notice type and date window,
which the API does honour.

`govinfo` walks the per-collection sitemaps GPO publishes for exactly this
purpose. No key: `api.govinfo.gov` is a keyed convenience layer over the same
content and is not used. A sitemap index gives per-year sitemaps, each of which
gives package IDs, and the PDF is at a fixed path under `/content/pkg/`. Title
and issue date come from the package's `mods.xml`, because the sitemap carries
only a `lastmod` and that records when GPO last touched the row rather than
when the document was issued -- a 2001 GAO report has a 2012 lastmod.

Collections are chosen rather than swept: `USCOURTS` alone is 2.17M packages
and would drown the rest, and `BILLSTATUS`, `BILLSUM` and `HOB` are metadata,
not documents. `GAOREPORTS` on govinfo stops at 2008; GAO's own site has 2009
onward and 403s anything but a browser, so that gap is still open.

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

`documentcloud` reaches the releases reading rooms never show. Agencies must
proactively post only the four categories in 5 U.S.C. 552(a)(2) plus records
already requested three or more times; a one-off release to a journalist goes to
that person and nowhere else, which is most of what FOIA produces. DocumentCloud
is where a lot of it ends up -- 455,300 public documents match "foia", uploaded
by the newsrooms that received them, and its search API needs no credentials.

Two things about that API. Passing `order=created_at` destroys relevance
ranking: it then returns the newest public uploads whatever the query, which is
town council agendas. And "freedom of information act" matches 396,713
documents, most citing the statute rather than being a product of it, so the
queries match how agencies TITLE a release instead.

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
documents are foldered by source and `metadata.parquet` is what you query: doc_id, source, agency, agency_raw,
office, title, notice type, posted date, source URL, landing URL, filename,
byte size, page count, and a SHA-256 of the original bytes.

`agency` is one spelling per body, so a filter finds everything: SAM says "DEPT
OF DEFENSE", the FOIA directory says "DoD", oversight.gov says "Department of
War", and a reading room says "SOL" meaning Interior's Solicitor. `agency_raw`
keeps what the source actually said, because a canonicaliser that guesses wrong
files a document under the wrong department and says nothing about it.

`posted_date` comes from the listing where there is one. Reading rooms give
none -- all 2,126 came back blank -- so the PDF's own CreationDate is used as a
fallback. It is not authoritative: re-exporting a 2017 file in 2026 restamps it.

Files are byte-identical to what the agency published. Nothing is converted,
recompressed or edited.

## Checks

    python run_checks.py

Offline, under a second, no network. It checks the wiring that otherwise only
breaks partway through a run: that every source has the methods the collector
calls, that every source's collection has a dataset and a card, and that every
argument `main()` reads is one the parser actually defines -- `--r2` was read
and never defined, so every collection run died with AttributeError while
publishing, which returns earlier, kept working.
