"""Remove mirrored PDFs from abigailhaddad/govinfo-documents.

The dataset is an index, not an archive (see publish.py DATASETS). The files
were deleted once already; a local cron kept re-uploading them because
resume.sh ran --source govinfo. This removes what got re-pushed and leaves
metadata.parquet, README.md and .gitattributes -- the index itself -- in place.

    .venv/bin/python scripts/unmirror_govinfo.py --dry-run
    .venv/bin/python scripts/unmirror_govinfo.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from govdocs import store

REPO = "abigailhaddad/govinfo-documents"
KEEP = {".gitattributes", "README.md", "metadata.parquet"}


def main() -> int:
    dry = "--dry-run" in sys.argv
    store.load_env()
    from huggingface_hub import HfApi

    api = HfApi(token=os.environ["HF_TOKEN"])
    files = api.list_repo_files(REPO, repo_type="dataset")
    docs = [f for f in files if f.startswith("documents/")]
    unexpected = [f for f in files if f not in KEEP and not f.startswith("documents/")]

    print(f"{REPO}: {len(files)} files, {len(docs)} under documents/")
    if unexpected:
        print(f"  refusing: unexpected paths outside documents/: {unexpected}")
        return 1
    if not docs:
        print("  nothing to remove; already an index")
        return 0
    if dry:
        for f in docs[:10]:
            print("  would delete", f)
        print(f"  ... {len(docs)} files total")
        return 0

    api.delete_folder(
        path_in_repo="documents", repo_id=REPO, repo_type="dataset",
        commit_message="Remove mirrored PDFs; this dataset is an index (see README)")
    left = api.list_repo_files(REPO, repo_type="dataset")
    print(f"  deleted {len(docs)}; {len(left)} files remain: {sorted(left)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
