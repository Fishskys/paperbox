#!/usr/bin/env python3
"""Rebuild chunks, embeddings and the search index for stored papers.

    uv run python scripts/reindex.py                 # every non-deleted paper
    uv run python scripts/reindex.py <paper_id> ...  # only the given papers
    uv run python scripts/reindex.py --missing       # only papers without chunks
    uv run python scripts/reindex.py --degraded      # only papers with an open degradation
    uv run python scripts/reindex.py --degraded --degraded-stage parsing \
        --degraded-code docling_unavailable          # exactly "docling was down"
    uv run python scripts/reindex.py --parser-backend pypdf   # by stamp, ledger or not
    uv run python scripts/reindex.py --parser-backend pypdf --dry-run   # just the list

Use this after changing the chunking strategy, the embedding model or the
OpenSearch mapping (plan sections 15 and 24). The index itself must exist:
run ``python scripts/create_index.py`` first when starting from scratch.

``--degraded`` is the companion of the degradation ledger (plan T7.3): it selects
the papers whose last run had to give something up -- a docling outage, a
semantic-chunking fallback -- and prints the ``stage/code`` pairs that put each
paper on the list, so re-running once the missing dependency is back is one
command. There is deliberately no per-stage variant: the pipeline is cheap to
re-run end to end, and a partial re-run would have to fake the stages before it.

``--parser-backend`` is the *stamp* selector and answers the other half of the
same question: a paper parsed by pypdf has ``papers.parser_backend='pypdf'``
whether that was a fallback (ledger row, ``--degraded`` sees it) or the configured
backend at the time (no ledger row). ``unknown`` selects rows with no stamp at all
-- the papers indexed before the column existed. So "docling is healthy again,
re-parse everything it did not produce" is::

    uv run python scripts/reindex.py --parser-backend pypdf
    uv run python scripts/reindex.py --parser-backend unknown

and ``--dry-run`` prints the selection without touching the library.
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
from app.core.config import PARSER_BACKENDS  # noqa: E402
from app.services import degradation_service, paper_service  # noqa: E402
from app.services import reindex_service  # noqa: E402
from app.services.consistency_service import UNKNOWN_BACKEND  # noqa: E402
from app.workers import tasks  # noqa: E402

#: Accepted values of ``--parser-backend``: the two real backends plus the key for
#: "this row has no stamp" (same spelling the consistency census uses).
STAMP_CHOICES = tuple(sorted(PARSER_BACKENDS | {UNKNOWN_BACKEND}))


def targets(
    session,
    paper_ids: list[str],
    missing_only: bool,
    degraded_only: bool = False,
    *,
    degraded_stage: str | None = None,
    degraded_code: str | None = None,
    parser_backend: str | None = None,
    auto: bool = False,
) -> tuple[list[Paper], list[reindex_service.ReindexReason]]:
    """Non-deleted papers to reindex, narrowed by every requested filter (AND).

    Selection is **shared with the API** (``app/services/reindex_service.py``): the
    same helpers answer "which papers have no chunks", "which stamps are stale",
    "which have open degradations" -- so the script, the endpoint and the report can
    never disagree about who needs a rebuild. Only the *execution* differs: this
    script runs the pipeline in-process (interruptible, ``--dry-run``-able), the
    endpoint queues one job per paper.

    ``--auto`` is the detection-driven path (换模型 / 换解析器 / 有降级 / 缺 chunk),
    and prints why each reason fired.
    """
    if auto:
        plan = reindex_service.select_papers(session, paper_ids=paper_ids or None)
        return plan.papers, plan.reasons

    wanted: set[str] | None = None
    reasons: list[reindex_service.ReindexReason] = []
    if missing_only:
        wanted = reindex_service.papers_without_chunks(session)
        reasons.append(
            reindex_service.ReindexReason(
                code=reindex_service.REASON_MISSING_CHUNKS,
                detail="还没有 chunk",
                papers=len(wanted),
                scope="subset",
            )
        )
    if degraded_only:
        flagged = degradation_service.paper_ids_with_open_degradations(
            session, stage=degraded_stage, code=degraded_code
        )
        wanted = flagged if wanted is None else (wanted & flagged)
        reasons.append(
            reindex_service.ReindexReason(
                code=reindex_service.REASON_OPEN_DEGRADATIONS,
                detail="有未决降级",
                papers=len(flagged),
                scope="subset",
            )
        )
    if parser_backend:
        # 语义：只挑**戳等于**该值的论文（"把 pypdf 解析的那些重跑一遍"）
        stamped = reindex_service.papers_with_stamp(session, parser_backend)
        wanted = stamped if wanted is None else (wanted & stamped)
        reasons.append(
            reindex_service.ReindexReason(
                code=reindex_service.REASON_PARSER_DRIFT,
                detail=f"解析戳是 {parser_backend}",
                papers=len(stamped),
                scope="subset",
            )
        )

    query = select(Paper).where(Paper.deleted_at.is_(None))
    if paper_ids:
        query = query.where(Paper.id.in_(paper_ids))
    papers = list(session.execute(query.order_by(Paper.created_at)).scalars())
    if wanted is not None:
        papers = [paper for paper in papers if paper.id in wanted]
    return papers, reasons


def _print_reasons(session, papers: list[Paper]) -> None:
    """Say why each selected paper is on the list: stamp + open degradations."""
    for paper in papers:
        rows = degradation_service.list_for_paper(session, paper.id)
        codes = ", ".join(f"{row.stage}/{row.code}" for row in rows) or "-"
        stamp = paper.parser_backend or UNKNOWN_BACKEND
        print(f"  {paper.id}: stamp={stamp} degraded={codes}")


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
    parser.add_argument(
        "--parser-backend",
        default=None,
        choices=STAMP_CHOICES,
        help=(
            "only papers whose current parser stamp is this backend (reads "
            "papers.parser_backend, so it also catches papers imported while that "
            "backend was configured on purpose); 'unknown' = no stamp yet"
        ),
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "let the detection decide: 换 embedding 模型 / 换解析器 / 有未决降级 / 缺 chunk "
            "分别命中，并打印每条理由（与 POST /api/papers/reindex 同一套检测）"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the selection (with stamp and open degradations) and exit",
    )
    args = parser.parse_args()

    print(f"embedding model : {settings.embedding_model}")
    print(f"index alias     : {settings.opensearch_alias}")

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

        papers, reasons = targets(
            session,
            args.paper_ids,
            args.missing,
            args.degraded,
            degraded_stage=args.degraded_stage,
            degraded_code=args.degraded_code,
            parser_backend=args.parser_backend,
            auto=args.auto,
        )
        if reasons:
            print("detected reasons:")
            for reason in reasons:
                print(f"  [{reason.code}] scope={reason.scope} papers={reason.papers}")
                print(f"      {reason.detail}")
        if not papers:
            print("nothing to reindex" if not args.dry_run else "no paper matches")
            return 0
        if args.dry_run:
            print(f"would reindex {len(papers)} paper(s)")
            _print_reasons(session, papers)
            return 0
        print(f"reindexing {len(papers)} paper(s)")
        if args.degraded or args.parser_backend or args.auto:
            _print_reasons(session, papers)
        opensearch.ensure_index()
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
