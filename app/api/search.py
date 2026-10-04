"""``POST /api/search`` -- paper-level hybrid search (MVP-SPEC section 8).

The route is a thin wrapper: it validates the body, runs
:func:`app.services.search_pipeline.run_search` (shared with the MCP tools, so both
surfaces report the same numbers for the same request) and maps the pipeline's
exceptions onto HTTP statuses. Everything else -- the rewrite gate, the retrieval
call, the response assembly, the search log -- lives in the pipeline.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.logging import get_logger
from app.core.security import require_api_key
from app.schemas.search import SearchRequest, SearchResponse
from app.services import search_pipeline, search_service

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["search"],
    dependencies=[Depends(require_api_key)],
)


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
    try:
        return await search_pipeline.run_search(request)
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


__all__ = ["router"]
