#!/usr/bin/env python3
"""Purge search documents and stored objects of already-deleted papers.

    uv run python scripts/purge_deleted.py --dry-run
    uv run python scripts/purge_deleted.py
    uv run python scripts/purge_deleted.py --hard

``DELETE /api/papers/{id}`` now cleans OpenSearch and MinIO before marking a row
deleted (plan section 23). Papers deleted **before** that behaviour existed still
have chunk documents in the index - they keep showing up in search results - and
objects in MinIO. This script reconciles those rows; it is idempotent and only
touches papers with ``deleted_at`` set.

By default the ``paper_chunks`` rows in PostgreSQL are intentionally kept (soft
delete, plan section 23), so the purge is limited to OpenSearch + MinIO.

``--hard`` additionally deletes the paper's PostgreSQL rows - chunks, files,
identifiers, sources, field provenance, author/tag links, its ingestion jobs and
finally the ``papers`` row itself. That is irreversible (the metadata layer's
provenance history goes with it), so take a ``pg_dump`` into ``backups/`` first;
without the flag nothing in PostgreSQL is touched.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.models import (  # noqa: E402
    IngestionJob,
    Paper,
    PaperAuthor,
    PaperChunk,
    PaperFieldProvenance,
    PaperFile,
    PaperIdentifier,
    PaperSource,
    PapersTag,
)
from app.db.session import SessionLocal  # noqa: E402
from app.search import opensearch  # noqa: E402
from app.services import object_storage  # noqa: E402

#: Child tables removed by ``--hard`` (every one carries ``paper_id``).
HARD_PURGE_MODELS: tuple[type, ...] = (
    PaperChunk,
    PaperFile,
    PaperIdentifier,
    PaperFieldProvenance,
    PaperAuthor,
    PapersTag,
    PaperSource,
    IngestionJob,
)


def deleted_papers(session) -> list[Paper]:
    """Every soft-deleted paper, oldest deletion first."""
    return list(
        session.execute(
            select(Paper).where(Paper.deleted_at.is_not(None)).order_by(Paper.deleted_at)
        ).scalars()
    )


def count_index_docs(paper_id: str) -> int:
    """Chunk documents still indexed for one paper."""
    response = opensearch.get_client().count(
        index=opensearch.ALIAS,
        body={"query": {"term": {"paper_id": str(paper_id)}}},
    )
    return int(response.get("count", 0))


def hard_delete_paper(session, paper_id: str) -> dict[str, int]:
    """Delete the paper's PostgreSQL rows; returns one count per table.

    The child rows go first and the ``papers`` row last, so a failure half way
    through leaves a consistent soft-deleted paper instead of orphaned children.
    """
    removed: dict[str, int] = {}
    for model in HARD_PURGE_MODELS:
        removed[model.__tablename__] = int(
            session.query(model)
            .filter(model.paper_id == paper_id)
            .delete(synchronize_session=False)
        )
    removed["papers"] = int(
        session.query(Paper)
        .filter(Paper.id == paper_id)
        .delete(synchronize_session=False)
    )
    return removed


def format_hard_counts(counts: dict[str, int]) -> str:
    """``chunks=12 files=1 papers=1`` - only the tables that had rows."""
    return " ".join(
        f"{table}={count}" for table, count in counts.items() if count
    ) or "no postgres rows"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    parser.add_argument(
        "--hard",
        action="store_true",
        help="also delete the paper's PostgreSQL rows (irreversible)",
    )
    args = parser.parse_args()

    session = SessionLocal()
    failures = 0
    purged_docs = 0
    purged_objects = 0
    purged_rows = 0
    try:
        papers = deleted_papers(session)
        if not papers:
            print("no deleted papers to purge")
            return 0
        scope = "postgres rows + documents + objects" if args.hard else "documents + objects"
        print(
            f"{'DRY RUN: ' if args.dry_run else ''}purging {len(papers)} deleted paper(s) "
            f"({scope})"
        )
        for index, paper in enumerate(papers, start=1):
            label = f"[{index}/{len(papers)}] {paper.id} {(paper.title or '')[:40]!r}"
            objects = [
                obj.object_name
                for obj in object_storage.list_objects(prefix=f"papers/{paper.id}/")
            ]
            try:
                docs = count_index_docs(paper.id)
            except Exception as exc:  # noqa: BLE001 - report and keep going
                docs = -1
                print(f"  warn could not count index docs for {paper.id}: {exc}")
            if args.dry_run:
                print(f"  would purge {label}: {docs} chunk doc(s), {len(objects)} object(s)")
                continue
            try:
                removed_chunks = opensearch.delete_by_paper_id(paper.id)
                removed_objects = object_storage.delete_prefix(paper.id)
                counts: dict[str, int] = {}
                if args.hard:
                    counts = hard_delete_paper(session, paper.id)
                    session.commit()
                    purged_rows += sum(counts.values())
            except Exception as exc:  # noqa: BLE001 - keep going, report at the end
                session.rollback()
                failures += 1
                print(f"  FAIL {label}: {type(exc).__name__}: {exc}")
                continue
            purged_docs += removed_chunks
            purged_objects += removed_objects
            suffix = f", {format_hard_counts(counts)}" if args.hard else ""
            print(
                f"  ok   {label}: removed {removed_chunks} chunk doc(s), "
                f"{removed_objects} object(s){suffix}"
            )
        if args.dry_run:
            print("dry run complete, nothing was changed")
            return 0
        print(
            f"done: {len(papers) - failures} ok, {failures} failed "
            f"({purged_docs} chunk doc(s), {purged_objects} object(s) removed"
            + (f", {purged_rows} postgres row(s) deleted)" if args.hard else ")")
        )
        return 0 if failures == 0 else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
