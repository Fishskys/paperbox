#!/usr/bin/env python3
"""Rebuild chunks, embeddings and the search index for stored papers.

    uv run python scripts/reindex.py                 # every non-deleted paper
    uv run python scripts/reindex.py <paper_id> ...  # only the given papers
    uv run python scripts/reindex.py --missing       # only papers without chunks

Use this after changing the chunking strategy, the embedding model or the
OpenSearch mapping (plan sections 15 and 24). The index itself must exist:
run ``python scripts/create_index.py`` first when starting from scratch.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.models import Paper, PaperChunk  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.search import opensearch  # noqa: E402
from app.services import paper_service  # noqa: E402
from app.workers import tasks  # noqa: E402


def targets(session, paper_ids: list[str], missing_only: bool) -> list[Paper]:
    query = select(Paper).where(Paper.deleted_at.is_(None))
    if paper_ids:
        query = query.where(Paper.id.in_(paper_ids))
    papers = list(session.execute(query.order_by(Paper.created_at)).scalars())
    if missing_only:
        have = {
            row[0]
            for row in session.execute(
                select(PaperChunk.paper_id).group_by(PaperChunk.paper_id)
            )
        }
        papers = [paper for paper in papers if paper.id not in have]
    return papers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paper_ids", nargs="*", help="paper ids (default: all)")
    parser.add_argument(
        "--missing",
        action="store_true",
        help="only papers that have no chunks yet (initial backfill)",
    )
    args = parser.parse_args()

    print(f"embedding model : {settings.embedding_model}")
    print(f"index alias     : {settings.opensearch_alias}")
    opensearch.ensure_index()

    session = SessionLocal()
    failures = 0
    try:
        papers = targets(session, args.paper_ids, args.missing)
        if not papers:
            print("nothing to reindex")
            return 0
        print(f"reindexing {len(papers)} paper(s)")
        for index, paper in enumerate(papers, start=1):
            started = time.perf_counter()
            label = f"[{index}/{len(papers)}] {paper.id} {(paper.title or '')[:48]!r}"
            try:
                job = tasks.reindex_paper(session, paper.id)
            except Exception as exc:  # noqa: BLE001 - keep going, report at the end
                failures += 1
                session.rollback()
                print(f"  FAIL {label}: {type(exc).__name__}: {exc}")
                continue
            chunks = session.execute(
                select(func.count(PaperChunk.id)).where(PaperChunk.paper_id == paper.id)
            ).scalar_one()
            took = time.perf_counter() - started
            print(f"  ok   {label} -> {chunks} chunks, stage={job.stage} ({took:.1f}s)")
        print(f"done: {len(papers) - failures} ok, {failures} failed")
        return 0 if failures == 0 else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
