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

`oversight` reads oversight.gov's api/v2/reports, the government-wide feed of
Inspector General work. One call covers 47 OIGs. It ignores pagination —
`page`, `offset` and `items_per_page` all return the same 1,063 rows — so it is
a rolling window of recent reports and coverage grows by running it regularly.

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
