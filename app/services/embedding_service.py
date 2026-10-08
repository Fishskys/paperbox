"""Embedding service client (MVP-SPEC section 7).

The worker talks to the standalone embedding server (``EMBEDDING_URL``) which
exposes ``POST /embed`` with ``{"texts": [...]}`` and answers
``{"embeddings": [[...]], "dimension": <actual width>}``. Every vector is
validated against ``EMBEDDING_DIMENSION`` before it reaches PostgreSQL or
OpenSearch.

This module also owns the startup three-way dimension check (``2026-10-07``):
``check_dimension_consistency`` compares the container's measured output width
against ``EMBEDDING_DIMENSION`` and against the live index mapping, so a
misconfigured deployment fails at startup instead of at the first embed batch.
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


# --------------------------------------------------------------------------- #
# startup three-way dimension check (2026-10-07)
#
# The dimension used to be stated in three places that never talked: the
# container's model (its real output width), ``EMBEDDING_DIMENSION`` (what the
# app validates against) and the live index mapping (what ``knn_vector`` was
# built for). A mismatch surfaced only as the first failed embed batch, or --
# worse -- as a silently degraded kNN search. The check runs once at startup
# (``app.main`` lifespan): a *verified* mismatch stops the process, an
# unreachable piece only warns (the app may legitimately start before its
# containers, and ``validate_dimension`` still guards every write).
# --------------------------------------------------------------------------- #

#: HTTP timeout of the one ``/info`` probe against the embedding container.
STARTUP_PROBE_TIMEOUT = 5.0


def container_dimension(timeout: float = STARTUP_PROBE_TIMEOUT) -> int | None:
    """The embedding container's *measured* dimension, or ``None`` if unknown.

    Reads ``GET /info``, whose ``dimension`` is what the loaded model actually
    produces (it is null until the model is lazily loaded). Every failure --
    connection refused, timeout, non-200, unexpected body -- is a warning and
    ``None``: "cannot verify", never a mismatch.
    """
    url = f"{settings.embedding_url.rstrip('/')}/info"
    try:
        response = httpx.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - unreachable is a warning, not a crash
        logger.warning(
            "embedding container not reachable for the dimension check (%s): %s",
            url,
            exc,
        )
        return None
    if response.status_code != 200:
        logger.warning(
            "embedding container answered HTTP %s to the dimension check: %s",
            response.status_code,
            url,
        )
        return None
    try:
        dimension = response.json().get("dimension")
    except ValueError:
        logger.warning("embedding container returned a non-JSON /info body")
        return None
    if not isinstance(dimension, int):
        logger.warning(
            "embedding container has no measured dimension yet (model not loaded?)"
        )
        return None
    return dimension


def index_dimension(index: str | None = None) -> int | None:
    """The ``knn_vector`` dimension the live physical index was built for.

    ``None`` when the index does not exist yet (``ensure_index`` will create it
    from ``EMBEDDING_DIMENSION``), or when OpenSearch cannot be reached, or the
    mapping carries no ``embedding`` field -- all "cannot verify".
    """
    from app.search import opensearch  # local: keeps the client import lazy

    name = index or opensearch.INDEX
    try:
        client = opensearch.get_client()
        if not opensearch.index_exists(client, name):
            return None
        mapping = client.indices.get_mapping(index=name)
    except Exception as exc:  # noqa: BLE001 - unreachable is a warning, not a crash
        logger.warning(
            "OpenSearch not reachable for the dimension check (%s): %s",
            settings.opensearch_url,
            exc,
        )
        return None
    return opensearch.mapping_embedding_dimension(mapping)


def check_dimension_consistency() -> dict[str, int | None]:
    """Compare the three dimension statements; raise on a verified mismatch.

    Returns the small report that gets logged. Raises :class:`RuntimeError`
    only on a mismatch that was actually measured -- a deployment whose
    ``EMBEDDING_DIMENSION`` disagrees with its model or with its index cannot
    embed or search correctly, so it must not come up at all.
    """
    expected = settings.embedding_dimension
    container = container_dimension()
    live = index_dimension()
    if container is not None and container != expected:
        raise RuntimeError(
            f"EMBEDDING_DIMENSION={expected} but the embedding model "
            f"{settings.embedding_model!r} produces {container}-dim vectors. "
            "Fix EMBEDDING_DIMENSION (or deploy the model you meant), and note "
            "that every existing chunk was embedded with the old model: after "
            "a model switch the whole library must be re-embedded "
            "(scripts/reindex.py), whatever the dimension is."
        )
    if live is not None and live != expected:
        raise RuntimeError(
            f"EMBEDDING_DIMENSION={expected} but the live index "
            f"{settings.opensearch_index!r} was built for {live}-dim vectors. "
            "A knn_vector dimension cannot be changed in place: point "
            "OPENSEARCH_INDEX at a new index and re-embed every paper "
            "(scripts/reindex.py)."
        )
    logger.info(
        "embedding dimension check passed",
        extra={
            "extra_fields": {
                "expected": expected,
                "container": container,
                "index": live,
            }
        },
    )
    return {"expected": expected, "container": container, "index": live}


__all__ = [
    "EMBED_PATH",
    "EmbeddingError",
    "check_dimension_consistency",
    "container_dimension",
    "embed_text",
    "embed_texts",
    "embedding_metadata",
    "index_dimension",
    "validate_dimension",
]
