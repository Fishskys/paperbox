"""``POST /api/search`` -- paper-level hybrid search (MVP-SPEC section 8).

The route is a thin wrapper: validate the body, run the chunk-level retrieval
through :mod:`app.search.hybrid`, aggregate into papers and serialize. The
blocking OpenSearch/embedding calls run in a worker thread so the event loop
stays responsive.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import settings
from app.core.logging import get_logger, get_request_id
from app.core.security import require_api_key
from app.db.session import SessionLocal
from app.schemas.search import (
    SearchEvidence,
    SearchRerankInfo,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchRewriteInfo,
)
from app.services import (
    query_rewrite_service,
    rerank_service,
    search_log_service,
    search_service,
)

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["search"],
    dependencies=[Depends(require_api_key)],
)

#: Paper aggregation over-fetches chunks: ``top_k`` papers need a wider pool.
CANDIDATE_FACTOR = 5


@router.post("/search", response_model=SearchResponse)
async def search(request: SearchRequest) -> SearchResponse:
    """Run a keyword / semantic / hybrid search and return paper results.

    With ``rerank=true`` the chunk retrieval becomes two-stage: the first stage
    over-fetches ``top_k * RERANK_CANDIDATES`` chunks and the cross-encoder
    rescores them before paper aggregation (SPEC-P1 section D2).

    When ``QUERY_REWRITE_ENABLED`` is on and the query contains CJK text, the
    query is first rewritten into an English search expression and *that* text
    drives retrieval (SPEC-P1 section I1). ``query`` in the response is always
    the original; the rewrite is reported separately under ``rewrite``.
    """
    started = time.perf_counter()
    filters = request.filters.to_query_filters() if request.filters else None
    #: Chunks pulled before paper aggregation (``top_k`` papers need a wide pool).
    candidates = request.top_k * CANDIDATE_FACTOR

    rewrite_outcome = await asyncio.to_thread(_maybe_rewrite, request.query)
    retrieval_query = (
        rewrite_outcome.rewritten if rewrite_outcome.applied else request.query
    )
    rewrite_info = SearchRewriteInfo(
        enabled=bool(settings.query_rewrite_enabled),
        applied=bool(rewrite_outcome.applied),
        model=rewrite_outcome.model if rewrite_outcome.applied else None,
        took_ms=rewrite_outcome.took_ms if rewrite_outcome.applied else None,
    )

    telemetry: dict[str, Any] = {}
    try:
        results, total = await asyncio.to_thread(
            search_service.search_papers,
            retrieval_query,
            request.mode,
            request.top_k,
            filters,
            rerank=request.rerank,
            telemetry=telemetry,
        )
    except search_service.SearchError as exc:
        logger.warning("search failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="search backend unavailable",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    took_ms = round((time.perf_counter() - started) * 1000, 3)
    # The reranker "did something" only when a paper actually carries a score.
    reranked = any(item.rerank_score is not None for item in results)
    rerank_info = SearchRerankInfo(
        enabled=bool(rerank_service.settings.rerank_enabled),
        model=rerank_service.settings.rerank_model if reranked else None,
        took_ms=telemetry.get("rerank_took_ms") if reranked else None,
    )
    payload = [
        SearchResult(
            paper_id=item.paper_id,
            title=item.title,
            authors=item.authors,
            year=item.year,
            doi=item.doi,
            score=item.score,
            relevance=item.relevance,
            retrieval_score=item.retrieval_score,
            rerank_score=item.rerank_score,
            evidence=[
                SearchEvidence(
                    chunk_id=evidence.chunk_id,
                    page=evidence.page,
                    section=evidence.section,
                    text=evidence.text,
                )
                for evidence in item.evidence
            ],
        )
        for item in results
    ]
    _log_search(
        request=request,
        filters=filters,
        candidates=candidates,
        returned=len(payload),
        took_ms=took_ms,
        results=serialize_results(payload),
        rewritten_query=retrieval_query if rewrite_outcome.applied else None,
    )

    return SearchResponse(
        query=request.query,
        rewritten_query=retrieval_query if rewrite_outcome.applied else None,
        mode=request.mode,
        total=total,
        took_ms=took_ms,
        rerank=rerank_info,
        rewrite=rewrite_info,
        results=payload,
    )


def _maybe_rewrite(query: str) -> query_rewrite_service.RewriteOutcome:
    """Rewrite only when the feature is on and the query actually needs it.

    The gate lives here so a disabled feature costs zero HTTP calls, and any
    failure inside the service still yields a pass-through outcome.
    """
    if not settings.query_rewrite_enabled or not query_rewrite_service.needs_rewrite(
        query
    ):
        return query_rewrite_service.RewriteOutcome(query, query, False)
    return query_rewrite_service.rewrite_query(query)


def serialize_results(payload: list[SearchResult]) -> list[dict]:
    """Compact the response results for logging (rank/score/evidence count).

    ``retrieval_score``/``rerank_score`` are part of the logged shape so a Bad
    Case can be replayed with the exact numbers the client saw (P1 B).
    """
    return [
        {
            "paper_id": item.paper_id,
            "title": item.title,
            "score": item.score,
            "retrieval_score": item.retrieval_score,
            "rerank_score": item.rerank_score,
            "evidence_count": len(item.evidence),
        }
        for item in payload
    ]


def _log_search(
    *,
    request: SearchRequest,
    filters: dict | None,
    candidates: int,
    returned: int,
    took_ms: float,
    results: list[dict],
    rewritten_query: str | None = None,
) -> None:
    """Persist the search on a dedicated session; never affect the response.

    A separate session is used so a logging commit can neither end nor roll back
    the request transaction, and any failure is swallowed inside
    :func:`search_log_service.log_search`.
    """
    session = SessionLocal()
    try:
        search_log_service.log_search(
            session,
            request_id=get_request_id() or uuid.uuid4().hex,
            query=request.query,
            rewritten_query=rewritten_query,
            mode=request.mode,
            top_k=request.top_k,
            rerank=bool(request.rerank),
            filters=filters,
            candidates=candidates,
            returned=returned,
            took_ms=took_ms,
            results=results,
        )
    finally:
        session.close()


__all__ = ["router"]
