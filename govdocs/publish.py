"""publish.py — push a collection to its own Hugging Face dataset.

Two collections, two datasets, one codebase:

    sam    -> abigailhaddad/sam-solicitation-documents
    foia   -> abigailhaddad/foia-reading-room-documents

They are kept separate because they are different things. Solicitation
attachments are working procurement paperwork; FOIA library documents are
records released on request. Anyone wanting one rarely wants the other, and a
single repo would make both harder to use.

Layout in each dataset:

    documents/<source>/<doc_id>.pdf     the file as published by the agency
    metadata.parquet                    one row per document

PDFs are uploaded as files rather than packed into parquet binary columns, so
they stay directly downloadable and the dataset viewer can show them. Hugging
Face slows down badly past ~100k files in a repo, so documents are foldered by
source and the manifest carries everything needed to find one without listing
the tree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DATASETS = {
    "sam": "abigailhaddad/sam-solicitation-documents",
    "foia": "abigailhaddad/foia-reading-room-documents",
}

CARD = """---
license: other
license_name: us-government-work
license_link: https://www.usa.gov/government-works
task_categories:
- text-retrieval
language:
- en
tags:
- government
- procurement
- foia
- public-records
pretty_name: {pretty}
size_categories:
- 1K<n<1M
---

# {pretty}

{blurb}

Every document here was published by a US federal agency and is a work of the
United States government. Nothing has been altered: files are byte-identical to
what the agency posted, and the checksum in `metadata.parquet` is of the
original bytes.

## Why this exists

Agencies publish these documents on their own sites, then move, re-number or
remove them. SAM.gov attachments in particular disappear when a notice is
archived. Keeping a copy makes them citable and lets people work with the corpus
in bulk rather than one PDF at a time.

## Layout

    documents/<source>/<doc_id>.pdf
    metadata.parquet

`metadata.parquet` has one row per document: source, agency, title, posted date,
the URL it came from, the page it was linked from, byte size, page count and a
SHA-256 of the file.

## Provenance

{provenance}

Collected with [govdocs](https://github.com/abigailhaddad/govdocs).
"""

BLURBS = {
    "sam": (
        "Attachments from federal solicitation notices on SAM.gov: statements of "
        "work, performance work statements, justifications, amendments, wage "
        "determinations and the rest of the paperwork that accompanies a federal "
        "contract opportunity."),
    "foia": (
        "Documents from federal FOIA reading rooms and Inspector General report "
        "libraries: audits, inspections, investigative summaries and records "
        "released under the Freedom of Information Act."),
}

PROVENANCE = {
    "sam": "Collected from the SAM.gov Opportunities API and the attachment URLs it returns.",
    "foia": ("Collected from agency FOIA libraries and Office of Inspector General "
             "report listings, following each site's robots.txt."),
}


def api():
    from huggingface_hub import HfApi
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is not set. Put a write token in .env or the environment.")
    return HfApi(token=token)


def ensure_dataset(collection: str) -> str:
    repo_id = DATASETS[collection]
    a = api()
    a.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True, private=False)
    pretty = repo_id.split("/")[-1].replace("-", " ").title()
    card = CARD.format(pretty=pretty, blurb=BLURBS[collection],
                       provenance=PROVENANCE[collection])
    a.upload_file(path_or_fileobj=card.encode(), path_in_repo="README.md",
                  repo_id=repo_id, repo_type="dataset",
                  commit_message="Update dataset card")
    return repo_id


def upload_folder(collection: str, local_dir: Path, message: str) -> str:
    repo_id = DATASETS[collection]
    a = api()
    a.upload_folder(folder_path=str(local_dir), repo_id=repo_id,
                    repo_type="dataset", commit_message=message)
    return repo_id
