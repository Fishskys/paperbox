"""Pick the parser backend and degrade with a reason (plan §2 T6).

One entry point for the ingestion pipeline: ``parse_pdf()`` prefers docling and
falls back to pypdf when the docling call fails, always recording *why* in
``ParseBundle.degraded_reason`` and in the logs.  The module is deliberately
pure: no DB, no MinIO, no task state -- caching is T7, a layer above.

Backend resolution order: explicit ``backend`` argument > ``PARSER_BACKEND``.
The docling call is guarded by a module-level semaphore sized
``PARSER_CONCURRENCY`` (default 1): docling is a shared remote service and a
burst of parses would pile conversions onto it.

Error mapping: ``DoclingError`` subclasses are caught here and turned into a
pypdf fallback.  ``PdfParseError`` (the file itself unreadable) is *not* caught
-- it propagates to the caller, because no backend can parse a bad PDF.
Mapping ``DoclingUnavailable`` to the ``PARSE_BACKEND_UNAVAILABLE`` ingest error
belongs to the task layer (T9), not here.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from app.core.config import PARSER_BACKENDS, settings
from app.core.logging import get_logger
from app.parsing import markdown as markdown_dialect
from app.parsing.docling_client import DoclingError, DoclingResult
from app.parsing.markdown import ParseBundle
from app.parsing.pdf import extract_pages

logger = get_logger(__name__)

#: docling is one remote server; cap concurrent conversions.
_docling_semaphore = threading.Semaphore(max(1, int(settings.parser_concurrency)))

DOCLING_FALLBACK_PREFIX = "docling unavailable"


def parse_pdf(
    data: bytes,
    *,
    filename: str,
    backend: str | None = None,
    converter: Callable[..., DoclingResult] | None = None,
    page_range: str | None = None,
) -> ParseBundle:
    """Parse ``data`` with the chosen backend, degrading to pypdf on failure.

    Args:
        data: the PDF bytes.
        filename: only used for logs and error messages.
        backend: explicit ``"docling"`` / ``"pypdf"``; defaults to ``PARSER_BACKEND``.
        converter: test seam replacing :func:`docling_client.convert_markdown`.
            The docling path still runs under the concurrency semaphore.
        page_range: passed through to docling (e.g. ``"1-3"``); None = whole file.

    Returns:
        A :class:`ParseBundle` whose ``degraded_reason`` says why the result is
        thinner than docling alone would produce.  ``None`` only when docling
        parsed cleanly.

    Raises:
        ValueError: explicit ``backend`` is not a known backend.
        PdfParseError: the bytes are not a readable PDF (no backend can save it).
    """
    resolved = _resolve_backend(backend)
    if resolved == "docling":
        try:
            return _parse_with_docling(
                data, filename=filename, converter=converter, page_range=page_range
            )
        except DoclingError as exc:
            reason = f"{DOCLING_FALLBACK_PREFIX} {exc.__class__.__name__}: {exc}"
            logger.warning(
                "parser backend fell back to pypdf",
                extra={"extra_fields": {"filename": filename, "reason": reason}},
            )
            bundle = _parse_with_pypdf(data)
            bundle.degraded_reason = _merge_reasons(reason, bundle.degraded_reason)
            return bundle
    return _parse_with_pypdf(data)


def _resolve_backend(backend: str | None) -> str:
    resolved = (backend or settings.parser_backend).strip().lower()
    if backend is not None and resolved not in PARSER_BACKENDS:
        raise ValueError(
            f"unknown parser backend {backend!r}; choose from {sorted(PARSER_BACKENDS)}"
        )
    return resolved


def _parse_with_docling(
    data: bytes,
    *,
    filename: str,
    converter: Callable[..., DoclingResult] | None,
    page_range: str | None,
) -> ParseBundle:
    convert = converter or _docling_convert
    started = time.perf_counter()
    with _docling_semaphore:
        result = convert(data, filename=filename, page_range=page_range)
    wall_s = round(time.perf_counter() - started, 4)

    prepared = markdown_dialect.prepare_docling_markdown(result.markdown)
    marker_count, spans = markdown_dialect.page_spans_from_markdown(prepared)
    page_count = max(marker_count or result.page_count, 0)

    timings: dict[str, float] = {"docling_s": wall_s}
    if result.processing_time is not None:
        timings["docling_processing_s"] = round(result.processing_time, 4)
    timings["total_s"] = round(time.perf_counter() - started, 4)

    return ParseBundle(
        markdown=prepared,
        page_count=page_count,
        spans=spans,
        backend="docling",
        parser_version=result.parser_version,
        degraded_reason=result.degraded_reason,
        timings=timings,
    )


def _docling_convert(data: bytes, *, filename: str, page_range: str | None) -> DoclingResult:
    """The real converter, behind the seam so tests can inject fakes."""
    from app.parsing.docling_client import convert_markdown

    return convert_markdown(data, filename=filename, page_range=page_range)


def _parse_with_pypdf(data: bytes) -> ParseBundle:
    started = time.perf_counter()
    pages = extract_pages(data)
    bundle = markdown_dialect.render_markdown(
        pages,
        pdf_bytes=data,
        backend="pypdf",
        timings={"extract_s": round(time.perf_counter() - started, 4)},
    )
    bundle.timings["total_s"] = round(time.perf_counter() - started, 4)
    return bundle


def _merge_reasons(prefix: str, existing: str | None) -> str:
    if existing:
        return f"{prefix}; {existing}"
    return prefix