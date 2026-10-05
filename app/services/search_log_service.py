"""Search logging: persist ``POST /api/search`` calls (SPEC-P1 section B).

The MVP kept search telemetry in log lines only, so a Bad Case could not be
replayed or listed. This module writes one ``search_queries`` row per search and
reads them back for ``GET /api/search-logs``.

Two rules shape the design:

* :func:`serialize_results` is a pure function (rank/truncate/shape), so it can
  be unit-tested without a database;
* :func:`log_search` never raises. Logging is telemetry: a broken logging
  database must not turn a working search into a 500.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_key_prefix, get_logger
from app.db.models import SearchQuery, new_uuid

logger = get_logger(__name__)

#: Fields kept per paper in the ``results`` JSONB column.
RESULT_FIELDS: tuple[str, ...] = (
    "paper_id",
    "title",
    "rank",
    "score",
    "retrieval_score",
    "rerank_score",
    "evidence_count",
)

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
MAX_RESULTS_LIMIT = 100


def serialize_results(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Compress search results into the compact logged summary.

    Keeps :data:`RESULT_FIELDS` per entry, numbers them from 1 (``rank`` follows
    the order the search returned, which is already ranked) and truncates to
    ``limit`` entries. ``limit <= 0`` logs no results at all.
    """
    budget = max(0, int(limit))
    if budget == 0:
        return []

    summary: list[dict[str, Any]] = []
    for item in results:
        if len(summary) >= budget:
            break
        if not isinstance(item, dict):
            continue
        # ``rank`` numbers the entries actually logged, so skipping a malformed
        # entry never leaves a gap in 1..N.
        rank = len(summary) + 1
        summary.append(
            {
                "paper_id": item.get("paper_id"),
                "title": item.get("title"),
                "rank": rank,
                "score": item.get("score"),
                "retrieval_score": item.get("retrieval_score", item.get("score")),
                "rerank_score": item.get("rerank_score"),
                "evidence_count": _evidence_count(item),
            }
        )
    return summary


def _whole_ms(took_ms: float | int) -> int:
    """Round a duration to whole milliseconds (half up, not banker's rounding)."""
    return int(Decimal(str(took_ms)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _evidence_count(item: dict[str, Any]) -> int:
    """Number of evidence chunks attached to one result."""
    explicit = item.get("evidence_count")
    if isinstance(explicit, int):
        return explicit
    evidence = item.get("evidence")
    if isinstance(evidence, (list, tuple)):
        return len(evidence)
    matched = item.get("matched_chunks")
    if isinstance(matched, int):
        return matched
    return 0


def log_search(
    session: Session,
    *,
    request_id: str | None,
    query: str,
    mode: str,
    rewritten_query: str | None = None,
    top_k: int,
    rerank: bool,
    filters: dict[str, Any] | None,
    candidates: int | None,
    returned: int,
    took_ms: float | int | None,
    results: list[dict[str, Any]] | None,
) -> None:
    """Best-effort insert of one ``search_queries`` row.

    Never raises: telemetry failures are logged and swallowed so the caller's
    response is unaffected. The caller owns ``session`` and commits for us when
    it is the request session; the API hands in a dedicated session instead.
    """
    if not settings.search_log_enabled:
        return

    try:
        row = SearchQuery(
            id=new_uuid(),
            request_id=(request_id or None),
            query=query,
            rewritten_query=rewritten_query,
            mode=mode,
            top_k=int(top_k),
            rerank=bool(rerank),
            filters=filters,
            candidates=None if candidates is None else int(candidates),
            returned=int(returned),
            took_ms=None if took_ms is None else _whole_ms(took_ms),
            key_prefix=get_key_prefix(),
            results=serialize_results(
                results or [], settings.search_log_results_limit
            ),
        )
        session.add(row)
        session.commit()
    except SQLAlchemyError as exc:
        logger.warning(
            "search log write failed",
            extra={"extra_fields": {"mode": mode, "error": str(exc)}},
        )
        _safe_rollback(session)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break search
        logger.warning(
            "search log write failed",
            extra={"extra_fields": {"mode": mode, "error": str(exc)}},
        )
        _safe_rollback(session)


def _safe_rollback(session: Session) -> None:
    try:
        session.rollback()
    except Exception:  # noqa: BLE001 - nothing else we can do
        logger.warning("search log rollback failed")


def serialize_search_log(row: SearchQuery) -> dict[str, Any]:
    """Shape one ``search_queries`` row for the API response."""
    created = row.created_at
    return {
        "id": row.id,
        "request_id": row.request_id,
        "key_prefix": row.key_prefix,
        "created_at": created.isoformat() if isinstance(created, datetime) else created,
        "query": row.query,
        "rewritten_query": row.rewritten_query,
        "mode": row.mode,
        "top_k": row.top_k,
        "rerank": row.rerank,
        "filters": row.filters,
        "candidates": row.candidates,
        "returned": row.returned,
        "took_ms": row.took_ms,
        "results": row.results,
    }


def list_search_logs(
    session: Session,
    *,
    limit: int = DEFAULT_LIMIT,
    since: datetime | None = None,
    mode: str | None = None,
) -> tuple[list[SearchQuery], int]:
    """Return ``(rows, total)`` newest first, optionally filtered."""
    statement = select(SearchQuery)
    if since is not None:
        statement = statement.where(SearchQuery.created_at >= since)
    if mode:
        statement = statement.where(SearchQuery.mode == mode)

    total = session.execute(
        select(func.count()).select_from(statement.subquery())
    ).scalar_one()
    rows = (
        session.execute(
            statement.order_by(SearchQuery.created_at.desc(), SearchQuery.id.desc())
            .limit(max(1, min(int(limit), MAX_LIMIT)))
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


def get_search_log(session: Session, log_id: str) -> SearchQuery | None:
    """Fetch one logged search by id."""
    return session.get(SearchQuery, log_id)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_RESULTS_LIMIT",
    "RESULT_FIELDS",
    "get_search_log",
    "list_search_logs",
    "log_search",
    "serialize_results",
    "serialize_search_log",
]
