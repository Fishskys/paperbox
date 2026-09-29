#!/usr/bin/env python3
"""Rebuild chunks, embeddings and the search index for stored papers.

    uv run python scripts/reindex.py                 # every non-deleted paper
    uv run python scripts/reindex.py <paper_id> ...  # only the given papers
    uv run python scripts/reindex.py --missing       # only papers without chunks
    uv run python scripts/reindex.py --degraded      # only papers with an open degradation

Use this after changing the chunking strategy, the embedding model or the
OpenSearch mapping (plan sections 15 and 24). The index itself must exist:
run ``python scripts/create_index.py`` first when starting from scratch.

``--degraded`` is the companion of the degradation ledger (plan T7.3): it selects
the papers whose last run had to give something up -- a docling outage, a
semantic-chunking fallback -- and prints the ``stage/code`` pairs that put each
paper on the list, so re-running once the missing dependency is back is one
command. There is deliberately no per-stage variant: the pipeline is cheap to
re-run end to end, and a partial re-run would have to fake the stages before it.
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
from app.services import degradation_service, paper_service  # noqa: E402
from app.workers import tasks  # noqa: E402


def targets(
    session, paper_ids: list[str], missing_only: bool, degraded_only: bool = False
) -> list[Paper]:
    """Non-deleted papers to reindex, narrowed by the requested filters."""
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
    if degraded_only:
        flagged = degradation_service.paper_ids_with_open_degradations(session)
        papers = [paper for paper in papers if paper.id in flagged]
    return papers


def _print_reasons(session, paper_ids: list[str]) -> None:
    """Say why each selected paper is on the list (plan T7.3)."""
    for paper_id in paper_ids:
        rows = degradation_service.list_for_paper(session, paper_id)
        codes = ", ".join(f"{row.stage}/{row.code}" for row in rows) or "?"
        print(f"  degraded: {paper_id}: {codes}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paper_ids", nargs="*", help="paper ids (default: all)")
    parser.add_argument(
        "--missing",
        action="store_true",
        help="only papers that have no chunks yet (initial backfill)",
    )
    parser.add_argument(
        "--degraded",
        action="store_true",
        help=(
            "only papers with an unresolved degradation (plan T7.3), e.g. a "
            "semantic-chunking fallback while the embedding server was down"
        ),
    )
    parser.add_argument(
        "--degraded-stage",
        default=None,
        choices=sorted(degradation_service.STAGES),
        help="with --degraded/--degradations: only this stage",
    )
    parser.add_argument(
        "--degraded-code",
        default=None,
        help="with --degraded/--degradations: only this code (e.g. semantic_fallback)",
    )
    parser.add_argument(
        "--degradations",
        action="store_true",
        help="print every open degradation and exit (no reindex)",
    )
    args = parser.parse_args()

    print(f"embedding model : {settings.embedding_model}")
    print(f"index alias     : {settings.opensearch_alias}")
    opensearch.ensure_index()

    session = SessionLocal()
    failures = 0
    try:
        if args.degradations:
            rows = degradation_service.open_degradations(
                session,
                stage=args.degraded_stage,
                code=args.degraded_code,
            )
            if not rows:
                print("no open degradations")
                return 0
            print(f"{len(rows)} open degradation(s)")
            by_paper: dict[str, list[str]] = {}
            for row in rows:
                by_paper.setdefault(row.paper_id, []).append(
                    f"{row.stage}/{row.code}x{row.occurrences}"
                )
            for paper_id, codes in sorted(by_paper.items()):
                print(f"  {paper_id}: {', '.join(codes)}")
            return 0

        if args.degraded and (args.degraded_stage or args.degraded_code):
            flagged = degradation_service.paper_ids_with_open_degradations(
                session, stage=args.degraded_stage, code=args.degraded_code
            )
            papers = [
                paper
                for paper in targets(session, args.paper_ids, args.missing)
                if paper.id in flagged
            ]
        else:
            papers = targets(session, args.paper_ids, args.missing, args.degraded)
        if not papers:
            print("nothing to reindex")
            return 0
        print(f"reindexing {len(papers)} paper(s)")
        if args.degraded:
            _print_reasons(session, [paper.id for paper in papers])
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
