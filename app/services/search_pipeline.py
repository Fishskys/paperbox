"""The ``POST /api/search`` pipeline, shared by REST and the MCP tools.

Both surfaces must return the same numbers for the same request (MCP contract
invariant 2), so the whole flow lives here exactly once: the rewrite gate, the
retrieval call, the response assembly and the search log. The surfaces keep only
what is genuinely theirs -- HTTP status mapping on the REST side, the MCP envelope
and ``citations`` on the agent side.

The pipeline raises the service-level exceptions unchanged
(:class:`app.search.hybrid.SearchError` for a backend that is down, ``ValueError``
for a bad argument) and never an HTTP or MCP type: mapping belongs to the caller.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger, get_request_id
from app.db.session import SessionLocal
from app.schemas.search import (
    SearchEvidence,
    SearchFacets,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchRerankInfo,
    SearchRewriteInfo,
)
from app.services import query_rewrite_service, rerank_service, search_log_service, search_service

logger = get_logger(__name__)


def maybe_rewrite(query: str) -> query_rewrite_service.RewriteOutcome:
    """Rewrite only when the feature is on and the query actually needs it.

    The gate lives here so a disabled feature costs zero HTTP calls, and any
    failure inside the service still yields a pass-through outcome.
    """
    if not settings.query_rewrite_enabled or not query_rewrite_service.needs_rewrite(query):
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


def to_response(
    request: SearchRequest,
    *,
    results: list[Any],
    total: int,
    candidates: int,
    telemetry: dict[str, Any],
    rewrite_outcome: query_rewrite_service.RewriteOutcome,
    retrieval_query: str,
    facets: dict[str, Any] | None,
    took_ms: float,
) -> SearchResponse:
    """Assemble the response body from a service outcome (pure, testable)."""
    payload = [
        SearchResult(
            paper_id=item.paper_id,
            title=item.title,
            authors=item.authors,
            year=item.year,
            doi=item.doi,
            venue=item.venue,
            venue_year=item.venue_year,
            paper_type=item.paper_type,
            volume=item.volume,
            issue=item.issue,
            pages=item.pages,
            publication_date=item.publication_date,
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
    # The reranker "did something" only when a paper actually carries a score.
    reranked = any(item.rerank_score is not None for item in results)
    return SearchResponse(
        query=request.query,
        rewritten_query=retrieval_query if rewrite_outcome.applied else None,
        mode=request.mode,
        backend=str(telemetry.get("backend") or request.backend or settings.search_backend),
        total=total,
        candidates=candidates,
        took_ms=took_ms,
        rerank=SearchRerankInfo(
            enabled=bool(rerank_service.settings.rerank_enabled),
            model=rerank_service.settings.rerank_model if reranked else None,
            took_ms=telemetry.get("rerank_took_ms") if reranked else None,
        ),
        rewrite=SearchRewriteInfo(
            enabled=bool(settings.query_rewrite_enabled),
            applied=bool(rewrite_outcome.applied),
            model=rewrite_outcome.model if rewrite_outcome.applied else None,
            took_ms=rewrite_outcome.took_ms if rewrite_outcome.applied else None,
        ),
        # ``outcome.facets`` is None without the flag and {} when the aggregation
        # failed; both come out as ``null`` (the warning is in the log).
        facets=SearchFacets(**facets) if facets else None,
        results=payload,
    )


def log_search(
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


async def run_search(request: SearchRequest) -> SearchResponse:
    """Run one search request end to end (rewrite, retrieve, assemble, log).

    Blocking engine/encoder work runs in a worker thread (``asyncio.to_thread``),
    so both callers -- a FastAPI handler and an MCP tool -- can await it without
    blocking their event loop.

    Raises whatever the service layer raised: :class:`SearchError` (backend down)
    or ``ValueError`` (bad argument). Mapping to HTTP 503/422 or to an MCP
    ``ToolFailure`` is the caller's job.
    """
    started = time.perf_counter()
    filters = request.filters.to_query_filters() if request.filters else None

    rewrite_outcome = await asyncio.to_thread(maybe_rewrite, request.query)
    retrieval_query = rewrite_outcome.rewritten if rewrite_outcome.applied else request.query

    telemetry: dict[str, Any] = {}
    outcome = await asyncio.to_thread(
        search_service.search_papers,
        retrieval_query,
        request.mode,
        request.top_k,
        filters,
        rerank=request.rerank,
        telemetry=telemetry,
        facets=request.facets,
        backend=request.backend,
    )
    took_ms = round((time.perf_counter() - started) * 1000, 3)
    response = to_response(
        request,
        results=outcome.results,
        total=outcome.total,
        candidates=outcome.candidates,
        telemetry=telemetry,
        rewrite_outcome=rewrite_outcome,
        retrieval_query=retrieval_query,
        facets=outcome.facets,
        took_ms=took_ms,
    )
    # The logging write commits on its own session -- on the event loop that
    # commit blocked every concurrent request (review 2026-10-05, P2-10).
    await asyncio.to_thread(
        log_search,
        request=request,
        filters=filters,
        candidates=outcome.candidates,
        returned=len(response.results),
        took_ms=took_ms,
        results=serialize_results(response.results),
        rewritten_query=retrieval_query if rewrite_outcome.applied else None,
    )
    return response


__all__ = [
    "log_search",
    "maybe_rewrite",
    "run_search",
    "serialize_results",
    "to_response",
]
