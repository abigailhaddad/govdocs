"""Remove documents stored twice under two different ids.

Before ids were made stable, a document's id depended on the collecting
process, so everything collected before the fix was collected again afterwards
and stored a second time. 7,271 of 21,630 foia_rooms rows are the same URL
twice: one row on the old flat path, one on the sharded path the collector uses
now. The dataset overstates its unique content by a third.

Three things have to move together, or the cleanup makes it worse:

  1. the redundant FILES come out of the dataset,
  2. the manifest stops naming them, and
  3. the collector's seen.jsonl stops naming them -- otherwise the next
     _flush rebuilds the manifest as a union of published and local, re-adds
     the dropped ids, and points them at files that no longer exist.

For (3) the dropped rows become duplicate markers rather than disappearing:
_seen() treats `duplicate` as settled, so the document is not fetched again,
while _rows_for() skips it because it has no doc_id. The checksum stays, so
the content is still recognised.

    .venv/bin/python scripts/dedupe_foia.py                      # dry run
    .venv/bin/python scripts/dedupe_foia.py --apply              # HF: files + manifest
    .venv/bin/python scripts/dedupe_foia.py --patch-seen data/seen.jsonl   # on the box
"""
import argparse
import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from govdocs import collect, publish, store

COLLECTION = "foia"
DROPS = Path("data/dedupe_drops.json")


def choose(group: list[dict]) -> tuple[dict, list[dict]]:
    """Keep one row per checksum: the sharded path, else the longest-lived id.

    Sharded because that is the scheme the collector writes and resolves
    against now, and because the flat directory is the one that hit Hugging
    Face's 10,000-files-per-directory limit.
    """
    def sharded(r: dict) -> bool:
        p = r.get("path") or ""
        parts = p.split("/")
        return len(parts) == 4 and len(parts[2]) == 2

    shard = [r for r in group if sharded(r)]
    keep = (shard or group)[0]
    return keep, [r for r in group if r is not keep]


def plan() -> tuple[list[dict], list[dict], list[dict]]:
    rows = collect._published_manifest(COLLECTION)
    if rows is None:
        raise SystemExit("could not read the published manifest; refusing to act on a partial view")
    groups = collections.defaultdict(list)
    for r in rows:
        if r.get("source") == "foia_rooms" and r.get("sha256"):
            groups[r["sha256"]].append(r)

    keeps, drops = [], []
    for sha, group in groups.items():
        if len(group) < 2:
            continue
        k, d = choose(group)
        keeps.append(k)
        drops.extend(d)
    return rows, keeps, drops


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="delete the files and rewrite the manifest")
    ap.add_argument("--patch-seen", metavar="PATH", default=None,
                    help="rewrite dropped rows in a seen.jsonl into duplicate markers")
    a = ap.parse_args()
    store.load_env()

    if a.patch_seen:
        return patch_seen(Path(a.patch_seen))

    rows, keeps, drops = plan()
    drop_paths = {r["path"] for r in drops if r.get("path")}
    keep_paths = {r["path"] for r in keeps if r.get("path")}

    print(f"manifest rows            {len(rows):,}")
    print(f"duplicate groups         {len(keeps):,}")
    print(f"rows to drop             {len(drops):,}")
    print(f"unique content after     {len(rows) - len(drops):,}")

    # A keeper and a dropped row must never share a path, or deleting one
    # deletes the other's file too.
    clash = drop_paths & keep_paths
    if clash:
        print(f"\nREFUSING: {len(clash)} paths are both kept and dropped, e.g. {list(clash)[:3]}")
        return 1
    print(f"\npaths to delete          {len(drop_paths):,}  (no overlap with kept paths)")

    flat = sum(1 for p in drop_paths if len(p.split("/")) == 3)
    print(f"  of which old flat path {flat:,}")

    # The manifest is a description of the repo, not the repo. Deleting on its
    # word alone risks removing a file whose supposed twin was never there --
    # which would lose the document rather than a copy of it. Ask the repo.
    api = publish.api()
    actual = {f for f in api.list_repo_files(publish.DATASETS[COLLECTION],
                                             repo_type="dataset")
              if f.startswith("documents/")}
    missing_keep = keep_paths - actual
    print(f"\nrepo holds               {len(actual):,} files")
    print(f"  keepers present        {len(keep_paths & actual):,} / {len(keep_paths):,}")
    print(f"  drops present          {len(drop_paths & actual):,} / {len(drop_paths):,}")
    if missing_keep:
        print(f"\nREFUSING: {len(missing_keep)} rows we would keep have no file in the "
              f"repo, so their twin is the only copy. e.g. {sorted(missing_keep)[:3]}")
        return 1

    if not a.apply:
        print("\n(dry run; nothing deleted. --apply to act, then --patch-seen on the box)")
        DROPS.parent.mkdir(parents=True, exist_ok=True)
        DROPS.write_text(json.dumps(sorted(r["doc_id"] for r in drops if r.get("doc_id"))))
        print(f"dropped ids written to {DROPS} for --patch-seen")
        return 0

    import pyarrow as pa
    import pyarrow.parquet as pq
    from huggingface_hub import CommitOperationDelete

    repo = publish.DATASETS[COLLECTION]
    dropped_ids = {r["doc_id"] for r in drops if r.get("doc_id")}

    kept_rows = [r for r in rows if r.get("doc_id") not in dropped_ids]
    tmp = Path("data/metadata.parquet.dedupe")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(kept_rows), tmp)
    print(f"\nrewrote manifest: {len(rows):,} -> {len(kept_rows):,} rows")

    ops = [CommitOperationDelete(path_in_repo=p) for p in sorted(drop_paths & actual)]
    print(f"deleting {len(ops):,} files in one commit...")
    api.create_commit(
        repo_id=repo, repo_type="dataset", operations=ops,
        commit_message=f"Remove {len(ops)} files stored twice under two ids")
    api.upload_file(path_or_fileobj=str(tmp), path_in_repo="metadata.parquet",
                    repo_id=repo, repo_type="dataset",
                    commit_message="Manifest without the de-duplicated rows")
    tmp.unlink(missing_ok=True)

    DROPS.write_text(json.dumps(sorted(dropped_ids)))
    print(f"done. now on the box:")
    print(f"  scripts/dedupe_foia.py --patch-seen data/seen.jsonl")
    return 0


def patch_seen(path: Path) -> int:
    if not DROPS.exists():
        raise SystemExit(f"{DROPS} not found; run the dry run or --apply first")
    dropped = set(json.loads(DROPS.read_text()))
    if not path.exists():
        raise SystemExit(f"{path} not found")

    out, changed = [], 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            out.append(line)
            continue
        if row.get("doc_id") in dropped:
            # Settled, so it is not fetched again; no doc_id, so it does not
            # re-enter the manifest; checksum kept, so the bytes stay known.
            out.append(json.dumps({"key": row.get("key"), "sha256": row.get("sha256"),
                                   "duplicate": True,
                                   "note": "de-duplicated: kept under another id"}))
            changed += 1
        else:
            out.append(line)

    backup = path.with_suffix(path.suffix + ".predupe")
    backup.write_text(path.read_text())
    path.write_text("\n".join(out) + "\n")
    print(f"rewrote {changed:,} rows in {path} (backup at {backup})")
    keys, hashes = collect._seen()
    print(f"_seen() now holds {len(keys):,} settled keys and {len(hashes):,} hashes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
