"""Cross-encoder reranking via the embedding service (SPEC-P1 section D2).

The embedding container exposes ``POST /rerank`` with a cross-encoder
(``Xenova/ms-marco-MiniLM-L-6-v2`` by default). This client is deliberately
forgiving: retrieval must degrade to the first-stage order, never fail, when the
reranker is missing, slow or returns garbage. Every failure path therefore
returns ``None`` (or ``False``) and logs a warning instead of raising.

Contract used by :func:`app.search.hybrid.search_chunks`:

* ``None``  -> "no rerank happened", keep the original order and scores;
* a list   -> scores aligned with the *input* texts via ``index``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

RERANK_PATH = "/rerank"
HEALTH_PATH = "/health"

#: Cross-encoder context is small; documents are truncated before sending.
MAX_DOCUMENT_CHARS = 2000
#: Health probes must not stall a caller (``/health`` is cheap).
HEALTH_TIMEOUT = 2.0


@dataclass(frozen=True)
class RerankScore:
    """One document's cross-encoder score, addressed by input index."""

    index: int
    score: float


def _endpoint(path: str) -> str:
    return f"{settings.rerank_url.rstrip('/')}{path}"


def truncate_document(text: str, limit: int = MAX_DOCUMENT_CHARS) -> str:
    """Trim one document to ``limit`` characters (indices are unaffected)."""
    value = text or ""
    if len(value) <= limit:
        return value
    return value[:limit]


def rerank_texts(
    query: str,
    texts: list[str],
    top_n: int | None = None,
) -> list[RerankScore] | None:
    """Score ``texts`` against ``query`` with the cross-encoder.

    Args:
        query: the user query.
        texts: candidate documents, in first-stage order.
        top_n: ask the service for at most this many results (``None`` = all).

    Returns:
        Scores ordered best first, each ``index`` pointing back into ``texts``;
        ``None`` when reranking is disabled or the service could not answer, so
        the caller keeps the first-stage order.
    """
    if not settings.rerank_enabled:
        return None

    documents = [truncate_document(text) for text in texts]
    if not documents:
        return []

    payload: dict[str, object] = {"query": query, "documents": documents}
    if top_n is not None:
        payload["top_n"] = int(top_n)

    try:
        response = httpx.post(
            _endpoint(RERANK_PATH),
            json=payload,
            timeout=settings.rerank_timeout,
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        logger.warning(
            "rerank request failed, falling back to first-stage order",
            extra={"extra_fields": {"error": str(exc)}},
        )
        return None
    except ValueError as exc:  # non-JSON body
        logger.warning(
            "rerank response was not JSON, falling back",
            extra={"extra_fields": {"error": str(exc)}},
        )
        return None

    scores = _parse_scores(body, len(documents))
    if scores is None:
        return None
    return scores


def _parse_scores(body: object, expected: int) -> list[RerankScore] | None:
    """Validate the service payload; ``None`` means "treat as unavailable"."""
    if not isinstance(body, dict):
        logger.warning("rerank response was not an object")
        return None
    raw = body.get("results")
    if not isinstance(raw, list):
        logger.warning("rerank response is missing 'results'")
        return None
    if len(raw) != expected:
        logger.warning(
            "rerank returned %d scores for %d documents",
            len(raw),
            expected,
        )
        return None

    parsed: list[RerankScore] = []
    for item in raw:
        if not isinstance(item, dict):
            logger.warning("rerank result entry is not an object")
            return None
        index = item.get("index")
        score = item.get("score")
        if not isinstance(index, int) or isinstance(index, bool):
            logger.warning("rerank result is missing an integer index")
            return None
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            logger.warning("rerank result is missing a numeric score")
            return None
        if not 0 <= index < expected:
            logger.warning("rerank result index %s is out of range", index)
            return None
        parsed.append(RerankScore(index=index, score=float(score)))

    parsed.sort(key=lambda item: item.score, reverse=True)
    return parsed


def is_available() -> bool:
    """Whether the rerank service answers ``/health`` (used by healthchecks)."""
    if not settings.rerank_enabled:
        return False
    try:
        response = httpx.get(_endpoint(HEALTH_PATH), timeout=HEALTH_TIMEOUT)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "rerank health check failed",
            extra={"extra_fields": {"error": str(exc)}},
        )
        return False
    if not isinstance(body, dict):
        return False
    return body.get("status") == "ok" or "rerank_model" in body


def rerank_took_ms(started: float) -> int:
    """Milliseconds elapsed since ``started`` (``time.perf_counter``)."""
    return int(round((time.perf_counter() - started) * 1000))


__all__ = [
    "HEALTH_TIMEOUT",
    "MAX_DOCUMENT_CHARS",
    "RerankScore",
    "is_available",
    "rerank_texts",
    "truncate_document",
]
