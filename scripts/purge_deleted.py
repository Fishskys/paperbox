#!/usr/bin/env python3
"""Purge search documents and stored objects of already-deleted papers.

    uv run python scripts/purge_deleted.py --dry-run
    uv run python scripts/purge_deleted.py

``DELETE /api/papers/{id}`` now cleans OpenSearch and MinIO before marking a row
deleted (plan section 23). Papers deleted **before** that behaviour existed still
have chunk documents in the index - they keep showing up in search results - and
objects in MinIO. This script reconciles those rows; it is idempotent and only
touches papers with ``deleted_at`` set.

The ``paper_chunks`` rows in PostgreSQL are intentionally kept (soft delete, plan
section 23), so the purge is limited to OpenSearch + MinIO.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.models import Paper  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.search import opensearch  # noqa: E402
from app.services import object_storage  # noqa: E402


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    args = parser.parse_args()

    session = SessionLocal()
    failures = 0
    purged_docs = 0
    purged_objects = 0
    try:
        papers = deleted_papers(session)
        if not papers:
            print("no deleted papers to purge")
            return 0
        print(f"{'DRY RUN: ' if args.dry_run else ''}purging {len(papers)} deleted paper(s)")
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
            except Exception as exc:  # noqa: BLE001 - keep going, report at the end
                failures += 1
                print(f"  FAIL {label}: {type(exc).__name__}: {exc}")
                continue
            purged_docs += removed_chunks
            purged_objects += removed_objects
            print(
                f"  ok   {label}: removed {removed_chunks} chunk doc(s), "
                f"{removed_objects} object(s)"
            )
        if args.dry_run:
            print("dry run complete, nothing was changed")
            return 0
        print(
            f"done: {len(papers) - failures} ok, {failures} failed "
            f"({purged_docs} chunk doc(s), {purged_objects} object(s) removed)"
        )
        return 0 if failures == 0 else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
