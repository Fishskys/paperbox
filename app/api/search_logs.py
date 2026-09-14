"""``GET /api/search-logs`` -- recent search telemetry (SPEC-P1 section B).

Read-only companion to ``POST /api/search``: it lists what was searched, with
which mode/filters, and how the results came back, so Bad Cases can be triaged
without re-running the query.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.security import require_api_key
from app.db.session import get_db
from app.schemas.search_log import SearchLogListOut, SearchLogOut
from app.services import search_log_service

router = APIRouter(
    prefix="/api/search-logs",
    tags=["search"],
    dependencies=[Depends(require_api_key)],
)


@router.get("", response_model=SearchLogListOut)
def list_search_logs(
    limit: int = Query(default=search_log_service.DEFAULT_LIMIT, ge=1, le=search_log_service.MAX_LIMIT),
    since: datetime | None = Query(default=None),
    mode: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> SearchLogListOut:
    """List logged searches newest first, optionally filtered by time/mode."""
    rows, total = search_log_service.list_search_logs(
        session, limit=limit, since=since, mode=mode
    )
    return SearchLogListOut(
        total=total,
        logs=[
            SearchLogOut.model_validate(search_log_service.serialize_search_log(row))
            for row in rows
        ],
    )


__all__ = ["router"]
