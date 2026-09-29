"""Pick the parser backend and degrade with a reason (plan §2 T6).

One entry point for the ingestion pipeline: ``parse_pdf()`` prefers docling and
falls back to pypdf when the docling call fails, always recording *why* in
``ParseBundle.degraded_reason`` and in the logs.  ``parse_pdf()`` itself stays
pure: no DB, no MinIO, no task state.  T7.1 adds ``parse_paper_file()`` on top,
which replays a previously stored parse from object storage (markdown + meta +
docling's own JSON) instead of paying for another conversion.

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

import json
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from app.core.config import PARSER_BACKENDS, settings
from app.core.logging import get_logger
import pypdf
from app.parsing import markdown as markdown_dialect
from app.parsing.docling_client import (
    FORMULA_FALLBACK_REASON,
    DoclingError,
    DoclingResult,
    version_from_server,
)
from app.parsing.markdown import ParseBundle
from app.parsing.pdf import extract_pages
from app.services.object_storage import ObjectNotFound, ObjectStorageError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.degradation_service import DegradeSink

logger = get_logger(__name__)

#: docling is one remote server; cap concurrent conversions.
_docling_semaphore = threading.Semaphore(max(1, int(settings.parser_concurrency)))

DOCLING_FALLBACK_PREFIX = "docling unavailable"
DOCLING_BACKEND = "docling"

#: Bumped whenever the *meaning* of a stored artifact changes (markdown dialect,
#: normalization, span computation): an entry written under an older value is
#: re-parsed instead of replayed.
PARSE_CACHE_VERSION = 1
PARSE_MARKDOWN_ARTIFACT = "document.md"
PARSE_JSON_ARTIFACT = "document.json"
PARSE_META_ARTIFACT = "parse-meta.json"

# --- degradation ledger (plan T7.3) --------------------------------------- #
#: Stage vocabulary for this module; ``degradation_service.record`` validates it.
DEGRADE_STAGE = "parsing"
#: One code per cause a parse is thinner than docling alone would produce.
CODE_DOCLING_UNAVAILABLE = "docling_unavailable"
CODE_FORMULAS_AS_TEXT = "formulas_as_text"
CODE_TABLE_STRUCTURE = "table_structure_lost"
CODE_READING_ORDER = "reading_order_unverified"
#: ``PARSER_MAX_PAGES`` deliberately cut the document (cheap probes on huge papers).
CODE_PAGES_TRUNCATED = "pagination_truncated"
#: Fallback so an unrecognised reason is never lost silently.
CODE_PARSE_DEGRADED = "parse_degraded"

#: ``degraded_reason`` fragment written when ``PARSER_MAX_PAGES`` truncated.
PAGES_LIMIT_PREFIX = "pages="

#: ``degraded_reason`` fragment -> ledger code. The fragments are produced by
#: ``app.parsing.markdown``; the parse layer still carries them as one joined
#: string (T8 can report structured reasons directly, at which point this table
#: becomes the compatibility shim for old cached/queued bundles).
_REASON_CODES: tuple[tuple[str, str], ...] = (
    (FORMULA_FALLBACK_REASON, CODE_FORMULAS_AS_TEXT),
    (markdown_dialect.DEGRADED_NO_FORMULA, CODE_FORMULAS_AS_TEXT),
    (markdown_dialect.DEGRADED_TABLE, CODE_TABLE_STRUCTURE),
    (markdown_dialect.DEGRADED_ORDER, CODE_READING_ORDER),
    (DOCLING_FALLBACK_PREFIX, CODE_DOCLING_UNAVAILABLE),
    (PAGES_LIMIT_PREFIX, CODE_PAGES_TRUNCATED),
)


def degradation_codes(reason: str) -> list[str]:
    """Ledger codes for a ``degraded_reason`` string (never empty).

    ``parse_degraded`` is the fallback: a new reason still produces a row
    instead of disappearing.
    """
    text = (reason or "").lower()
    codes = [code for fragment, code in _REASON_CODES if fragment.lower() in text]
    return codes or [CODE_PARSE_DEGRADED]


def _report_degradation(bundle: ParseBundle, on_degrade: "DegradeSink | None") -> None:
    """Put every cause in ``bundle.degraded_reason`` into the ledger."""
    if on_degrade is None or not bundle.degraded_reason:
        return
    detail = {
        "backend": bundle.backend,
        "parser_version": bundle.parser_version,
        "reason": bundle.degraded_reason,
    }
    for code in degradation_codes(bundle.degraded_reason):
        on_degrade(DEGRADE_STAGE, code, detail)


class ArtifactStore(Protocol):
    """The slice of ``object_storage`` the cache needs (faked in tests)."""

    def download_bytes(self, object_key: str, bucket: str | None = None) -> bytes: ...

    def upload_bytes(
        self,
        object_key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        bucket: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> object: ...


def parse_pdf(
    data: bytes,
    *,
    filename: str,
    backend: str | None = None,
    converter: Callable[..., DoclingResult] | None = None,
    page_range: str | None = None,
    on_degrade: "DegradeSink | None" = None,
) -> ParseBundle:
    """Parse ``data`` with the chosen backend, degrading to pypdf on failure.

    Args:
        data: the PDF bytes.
        filename: only used for logs and error messages.
        backend: explicit ``"docling"`` / ``"pypdf"``; defaults to ``PARSER_BACKEND``.
        converter: test seam replacing :func:`docling_client.convert_markdown`.
            The docling path still runs under the concurrency semaphore.
        page_range: passed through to docling (e.g. ``"1-3"``); None = whole file.
        on_degrade: optional degradation sink (plan T7.3), called as
            ``(stage, code, detail)`` once per cause in ``degraded_reason``.

    Returns:
        A :class:`ParseBundle` whose ``degraded_reason`` says why the result is
        thinner than docling alone would produce.  ``None`` only when docling
        parsed cleanly.

    Raises:
        ValueError: explicit ``backend`` is not a known backend.
        PdfParseError: the bytes are not a readable PDF (no backend can save it).
    """
    resolved = _resolve_backend(backend)
    limit_range = _max_pages_range()
    effective_range = page_range if page_range is not None else limit_range
    if resolved == "docling":
        try:
            bundle = _parse_with_docling(
                data, filename=filename, converter=converter, page_range=effective_range
            )
            if limit_range is not None and page_range is None:
                # PARSER_MAX_PAGES cut the document: the result is thinner on
                # purpose, and that must be visible in the artifact + the ledger.
                bundle.degraded_reason = _merge_reasons(
                    f"{PAGES_LIMIT_PREFIX}{limit_range}", bundle.degraded_reason
                )
            _report_degradation(bundle, on_degrade)
            return bundle
        except DoclingError as exc:
            reason = f"{DOCLING_FALLBACK_PREFIX} {exc.__class__.__name__}: {exc}"
            logger.warning(
                "parser backend fell back to pypdf",
                extra={"extra_fields": {"filename": filename, "reason": reason}},
            )
            bundle = _parse_with_pypdf(data)
            bundle.degraded_reason = _merge_reasons(reason, bundle.degraded_reason)
            _report_degradation(bundle, on_degrade)
            return bundle
    bundle = _parse_with_pypdf(data)
    _report_degradation(bundle, on_degrade)
    return bundle


def _max_pages_range() -> str | None:
    """``PARSER_MAX_PAGES`` as a docling ``page_range``; ``None`` = whole file.

    Only docling is limited: the pypdf fallback costs about a second, and
    slicing it would silently change what the fallback is meant to reproduce.
    """
    limit = int(settings.parser_max_pages or 0)
    return f"1-{limit}" if limit > 0 else None


def _partial_parse(page_range: str | None) -> bool:
    """True when this call parses less than the whole document."""
    return page_range is not None or _max_pages_range() is not None


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
        raw_json=result.raw_json,
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
        # Same shape as docling's string ("docling 2.130.0"): the stamp has to be
        # able to say *which* pypdf produced these chunks.
        parser_version=f"pypdf {pypdf.__version__}",
        timings={"extract_s": round(time.perf_counter() - started, 4)},
    )
    bundle.timings["total_s"] = round(time.perf_counter() - started, 4)
    return bundle


def _merge_reasons(prefix: str, existing: str | None) -> str:
    if existing:
        return f"{prefix}; {existing}"
    return prefix


# --------------------------------------------------------------------------- #
# parse-artifact cache (plan section 2 T7.1)
# --------------------------------------------------------------------------- #
def parse_paper_file(
    paper_id: str,
    data: bytes,
    *,
    filename: str,
    backend: str | None = None,
    converter: Callable[..., DoclingResult] | None = None,
    page_range: str | None = None,
    store: ArtifactStore | None = None,
    cache: bool | None = None,
    version_probe: Callable[[], str | None] | None = None,
    now: datetime | None = None,
    on_degrade: "DegradeSink | None" = None,
) -> ParseBundle:
    """Parse ``data`` for ``paper_id``, replaying stored artifacts if possible.

    Artifacts live under ``papers/<paper_id>/extracted/parsed/``: the markdown
    under ``<backend>/document.md``, docling's own JSON under
    ``docling/document.json`` and the bookkeeping under ``parse-meta.json``.
    They are removed with the paper (``delete_prefix``), and they make a parse
    replayable: the second ingestion of the same file costs a download instead
    of a conversion, and a parse can be inspected or A/B-ed later.

    A stored entry is replayed only when it is as good as a fresh parse:

    * its ``cache_version`` matches :data:`PARSE_CACHE_VERSION`,
    * it was written by the backend now being asked for,
    * it is not a degradation (a docling outage must not become sticky),
    * the markdown object is still there,
    * and, for docling, the server still reports the version it reported then
      (probed with ``GET /version``, which is milliseconds against a conversion
      that can take minutes).

    A **partial** parse (explicit ``page_range``, or ``PARSER_MAX_PAGES`` > 0) is
    never read from nor written to the cache -- replaying a truncated document
    as if it were complete would be worse than a slow parse.

    Args:
        paper_id: owning paper; also the object-key prefix.
        data: the PDF bytes, read only when the cache cannot answer.
        filename: for logs and the artifact metadata.
        backend: explicit backend, defaults to ``PARSER_BACKEND``.
        converter: test seam passed through to :func:`parse_pdf`.
        page_range: passed through to docling.
        store: storage seam (:class:`ArtifactStore`); defaults to
            ``object_storage``. Tests inject a dict-backed fake.
        cache: override ``PARSER_CACHE`` for this call.
        version_probe: test seam for the docling version check.
        now: injectable clock for the metadata timestamp.
        on_degrade: degradation sink (plan T7.3) handed to :func:`parse_pdf`;
            a replayed cache entry reports nothing because a degraded parse is
            never replayed in the first place.

    Returns:
        The parsed bundle; ``cache_hit`` is ``True`` when it came from storage.
    """
    resolved = _resolve_backend(backend)
    storage = store if store is not None else _default_store()
    enabled = settings.parser_cache if cache is None else bool(cache)
    if enabled and _partial_parse(page_range):
        # A partial parse is a probe, not a product: caching it would let the
        # next full parse replay a truncated document as if it were complete.
        logger.info(
            "partial parse: artifacts are neither read nor written",
            extra={
                "extra_fields": {
                    "reason": "page_range" if page_range else "PARSER_MAX_PAGES",
                    "paper_id": paper_id,
                }
            },
        )
        enabled = False

    if enabled:
        cached = _load_cached_bundle(
            paper_id,
            backend=resolved,
            storage=storage,
            version_probe=version_probe,
        )
        if cached is not None:
            return cached

    bundle = parse_pdf(
        data,
        filename=filename,
        backend=resolved,
        converter=converter,
        page_range=page_range,
        on_degrade=on_degrade,
    )
    if enabled:
        _store_bundle(
            paper_id,
            bundle,
            storage=storage,
            filename=filename,
            page_range=page_range,
            now=now,
        )
    return bundle


def _default_store() -> Any:
    """The real object storage, imported lazily so tests never dial MinIO."""
    from app.services import object_storage

    return object_storage


def _artifact_key(paper_id: str, name: str, *, backend: str | None = None) -> str:
    from app.services import object_storage

    prefix = "parsed" if backend is None else f"parsed/{backend}"
    return object_storage.build_extracted_key(paper_id, f"{prefix}/{name}")


def _key_belongs_to(paper_id: str, object_key: str) -> bool:
    """Guard against a meta file pointing at some other paper's objects."""
    prefix = _artifact_key(paper_id, "")
    return isinstance(object_key, str) and object_key.startswith(prefix)


def _docling_version_probe() -> str | None:
    """``GET /version``; ``None`` when it cannot be read (never raises)."""
    return version_from_server()


def _load_cached_bundle(
    paper_id: str,
    *,
    backend: str,
    storage: ArtifactStore,
    version_probe: Callable[[], str | None] | None,
) -> ParseBundle | None:
    started = time.perf_counter()
    meta_key = _artifact_key(paper_id, PARSE_META_ARTIFACT)
    try:
        raw_meta = storage.download_bytes(meta_key)
    except ObjectNotFound:
        return None
    except ObjectStorageError as exc:
        _log_cache_problem("parse cache unreadable, parsing again", meta_key, exc)
        return None

    meta = _decode_meta(raw_meta, meta_key)
    if meta is None:
        return None
    if meta.get("cache_version") != PARSE_CACHE_VERSION:
        logger.info(
            "parse cache is stale: cache version changed",
            extra={
                "extra_fields": {
                    "paper_id": paper_id,
                    "cached": meta.get("cache_version"),
                    "current": PARSE_CACHE_VERSION,
                }
            },
        )
        return None
    if meta.get("backend") != backend:
        return None
    if meta.get("degraded_reason"):
        # A degraded parse is kept for inspection but never replayed: whatever
        # broke the backend may be fixed by the next attempt.
        logger.info(
            "parse cache holds a degraded parse, parsing again",
            extra={
                "extra_fields": {
                    "paper_id": paper_id,
                    "reason": meta.get("degraded_reason"),
                }
            },
        )
        return None

    artifacts = meta.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    markdown_key = artifacts.get("markdown") or _artifact_key(
        paper_id, PARSE_MARKDOWN_ARTIFACT, backend=backend
    )
    if not _key_belongs_to(paper_id, markdown_key):
        return None

    if backend == DOCLING_BACKEND:
        probe = version_probe or _docling_version_probe
        cached_version = str(meta.get("parser_version") or "")
        try:
            probed = probe()
        except Exception as exc:  # noqa: BLE001 - a probe must never break a replay
            _log_cache_problem("docling version probe failed", meta_key, exc)
            probed = None
        if probed and cached_version and probed != cached_version:
            logger.info(
                "parse cache is stale: parser version changed",
                extra={
                    "extra_fields": {
                        "paper_id": paper_id,
                        "cached": cached_version,
                        "current": probed,
                    }
                },
            )
            return None

    try:
        markdown = storage.download_bytes(markdown_key).decode("utf-8")
    except ObjectNotFound:
        return None
    except (ObjectStorageError, UnicodeDecodeError) as exc:
        _log_cache_problem("parse cache unreadable, parsing again", markdown_key, exc)
        return None

    marker_count, spans = markdown_dialect.page_spans_from_markdown(markdown)
    page_count = _positive_int(meta.get("page_count")) or max(marker_count, 0)
    timings = _timings(meta.get("timings"))
    timings["cache_load_s"] = round(time.perf_counter() - started, 4)

    logger.info(
        "parse cache hit",
        extra={
            "extra_fields": {
                "paper_id": paper_id,
                "backend": backend,
                "chars": len(markdown),
                "cache_load_s": timings["cache_load_s"],
            }
        },
    )
    return ParseBundle(
        markdown=markdown,
        page_count=page_count,
        spans=spans,
        backend=backend,
        parser_version=str(meta.get("parser_version") or ""),
        degraded_reason=None,
        timings=timings,
        headings=_headings(meta.get("headings")),
        raw_json=_cached_json(paper_id, artifacts, storage),
        cache_hit=True,
    )


def _store_bundle(
    paper_id: str,
    bundle: ParseBundle,
    *,
    storage: ArtifactStore,
    filename: str,
    page_range: str | None,
    now: datetime | None,
) -> None:
    """Write the parse artifacts; the meta file goes last, so a partial write is a miss."""
    markdown_key = _artifact_key(paper_id, PARSE_MARKDOWN_ARTIFACT, backend=bundle.backend)
    json_key = (
        _artifact_key(paper_id, PARSE_JSON_ARTIFACT, backend=DOCLING_BACKEND)
        if bundle.raw_json is not None
        else None
    )
    meta = {
        "cache_version": PARSE_CACHE_VERSION,
        "backend": bundle.backend,
        "parser_version": bundle.parser_version,
        "page_count": bundle.page_count,
        "degraded_reason": bundle.degraded_reason,
        "filename": filename,
        "page_range": page_range,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "timings": dict(bundle.timings),
        "headings": [list(item) for item in bundle.headings],
        "artifacts": {"markdown": markdown_key, "json": json_key},
    }
    try:
        storage.upload_bytes(
            markdown_key,
            bundle.markdown.encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )
        if json_key is not None:
            storage.upload_bytes(
                json_key,
                json.dumps(bundle.raw_json, ensure_ascii=False).encode("utf-8"),
                content_type="application/json",
            )
        storage.upload_bytes(
            _artifact_key(paper_id, PARSE_META_ARTIFACT),
            json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json",
        )
    except ObjectStorageError as exc:
        # The bundle in hand is still good: never fail an ingestion because the
        # cache could not be written.
        _log_cache_problem(
            "could not write the parse artifacts", _artifact_key(paper_id, "parsed"),
            exc,
        )
        return
    logger.info(
        "parse artifacts stored",
        extra={
            "extra_fields": {
                "paper_id": paper_id,
                "backend": bundle.backend,
                "markdown_key": markdown_key,
                "json_key": json_key,
                "degraded_reason": bundle.degraded_reason,
            }
        },
    )


def _decode_meta(raw: bytes, meta_key: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _log_cache_problem("parse cache meta is unreadable, parsing again", meta_key, exc)
        return None
    if not isinstance(decoded, dict):
        _log_cache_problem(
            "parse cache meta is not an object, parsing again",
            meta_key,
            TypeError("not an object"),
        )
        return None
    return decoded


def _cached_json(
    paper_id: str, artifacts: dict[str, Any], storage: ArtifactStore
) -> dict[str, Any] | None:
    json_key = artifacts.get("json")
    if not json_key or not _key_belongs_to(paper_id, json_key):
        return None
    try:
        decoded = json.loads(storage.download_bytes(json_key).decode("utf-8"))
    except (ObjectNotFound, ObjectStorageError, UnicodeDecodeError, json.JSONDecodeError):
        # The markdown is what the pipeline consumes; a missing or broken JSON
        # artifact must not turn a replay into a parse.
        return None
    return decoded if isinstance(decoded, dict) else None


def _timings(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): float(value)
        for name, value in raw.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _headings(raw: Any) -> list[tuple[int, str]]:
    if not isinstance(raw, list):
        return []
    headings: list[tuple[int, str]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            level = _positive_int(item[0])
            if level:
                headings.append((level, str(item[1])))
    return headings


def _positive_int(value: Any) -> int:
    return int(value) if isinstance(value, int) and value > 0 else 0


def _log_cache_problem(message: str, object_key: str, exc: BaseException) -> None:
    logger.warning(
        message,
        extra={
            "extra_fields": {
                "object_key": object_key,
                "error": f"{type(exc).__name__}: {exc}",
            }
        },
    )