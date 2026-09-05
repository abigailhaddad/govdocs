"""run_checks.py — offline checks for the wiring that only breaks at runtime.

Every check here exists because something was actually broken, and because the
break was invisible until a collection had already been running for a while.
No network: these are all import-and-inspect, and run in well under a second.

    python run_checks.py
"""

from __future__ import annotations

import inspect
import re

from govdocs import collect, publish

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail and not ok else ''}")
    if not ok:
        FAILURES.append(name)


def check_cli_args_exist() -> None:
    """Every attribute main() reads off the parsed args must be defined.

    `--r2` was referenced as `a.r2` and never added to the parser, so every
    --source run died with AttributeError before fetching a single document.
    Publishing returned earlier and was unaffected, which is why it went unseen.

    The real parser is inspected, not a copy: a copy would drift and quietly
    stop catching this.
    """
    ns = collect._parser().parse_args([])
    read = set(re.findall(r"\ba\.([A-Za-z_][A-Za-z0-9_]*)",
                          inspect.getsource(collect.main)))
    missing = sorted(a for a in read if not hasattr(ns, a))
    check("main() reads only args the parser defines", not missing,
          f"undefined: {missing}")


def check_sources_shape() -> None:
    bad = []
    for name, cls in collect.SOURCES.items():
        for attr in ("name", "collection", "discover", "fetch"):
            if not hasattr(cls, attr):
                bad.append(f"{name}.{attr}")
    check("every source has name/collection/discover/fetch", not bad, str(bad))


def check_source_collections_have_datasets() -> None:
    """A source whose collection has no dataset fails only at the first flush.

    By then it has already fetched up to BATCH documents, so the failure lands
    after the slow part rather than before it.
    """
    missing = sorted({cls.collection for cls in collect.SOURCES.values()}
                     - set(publish.DATASETS))
    check("every source collection maps to a dataset", not missing,
          f"no dataset for: {missing}")


def check_datasets_have_cards() -> None:
    """ensure_dataset() indexes BLURBS and PROVENANCE by collection."""
    missing = [f"{c}:{which}"
               for c in publish.DATASETS
               for which, d in (("blurb", publish.BLURBS), ("provenance", publish.PROVENANCE))
               if c not in d]
    check("every dataset has a blurb and a provenance line", not missing, str(missing))


def check_limit_counts_collected() -> None:
    """`limit` must cap documents collected, not records discovered.

    Capping discovery meant a second run over a fixed corpus walked the same
    first N records, found them all in seen.jsonl and collected nothing, so a
    nightly job stopped making progress after its first run. The seen-check has
    to come first and the limit second.
    """
    src = inspect.getsource(collect.collect)
    unbounded = "src.discover(since=since, limit=None)" in src
    check("discovery is unbounded; limit is applied after the seen-check",
          unbounded, "collect() still passes limit into discover()")

    # The break must come after the `key in keys` skip, or it caps discovery again.
    body = src.split("for rec in src.discover", 1)[-1]
    seen_at = body.find("if key in keys")
    break_at = body.find("got >= limit")
    check("the limit break follows the already-seen skip",
          seen_at != -1 and break_at != -1 and break_at > seen_at,
          f"seen-check at {seen_at}, limit break at {break_at}")


def main() -> int:
    print("govdocs checks")
    check_sources_shape()
    check_source_collections_have_datasets()
    check_datasets_have_cards()
    check_cli_args_exist()
    check_limit_counts_collected()
    print(f"\n{len(FAILURES)} failed" if FAILURES else "\nall passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
