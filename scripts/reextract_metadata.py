#!/usr/bin/env python3
"""Re-extract stored PDF metadata through the shared quality policy.

    uv run python scripts/reextract_metadata.py --dry-run
    uv run python scripts/reextract_metadata.py --dry-run --paper-id <uuid>
    uv run python scripts/reextract_metadata.py --apply
    uv run python scripts/reextract_metadata.py --apply --limit 10

Unlike ``POST /api/papers/{id}/reindex`` this does **not** re-parse the body, re-
chunk, re-embed or rewrite the index from scratch: it only re-runs the metadata
discovery layers (PDF Info/XMP + first two pages + file name) over each stored PDF
and merges the result through the shared authority and PDF quality policy,
protecting manual decisions and structured sources. It applies an improved
extraction rule -- a new identifier source, a better DOI heuristic -- onto the
library that was indexed before the rule existed.

The default is ``--dry-run`` (report what would change, write nothing). ``--apply``
writes the changes and, unless ``--no-index`` is given, rewrites the affected
papers' index-metadata snapshot so the new identifiers reach search filters; the
chunk text and embeddings are untouched. --no-index skips the immediate attempt;
the durable background metadata queue still retries committed changes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import SessionLocal  # noqa: E402
from app.services import metadata_reextract  # noqa: E402


def _print_detail(summary: "metadata_reextract.ReextractSummary") -> None:
    for result in summary.details:
        if not result.changes:
            continue
        print(f"  {result.paper_id}  ({result.filename or 'no file name'})")
        for change in result.changes:
            print(f"      + {change.field}: {change.old!r} -> {change.new!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report only (this is the default; the flag is accepted for clarity)",
    )
    parser.add_argument("--paper-id", default=None, help="one paper only")
    parser.add_argument("--limit", type=int, default=None, help="at most N papers")
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="skip immediate index refresh; background metadata sync still runs",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="summary only, no per-paper detail"
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        if args.paper_id:
            from app.services import paper_service

            paper = paper_service.get_paper(session, args.paper_id)
            if paper is None:
                print(f"paper {args.paper_id} not found", file=sys.stderr)
                return 1
            summary = metadata_reextract.reextract_one(
                session,
                paper,
                apply=args.apply,
                refresh_index=not args.no_index,
                detail=not args.quiet,
            )
        else:
            summary = metadata_reextract.reextract_library(
                session,
                limit=args.limit,
                apply=args.apply,
                refresh_index=not args.no_index,
                detail=not args.quiet,
            )
    finally:
        session.close()

    mode = "APPLY" if args.apply else "DRY RUN"
    if not args.quiet:
        _print_detail(summary)
    print(
        f"\n{mode}: {summary.papers} paper(s) scanned, "
        f"{summary.changed_papers} would change" if not args.apply
        else f"\n{mode}: {summary.papers} paper(s) scanned, {summary.changed_papers} changed"
    )
    print(f"  skipped (no stored file): {summary.skipped_no_file}")
    print(f"  failed: {summary.failed}")
    if summary.added_fields:
        print("  fields changed:")
        for name, count in sorted(summary.added_fields.items()):
            print(f"    {name}: {count}")
    if args.apply:
        print(f"  index documents refreshed: {summary.index_documents}")
        print(f"  papers pending index sync: {summary.index_pending}")
    if summary.quality_counts:
        print("  quality decisions:")
        for reason, count in sorted(summary.quality_counts.items()):
            print(f"    {reason}: {count}")
    if summary.errors:
        print("  errors:")
        for item in summary.errors:
            print(f"    {item['paper_id']}: {item['error']}")
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
