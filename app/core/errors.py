"""Failure attribution for ingestion jobs (SPEC-P1 section A2).

The worker used to store a single free-text ``error_message``; clients could not
tell a missing text layer from a broken embedding server without parsing prose.
:func:`classify_failure` maps any exception raised by the pipeline onto one of a
fixed set of codes, and the worker persists that code next to the message.

Codes are intentionally coarse and stable -- they are an API surface, so they
must not change without a migration:

``NO_TEXT_LAYER`` / ``ENCRYPTED_PDF`` / ``CORRUPT_PDF`` / ``DOWNLOAD_FAILED`` /
``OVERSIZED`` / ``UNSUPPORTED_TYPE`` / ``DUPLICATE_FINGERPRINT`` /
``PARSE_BACKEND_UNAVAILABLE`` / ``PARSE_FAILED`` / ``EMBEDDING_FAILED`` /
``INDEX_FAILED`` / ``STORAGE_FAILED`` / ``INTERRUPTED`` / ``INTERNAL``.

``INTERRUPTED`` is raised by the queue's startup recovery (2026-09-19): a job
that was mid-pipeline when the process restarted is marked failed so the client
sees it, and ``POST /api/jobs/{id}/retry`` re-drives it.

The two ``PARSE_*`` codes come from the docling backend (2026-09-29).  The
pipeline itself does **not** fail when docling is down -- it degrades to pypdf
and records ``degraded_reason`` + a ``paper_degradations`` row (AGENTS.md
section 3.10/3.11).  These codes exist for the paths that demand the docling
backend without a fallback: probes, and any future "strict parse" caller.  A
job that fails this way is worth retrying (the service may have been
restarting), which is exactly what ``POST /api/jobs/{id}/retry`` is for.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.exc import IntegrityError

from app.parsing.docling_client import DoclingFailed, DoclingUnavailable
from app.parsing.pdf import PdfParseError
from app.search.opensearch import SearchIndexError
from app.services.embedding_service import EmbeddingError
from app.services.ingestion_service import (
    IngestionError,
    LocalSourceUnavailable,
    UnsupportedSource,
)
from app.services.object_storage import ObjectStorageError

#: Every code the API may return; kept as a tuple so tests can assert coverage.
FAILURE_CODES: tuple[str, ...] = (
    "NO_TEXT_LAYER",
    "ENCRYPTED_PDF",
    "CORRUPT_PDF",
    "DOWNLOAD_FAILED",
    "OVERSIZED",
    "UNSUPPORTED_TYPE",
    "DUPLICATE_FINGERPRINT",
    "PARSE_BACKEND_UNAVAILABLE",
    "PARSE_FAILED",
    "EMBEDDING_FAILED",
    "INDEX_FAILED",
    "STORAGE_FAILED",
    "INTERRUPTED",
    "INTERNAL",
)

#: Extra hint appended to NO_TEXT_LAYER messages (the MVP has no OCR fallback).
NO_TEXT_LAYER_HINT = "the PDF has no text layer, OCR is required (not supported yet)"

_KEYWORDS_ENCRYPTED = ("encrypted", "password")
_KEYWORDS_NO_TEXT = (
    "no chunks",
    "no text",
    "no extractable text",
    "empty text",
    "no text layer",
)
_KEYWORDS_OVERSIZED = ("too large", "exceeds", "oversized", "size limit")
_KEYWORDS_UNSUPPORTED = (
    "only pdf",
    "unsupported",
    "not a pdf",
    "empty payload",
    "missing",
    "invalid source",
    "must be an http",
)
_KEYWORDS_DUPLICATE = (
    "duplicate key",
    "unique violation",
    "uniqueviolation",
    "uq_papers_fingerprint",
    "already exists",
)
_KEYWORDS_DOWNLOAD = ("download failed", "download", "httpstatus", "timed out", "timeout")


@dataclass(frozen=True)
class Failure:
    """One classified failure: a stable ``code`` plus a readable ``message``."""

    code: str
    message: str


def _describe(exc: BaseException) -> str:
    """``TypeName: original text`` -- keeps the raw cause traceable."""
    text = str(exc).strip() or exc.__class__.__name__
    return f"{type(exc).__name__}: {text}"


def _mentions(exc: BaseException, keywords: tuple[str, ...]) -> bool:
    haystack = f"{type(exc).__name__} {exc}".lower()
    return any(keyword in haystack for keyword in keywords)


def _with_cause(exc: BaseException) -> str:
    """Message text including the ``__cause__`` chain (``raise ... from ...``)."""
    parts = [_describe(exc)]
    cause = exc.__cause__
    seen = 0
    while cause is not None and seen < 3:
        parts.append(_describe(cause))
        cause = cause.__cause__
        seen += 1
    return " <- ".join(parts)


def classify_failure(exc: BaseException) -> Failure:
    """Map a pipeline exception onto ``(code, message)``.

    Order matters: the more specific signals (encryption, size, type, duplicate
    keys) are checked before the broad categories (parsing, download, storage),
    so a ``UnsupportedSource`` about size never falls through to
    ``UNSUPPORTED_TYPE``.
    """
    detail = _with_cause(exc)

    if isinstance(exc, IntegrityError) or _mentions(exc, _KEYWORDS_DUPLICATE):
        return Failure(
            "DUPLICATE_FINGERPRINT",
            f"DUPLICATE_FINGERPRINT: another live paper already claims this fingerprint ({detail})",
        )

    if isinstance(exc, LocalSourceUnavailable):
        # A server-side path (``/ingest/dir``, archive extraction) disappeared
        # before a pipeline slot freed up: the payload could not be obtained,
        # which is the same failure a dead URL produces.
        return Failure("DOWNLOAD_FAILED", f"DOWNLOAD_FAILED: {detail}")

    if _is_oversized(exc):
        return Failure("OVERSIZED", f"OVERSIZED: {detail}")

    if _is_unsupported_type(exc):
        return Failure("UNSUPPORTED_TYPE", f"UNSUPPORTED_TYPE: {detail}")

    if _is_no_text_layer(exc):
        return Failure("NO_TEXT_LAYER", f"NO_TEXT_LAYER: {detail}; {NO_TEXT_LAYER_HINT}")

    if isinstance(exc, DoclingUnavailable):
        return Failure(
            "PARSE_BACKEND_UNAVAILABLE",
            f"PARSE_BACKEND_UNAVAILABLE: the docling backend was unreachable "
            f"and no fallback was allowed ({detail})",
        )

    if isinstance(exc, DoclingFailed):
        return Failure(
            "PARSE_FAILED",
            f"PARSE_FAILED: the docling backend rejected the document ({detail})",
        )

    if isinstance(exc, PdfParseError):
        if _mentions(exc, _KEYWORDS_ENCRYPTED):
            return Failure(
                "ENCRYPTED_PDF",
                f"ENCRYPTED_PDF: the PDF is password protected ({detail})",
            )
        return Failure("CORRUPT_PDF", f"CORRUPT_PDF: {detail}")

    if isinstance(exc, EmbeddingError):
        return Failure("EMBEDDING_FAILED", f"EMBEDDING_FAILED: {detail}")

    if isinstance(exc, SearchIndexError):
        return Failure("INDEX_FAILED", f"INDEX_FAILED: {detail}")

    if isinstance(exc, ObjectStorageError):
        return Failure("STORAGE_FAILED", f"STORAGE_FAILED: {detail}")

    if isinstance(exc, httpx.HTTPError):
        return Failure("DOWNLOAD_FAILED", f"DOWNLOAD_FAILED: {detail}")

    if isinstance(exc, UnsupportedSource):
        return Failure("UNSUPPORTED_TYPE", f"UNSUPPORTED_TYPE: {detail}")

    if isinstance(exc, IngestionError) and _mentions(exc, _KEYWORDS_DOWNLOAD):
        return Failure("DOWNLOAD_FAILED", f"DOWNLOAD_FAILED: {detail}")

    return Failure("INTERNAL", f"INTERNAL: {detail}")


def _is_oversized(exc: BaseException) -> bool:
    return isinstance(exc, UnsupportedSource) and _mentions(exc, _KEYWORDS_OVERSIZED)


def _is_unsupported_type(exc: BaseException) -> bool:
    if not isinstance(exc, (UnsupportedSource, IngestionError)):
        return False
    if _mentions(exc, _KEYWORDS_OVERSIZED):
        return False
    return _mentions(exc, _KEYWORDS_UNSUPPORTED) or isinstance(exc, UnsupportedSource)


def _is_no_text_layer(exc: BaseException) -> bool:
    if isinstance(exc, PdfParseError):
        return False
    return isinstance(exc, IngestionError) and _mentions(exc, _KEYWORDS_NO_TEXT)


__all__ = [
    "FAILURE_CODES",
    "NO_TEXT_LAYER_HINT",
    "Failure",
    "classify_failure",
]
