"""Query rewriting via an OpenAI-compatible chat endpoint (SPEC-P1 section I1).

Measured on the live corpus, a Chinese query retrieves badly (top-1 hit rate
0.30) while the same query rewritten into an English search expression hits
1.00. This module makes that rewrite a first-class, *optional* step of the
search path.

It is off unless ``QUERY_REWRITE_ENABLED=true``. Every failure mode - endpoint
unreachable, timeout, non-200, malformed body, empty choice, a rewrite that
comes back empty or identical to the original - degrades to "no rewrite" and is
logged as a warning; the caller never has to guard against an exception.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: CJK code point ranges: Han, Hiragana/Katakana, Hangul.
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

#: Room for one short search expression; the rewrite is not a summary.
#: The *effective* budget comes from ``QUERY_REWRITE_MAX_TOKENS`` (default 512)
#: because reasoning models spend tokens on hidden reasoning before emitting the
#: visible answer -- see ``Settings.query_rewrite_max_tokens``.
DEFAULT_MAX_TOKENS = 512

SYSTEM_PROMPT = (
    "You rewrite academic search queries into a concise English search "
    "expression for a paper retrieval system. Preserve every technical term, "
    "acronym, number and unit. Output only the search expression itself: no "
    "quotes, no explanation, no numbering, no trailing punctuation."
)


@dataclass(frozen=True)
class RewriteOutcome:
    """Result of one rewrite attempt (always returned, never raised)."""

    original: str
    rewritten: str
    applied: bool
    model: str | None = None
    took_ms: int | None = None
    reason: str | None = None


def needs_rewrite(query: str) -> bool:
    """True when the query contains CJK and is short enough to send.

    Pure-ASCII queries (including cross-language English ones) are already in
    the retrieval language, and an empty or oversized query is not worth an LLM
    round trip.
    """
    text = (query or "").strip()
    if not text:
        return False
    if len(text) > settings.query_rewrite_max_chars:
        return False
    return bool(CJK_PATTERN.search(text))


def build_rewrite_messages(query: str) -> list[dict[str, str]]:
    """Chat messages for the rewrite call (system instruction + user query)."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (query or "").strip()},
    ]


def clean_rewrite(raw: str) -> str:
    """Normalize a model answer into a single-line search expression.

    Strips surrounding whitespace and any matching quote pair, flattens
    newlines/tabs into single spaces and truncates to
    ``QUERY_REWRITE_MAX_CHARS``.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    for opening, closing in (('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019")):
        if len(text) >= 2 and text.startswith(opening) and text.endswith(closing):
            text = text[1:-1].strip()
            break
    text = re.sub(r"\s+", " ", text).strip()
    limit = int(settings.query_rewrite_max_chars)
    if limit > 0 and len(text) > limit:
        text = text[:limit].strip()
    return text


def _extract_content(payload: object) -> str | None:
    """Pull ``choices[0].message.content`` out of an OpenAI-style body."""
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
    # Some gateways answer with ``text`` instead of ``message.content``.
    text = first.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return None


def rewrite_query(query: str) -> RewriteOutcome:
    """Rewrite ``query`` into English; degrade silently on any failure."""
    original = (query or "").strip()
    if not original:
        return RewriteOutcome(original, original, False, reason="empty query")
    if not settings.query_rewrite_enabled:
        return RewriteOutcome(original, original, False, reason="rewrite disabled")

    model = settings.query_rewrite_model
    url = f"{settings.query_rewrite_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": build_rewrite_messages(original),
        "temperature": 0,
        "max_tokens": settings.query_rewrite_max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {settings.query_rewrite_api_key}",
        "Content-Type": "application/json",
    }

    started = time.perf_counter()
    try:
        response = httpx.post(
            url, json=body, headers=headers, timeout=settings.query_rewrite_timeout
        )
        took_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            logger.warning(
                "query rewrite returned a non-200 response",
                extra={"extra_fields": {"status": response.status_code}},
            )
            return RewriteOutcome(
                original, original, False, model=model, took_ms=took_ms,
                reason=f"http {response.status_code}",
            )
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - any decode problem degrades
            logger.warning(
                "query rewrite response was not JSON",
                extra={"extra_fields": {"error": f"{type(exc).__name__}: {exc}"}},
            )
            return RewriteOutcome(
                original, original, False, model=model, took_ms=took_ms,
                reason="invalid json",
            )
    except Exception as exc:  # noqa: BLE001 - network errors degrade too
        took_ms = int((time.perf_counter() - started) * 1000)
        logger.warning(
            "query rewrite request failed",
            extra={"extra_fields": {"error": f"{type(exc).__name__}: {exc}"}},
        )
        return RewriteOutcome(
            original, original, False, model=model, took_ms=took_ms,
            reason=f"{type(exc).__name__}",
        )

    content = _extract_content(payload)
    if content is None:
        logger.warning("query rewrite response carried no content")
        return RewriteOutcome(
            original, original, False, model=model, took_ms=took_ms, reason="no choices"
        )

    rewritten = clean_rewrite(content)
    if not rewritten:
        logger.warning("query rewrite came back empty after cleaning")
        return RewriteOutcome(
            original, original, False, model=model, took_ms=took_ms, reason="empty rewrite"
        )
    if rewritten == original:
        return RewriteOutcome(
            original, original, False, model=model, took_ms=took_ms, reason="unchanged"
        )

    logger.info(
        "query rewritten",
        extra={
            "extra_fields": {
                "original_chars": len(original),
                "rewritten_chars": len(rewritten),
                "took_ms": took_ms,
            }
        },
    )
    return RewriteOutcome(original, rewritten, True, model=model, took_ms=took_ms)


__all__ = [
    "CJK_PATTERN",
    "DEFAULT_MAX_TOKENS",
    "SYSTEM_PROMPT",
    "RewriteOutcome",
    "build_rewrite_messages",
    "clean_rewrite",
    "needs_rewrite",
    "rewrite_query",
]
