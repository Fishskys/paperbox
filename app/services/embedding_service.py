"""Embedding service client (MVP-SPEC section 7).

The worker talks to the standalone embedding server (``EMBEDDING_URL``) which
exposes ``POST /embed`` with ``{"texts": [...]}`` and answers
``{"embeddings": [[...]], "dimension": 1024}``. Every vector is validated
against ``EMBEDDING_DIMENSION`` before it reaches PostgreSQL or OpenSearch.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

EMBED_PATH = "/embed"


class EmbeddingError(RuntimeError):
    """Raised when the embedding server cannot produce valid vectors."""


def _endpoint() -> str:
    base = settings.embedding_url.rstrip("/")
    return f"{base}{EMBED_PATH}"


def _post_batch(texts: Sequence[str], timeout: float) -> list[list[float]]:
    payload = {"texts": list(texts)}
    if settings.embedding_model:
        payload["model"] = settings.embedding_model
    with httpx.Client(timeout=timeout) as client:
        response = client.post(_endpoint(), json=payload)
    if response.status_code >= 400:
        raise EmbeddingError(
            f"embedding server returned HTTP {response.status_code}: {response.text[:300]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise EmbeddingError("embedding server returned a non-JSON body") from exc
    if not isinstance(data, dict):
        raise EmbeddingError("embedding server returned a non-object body")
    embeddings = data.get("embeddings")
    if embeddings is None:
        embeddings = data.get("data")
        if isinstance(embeddings, list):
            embeddings = [
                item.get("embedding") if isinstance(item, dict) else item
                for item in embeddings
            ]
    if not isinstance(embeddings, list):
        raise EmbeddingError("embedding response is missing the 'embeddings' field")
    vectors: list[list[float]] = []
    for index, vector in enumerate(embeddings):
        if not isinstance(vector, (list, tuple)) or not vector:
            raise EmbeddingError(f"embedding #{index} is not a non-empty vector")
        vectors.append([float(value) for value in vector])
    return vectors


def validate_dimension(vectors: Sequence[Sequence[float]], expected: int | None = None) -> None:
    """Raise :class:`EmbeddingError` unless every vector has ``expected`` dims."""
    dimension = expected or settings.embedding_dimension
    for index, vector in enumerate(vectors):
        if len(vector) != dimension:
            raise EmbeddingError(
                f"embedding #{index} has dimension {len(vector)}, expected {dimension}"
            )


def embed_texts(
    texts: Sequence[str],
    batch_size: int | None = None,
    retries: int | None = None,
) -> list[list[float]]:
    """Embed ``texts`` in batches, retrying transient failures with backoff.

    Args:
        texts: raw strings; empty strings are sent as-is because the number of
            returned vectors must line up one-to-one with the input.
        batch_size: number of texts per HTTP request (default 32 from spec,
            overridable through ``EMBEDDING_BATCH_SIZE``).
        retries: extra attempts per batch after the first failure (default 2).

    Returns:
        One vector per input text, already dimension-checked.
    """
    items = list(texts)
    if not items:
        return []

    if batch_size is None:
        batch_size = settings.embedding_batch_size or 32
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if retries is None:
        retries = settings.embedding_max_retries
    retries = max(0, int(retries))

    timeout = settings.embedding_timeout
    vectors: list[list[float]] = []
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                produced = _post_batch(batch, timeout)
                if len(produced) != len(batch):
                    raise EmbeddingError(
                        f"embedding server returned {len(produced)} vectors for "
                        f"{len(batch)} texts"
                    )
                validate_dimension(produced)
                vectors.extend(produced)
                break
            except Exception as exc:  # noqa: BLE001 - retried or re-raised below
                last_error = exc
                if attempt >= retries:
                    raise EmbeddingError(
                        f"embedding failed for batch at offset {start} after "
                        f"{attempt + 1} attempts: {exc}"
                    ) from exc
                delay = 0.5 * (2**attempt)
                logger.warning(
                    "embedding batch failed, retrying",
                    extra={
                        "extra_fields": {
                            "offset": start,
                            "attempt": attempt + 1,
                            "delay_s": delay,
                            "error": str(exc),
                        }
                    },
                )
                time.sleep(delay)
        if last_error is not None and len(vectors) < min(start + batch_size, len(items)):
            raise EmbeddingError(f"embedding batch at offset {start} produced no vectors")
    return vectors


def embed_text(text: str) -> list[float]:
    """Convenience wrapper for a single text (used by semantic search)."""
    return embed_texts([text])[0]


def embedding_metadata() -> dict[str, object]:
    """Model/dimension pairs recorded on papers and chunks."""
    return {
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
    }


__all__ = [
    "EMBED_PATH",
    "EmbeddingError",
    "embed_text",
    "embed_texts",
    "embedding_metadata",
    "validate_dimension",
]
