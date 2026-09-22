#!/usr/bin/env python3
"""Rewrite the metadata snapshot of already-indexed chunks (no re-embedding).

    uv run python scripts/refresh_index_metadata.py --dry-run
    uv run python scripts/refresh_index_metadata.py
    uv run python scripts/refresh_index_metadata.py --paper-id <uuid>

``POST /api/search`` filters read fields that live *inside* the index: venue name
and edition year, paper type, citation fields, identifiers and the tag lists per
kind. Those fields are written when a paper is indexed, so a metadata change - or
a snapshot field that did not exist yet, like the ones added on 2026-09-22 - only
reaches filtering once the documents are rewritten. ``POST /api/papers/{id}/reindex``
does that but re-embeds every chunk (~1 chunk/s); this script updates only the
metadata part in bulk, which takes seconds.

It first sends the mapping (adding *new* fields to a live index is allowed, and it
has to happen before the first document carrying them, otherwise ``dynamic: true``
maps them as ``text``), then one partial update per chunk document. Soft-deleted
papers are skipped - their documents were purged from the index on purpose.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.models import Paper, PaperChunk  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.search import opensearch, snapshot  # noqa: E402
from app.services import paper_service  # noqa: E402

PROGRESS_EVERY = 20


def paper_ids_statement(*, paper_id: str | None = None):
    """Query for the ids of live papers that have chunks.

    ``created_at`` is selected *and* ordered by on purpose: PostgreSQL rejects
    ``SELECT DISTINCT ... ORDER BY`` on an expression that is not in the select
    list (SQLite happily accepts it, so this only ever failed on the real
    database).
    """
    statement = (
        select(Paper.id, Paper.created_at)
        .join(PaperChunk, PaperChunk.paper_id == Paper.id)
        .where(Paper.deleted_at.is_(None))
        .distinct()
        .order_by(Paper.created_at, Paper.id)
    )
    if paper_id:
        statement = statement.where(Paper.id == paper_id)
    return statement


def indexed_paper_ids(session, *, paper_id: str | None = None) -> list[str]:
    """Live papers that have chunk rows (oldest first, for a stable run order)."""
    return [
        str(row[0])
        for row in session.execute(paper_ids_statement(paper_id=paper_id)).all()
    ]


def chunk_ids(session, paper_id: str) -> list[str]:
    """Every chunk id of one paper."""
    return [
        str(row[0])
        for row in session.execute(
            select(PaperChunk.id)
            .where(PaperChunk.paper_id == paper_id)
            .order_by(PaperChunk.chunk_index)
        ).all()
    ]


def build_updates(session, paper_ids: list[str]) -> list[dict]:
    """One ``{"chunk_id", "doc"}`` entry per chunk of the given papers."""
    updates: list[dict] = []
    for current in paper_ids:
        paper = paper_service.get_paper(session, current)
        if paper is None:  # deleted between the query and now
            continue
        doc = snapshot.paper_metadata_snapshot(paper)
        for identifier in chunk_ids(session, current):
            updates.append({"chunk_id": identifier, "doc": doc})
    return updates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    parser.add_argument("--paper-id", default=None, help="refresh one paper only")
    parser.add_argument("--limit", type=int, default=None, help="refresh at most N papers")
    parser.add_argument(
        "--no-mapping",
        action="store_true",
        help="skip the (idempotent) mapping update and only rewrite documents",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        paper_ids = indexed_paper_ids(session, paper_id=args.paper_id)
        if args.limit is not None:
            paper_ids = paper_ids[: max(0, args.limit)]
        if not paper_ids:
            print("no indexed papers to refresh")
            return 0

        updates = build_updates(session, paper_ids)
        print(
            f"{'DRY RUN: ' if args.dry_run else ''}refreshing the metadata snapshot of "
            f"{len(paper_ids)} paper(s) / {len(updates)} chunk document(s)"
        )
        if args.dry_run:
            sample = updates[:3]
            for entry in sample:
                print(f"  would update {entry['chunk_id']}: {sorted(entry['doc'])}")
            print("dry run complete, nothing was changed")
            return 0

        if not args.no_mapping:
            result = opensearch.update_mapping()
            print(f"  mapping {result['index']}: updated={result['updated']}")
        report = opensearch.bulk_update_documents(updates)
        print(
            f"done: {report['updated']} document(s) updated, {report['failed']} failed "
            f"(index {opensearch.ALIAS})"
        )
        return 0 if report["failed"] == 0 else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())