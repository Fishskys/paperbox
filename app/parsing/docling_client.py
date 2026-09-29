"""Thin HTTP client for docling-serve (plan 2026-09-28_160551 §1.3 / §2 T4).

docling runs as a **separate service** (a container, since 2026-09-29 on the fnOS
NAS) rather than an in-process library, because one conversion saturates the CPU
for minutes and the app must stay responsive. This module is the only place that
knows the wire format; ``app.services.parser_service`` (T6) decides *whether* to
call it and what to do when it fails.

Three rules the rest of the pipeline relies on:

1. **Options travel as multipart form fields.** docling-serve 1.35.0 accepts only
   ``multipart/form-data`` on ``/v1/convert/file`` and *silently ignores query
   parameters*, so a request built with ``params=`` runs on server defaults (no
   page markers, flat headings, OCR on). Measured in the T1/T2 probes.
2. **Every failure is classified and degradable, never silent.** Unreachable
   service, timeout, transport error, 5xx, or an unparseable/empty body raise
   :class:`DoclingUnavailable`; 4xx and a 200 whose ``errors`` list is non-empty
   raise :class:`DoclingFailed`. Transient failures are retried
   ``DOCLING_MAX_RETRIES`` times (default 1); a client error is not.
3. **Formula enrichment degrades by itself** (plan decision 16). Formulas are
   wanted (``$$…$$``), but on a formula-dense paper the conversion can run into
   the container's memory ceiling and the request dies with an empty body at the
   document timeout. That is far worse than plain text, so on a *transient*
   failure the client retries once with ``do_formula_enrichment=false`` and marks
   the result ``degraded_reason="formulas=text"``. "No formulas" is acceptable,
   "no paper" is not.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

CONVERT_PATH = "/v1/convert/file"
VERSION_PATH = "/version"

#: ``pdf_heading_hierarchy_options`` as frozen in the contract (plan §1.3).
#: Measured caveat: in docling-serve 1.35.0 this is a **no-op for the markdown
#: export** -- the relative levels actually come from ``do_pdf_heading_hierarchy``,
#: and the paper title's ``#`` is added by our own post-processing (plan §0.6).
#: It is still sent so the wire contract matches what was agreed, and
#: ``max_level=6`` clamps a stray level-7 (plan decision 12).
HEADING_HIERARCHY_OPTIONS: dict[str, Any] = {
    "enabled": True,
    "use_numbering": True,
    "use_style": True,
    "use_font_style": True,
    "max_level": 6,
}

#: Fields from the plan's §1.3 mapping table that never change per request.
#: Values are strings because everything travels as a form field.
STATIC_FIELDS: dict[str, str] = {
    "md_compact_tables": "true",
    # Server default is False, which leaves every heading at one flat level.
    "do_pdf_heading_hierarchy": "true",
    "do_table_structure": "true",
    "table_cell_matching": "true",
    # Images only ever become a placeholder line, matching the pypdf fallback.
    "include_images": "false",
    "image_export_mode": "placeholder",
    # One bad page must not discard the whole document.
    "abort_on_error": "false",
    # Never let OCR overwrite an existing text layer.
    "force_ocr": "false",
}

#: Marker recorded in ``DoclingResult.degraded_reason`` / ``ParseBundle``.
FORMULA_FALLBACK_REASON = "formulas=text"


class DoclingError(RuntimeError):
    """Base class: the conversion did not produce markdown."""


class DoclingUnavailable(DoclingError):
    """Service unreachable/too slow, or it failed after retries.

    Degradable: the caller falls back to the pypdf backend.
    """


class DoclingFailed(DoclingError):
    """The service answered, but rejected the request or reported an error.

    Also degradable, but it points at the *input* (or the options) rather than at
    the service, so it keeps a louder trace.
    """


def is_client_error(exc: BaseException) -> bool:
    """True when the service blamed the *request* (HTTP 4xx).

    Those are the only failures where replaying a different set of options cannot
    help, so the caller stops immediately instead of trying the no-formula path.
    """
    return bool(getattr(exc, "client_error", False))


def _client_error(message: str) -> DoclingFailed:
    error = DoclingFailed(message)
    error.client_error = True  # type: ignore[attr-defined]
    return error


@dataclass(slots=True)
class DoclingResult:
    """One successful conversion."""

    markdown: str
    page_count: int
    parser_version: str
    processing_time: float | None = None
    raw_json: dict[str, Any] | None = None
    #: True when formulas were requested but had to be dropped (see rule 3 above).
    formula_fallback: bool = False
    #: Free-form notes for the artifact/log ("formula fallback after ...").
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def degraded_reason(self) -> str | None:
        """``None`` for a clean parse, otherwise the reason to persist."""
        if self.formula_fallback:
            return FORMULA_FALLBACK_REASON
        return None


def _bool_field(value: bool) -> str:
    return "true" if value else "false"


def page_count_from_markdown(markdown: str, page_break: str) -> int:
    """Pages implied by the page-break markers.

    docling writes the marker *between* pages, so the marker count is
    ``pages - 1`` (measured: 7 pages -> 6 markers, 15 -> 14). A document whose
    text has no marker at all is reported as 1 page rather than 0, because the
    caller uses this for ``page_count`` metadata.
    """
    marker = (page_break or "").strip()
    if not marker:
        return 1
    count = sum(1 for line in markdown.splitlines() if line.strip() == marker)
    return count + 1


def build_request_fields(
    *,
    ocr: bool,
    table_mode: str,
    page_break: str,
    document_timeout: float | None,
    formula: bool,
    formula_preset: str,
    formats: list[str] | None = None,
    page_range: str | None = None,
) -> dict[str, Any]:
    """Build the multipart form fields for one conversion request.

    Pure function: the tests assert the exact wire contract here rather than
    against a real server.
    """
    fields: dict[str, Any] = dict(STATIC_FIELDS)
    # httpx expands a list value into repeated multipart fields, which is what the
    # server's ``to_formats``/``page_range`` arrays need.
    fields["to_formats"] = list(formats or ["md", "json"])
    fields["md_page_break_placeholder"] = page_break
    fields["do_ocr"] = _bool_field(ocr)
    fields["table_mode"] = table_mode
    # The server matches the options object against its cache key, so the value
    # must be byte-stable: sorted keys, no whitespace.
    fields["pdf_heading_hierarchy_options"] = json.dumps(
        HEADING_HIERARCHY_OPTIONS, separators=(",", ":"), sort_keys=True
    )
    if formula:
        # Both fields are required; ``do_formula_enrichment`` alone answers 404
        # ("Preset 'default' not found for CodeFormulaVlmOptions").
        fields["do_formula_enrichment"] = "true"
        fields["code_formula_preset"] = formula_preset
    else:
        fields["do_formula_enrichment"] = "false"
    if document_timeout is not None:
        # The server default is "no deadline": a single conversion then keeps every
        # core busy long after the caller gave up (measured, 17+ minutes).
        fields["document_timeout"] = str(int(document_timeout))
    if page_range:
        fields["page_range"] = [
            part.strip() for part in str(page_range).split(",") if part.strip()
        ]
    return fields


def format_parser_version(payload: object) -> str:
    """Turn ``GET /version`` into the string that goes into artifact metadata."""
    if not isinstance(payload, dict):
        return ""
    serve = str(payload.get("docling-serve") or "").strip()
    docling = str(payload.get("docling") or "").strip()
    parts: list[str] = []
    if serve:
        parts.append(f"docling-serve {serve}")
    if docling:
        parts.append(f"docling {docling}")
    return " / ".join(parts)


def _response_error_detail(response: httpx.Response) -> str:
    """Short, loggable description of a failed response body."""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - any decode failure is just "unparseable"
        body = (response.text or "").strip()
        return f"unparseable body ({len(body)} chars)" if body else "empty body"
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(item) for item in errors[:3])
        detail = payload.get("detail")
        if detail:
            return str(detail)
    return f"http {response.status_code}"


def _errors_of(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors")
    if isinstance(errors, list):
        return [str(item) for item in errors if str(item).strip()]
    return []


def parse_conversion_payload(
    payload: object,
    *,
    page_break: str,
    parser_version: str,
    formula_fallback: bool = False,
    notes: tuple[str, ...] = (),
) -> DoclingResult:
    """Validate a 200 body and turn it into a :class:`DoclingResult`.

    Raises :class:`DoclingFailed` when the body carries errors or no markdown --
    a "successful" HTTP response with an empty document is the failure mode the
    formula blow-up produced, so it must not be mistaken for a parse.
    """
    if not isinstance(payload, dict):
        raise DoclingFailed("response body was not a JSON object")
    errors = _errors_of(payload)
    document = payload.get("document")
    if not isinstance(document, dict):
        raise DoclingFailed(
            "response body carried no document" + (f": {errors}" if errors else "")
        )
    markdown = document.get("md_content")
    if not isinstance(markdown, str) or not markdown.strip():
        detail = "; ".join(errors) if errors else "empty md_content"
        raise DoclingFailed(f"no markdown in response: {detail}")

    raw_json = document.get("json_content")
    processing_time = payload.get("processing_time")
    return DoclingResult(
        markdown=markdown,
        page_count=page_count_from_markdown(markdown, page_break),
        parser_version=parser_version,
        processing_time=(
            float(processing_time) if isinstance(processing_time, (int, float)) else None
        ),
        raw_json=raw_json if isinstance(raw_json, dict) else None,
        formula_fallback=formula_fallback,
        notes=notes,
    )


def resolve_parser_version(
    *,
    client: httpx.Client | None = None,
    base_url: str | None = None,
    timeout: float = 10.0,
) -> str:
    """Best-effort ``GET /version``; never raises.

    Falls back to the pinned image tag (``DOCLING_IMAGE_TAG``) and finally to a
    plain "version unknown" marker, so artifacts always carry *something*.
    """
    url = (base_url if base_url is not None else settings.docling_url).strip()
    own_client = client is None
    if own_client:
        if not url:
            return _version_fallback()
        client = httpx.Client(base_url=url, timeout=timeout)
    try:
        response = client.get(VERSION_PATH, timeout=timeout)
        if response.status_code == 200:
            version = format_parser_version(response.json())
            if version:
                return version
    except Exception as exc:  # noqa: BLE001 - provenance is best-effort
        logger.debug(
            "could not read the docling version",
            extra={"extra_fields": {"error": f"{type(exc).__name__}: {exc}"}},
        )
    finally:
        if own_client and client is not None:
            client.close()
    return _version_fallback()


def _version_fallback() -> str:
    tag = (settings.docling_image_tag or "").strip()
    return f"docling-serve ({tag})" if tag else "docling-serve (version unknown)"


def is_transient_failure(exc: BaseException) -> bool:
    """True when retrying -- or dropping formulas -- could plausibly help.

    4xx and "the server answered 200 but produced nothing" are *not* retried:
    they describe the request, not a flaky service.
    """
    if isinstance(exc, DoclingUnavailable):
        return True
    if isinstance(exc, DoclingFailed):
        return False
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


def convert_markdown(
    pdf: bytes,
    *,
    filename: str,
    ocr: bool | None = None,
    formula: bool | None = None,
    timeout: float | None = None,
    page_range: str | None = None,
    client: httpx.Client | None = None,
    base_url: str | None = None,
) -> DoclingResult:
    """Convert one PDF to markdown via docling-serve.

    ``ocr``/``formula`` default to ``DOCLING_OCR``/``DOCLING_FORMULA_ENRICHMENT``.
    Returns the first attempt that produces markdown; raises
    :class:`DoclingUnavailable` / :class:`DoclingFailed` when every attempt fails.
    """
    url = (base_url if base_url is not None else settings.docling_url).strip()
    if not url:
        raise DoclingUnavailable("DOCLING_URL is not configured")

    want_ocr = settings.docling_ocr if ocr is None else ocr
    want_formula = settings.docling_formula_enrichment if formula is None else formula
    request_timeout = settings.docling_timeout if timeout is None else timeout
    retries = max(0, int(settings.docling_max_retries))

    own_client = client is None
    if own_client:
        client = httpx.Client(base_url=url, timeout=request_timeout)
    assert client is not None  # for type checkers

    # Attempt plan. With formulas on, the first attempt gets one single shot: the
    # known failure mode is deterministic (a formula-dense paper hitting the memory
    # ceiling), so retrying it just burns another few minutes before the fallback
    # anyway. Everything after that gets the configured retry budget.
    attempts: list[tuple[bool, bool, int]] = []
    if want_formula:
        attempts.append((True, False, 1))
        attempts.append((False, True, 1 + retries))
    else:
        attempts.append((False, False, 1 + retries))

    parser_version: str | None = None
    last_error: DoclingError | None = None
    try:
        for send_formula, is_fallback, tries in attempts:
            for attempt in range(1, tries + 1):
                try:
                    result = _convert_once(
                        client,
                        pdf=pdf,
                        filename=filename,
                        ocr=want_ocr,
                        formula=send_formula,
                        page_range=page_range,
                        timeout=request_timeout,
                    )
                except (DoclingError, httpx.TransportError) as exc:
                    error = (
                        exc
                        if isinstance(exc, DoclingError)
                        else DoclingUnavailable(f"{type(exc).__name__}: {exc}")
                    )
                    last_error = error
                    transient = is_transient_failure(error)
                    logger.warning(
                        "docling conversion failed",
                        extra={
                            "extra_fields": {
                                "backend": "docling",
                                "file": filename,
                                "formula": send_formula,
                                "fallback": is_fallback,
                                "attempt": attempt,
                                "max_attempts": tries,
                                "transient": transient,
                                "error": f"{type(error).__name__}: {error}",
                            }
                        },
                    )
                    if is_client_error(error):
                        # 4xx: the request itself is wrong. Retrying it, or
                        # dropping formulas, cannot help -- surface it now.
                        raise error
                    if not transient:
                        # A document-level failure ("status=failure", empty
                        # markdown): re-sending the same options is pointless, but
                        # the no-formula attempt still gets its chance below.
                        break
                    if attempt < tries:
                        continue
                    break  # out of tries -> next attempt, or raise below

                if is_fallback:
                    note = (
                        f"formula fallback after {last_error}"
                        if last_error
                        else "formula fallback after a transient failure"
                    )
                    result.formula_fallback = True
                    result.notes = result.notes + (note,)
                    logger.warning(
                        "docling parsed without formula enrichment",
                        extra={
                            "extra_fields": {
                                "backend": "docling",
                                "file": filename,
                                "fallback": True,
                                "degraded_reason": result.degraded_reason,
                                "pages": result.page_count,
                                "chars": len(result.markdown),
                                "cause": f"{type(last_error).__name__}: {last_error}",
                            }
                        },
                    )
                if parser_version is None:
                    parser_version = resolve_parser_version(
                        client=client, base_url=url, timeout=min(request_timeout, 10.0)
                    )
                result.parser_version = result.parser_version or parser_version
                logger.info(
                    "docling conversion finished",
                    extra={
                        "extra_fields": {
                            "backend": "docling",
                            "file": filename,
                            "pages": result.page_count,
                            "chars": len(result.markdown),
                            "processing_time": result.processing_time,
                            "formula": send_formula,
                            "formula_fallback": result.formula_fallback,
                            "parser_version": result.parser_version,
                        }
                    },
                )
                return result
    finally:
        if own_client and client is not None:
            client.close()

    raise last_error or DoclingUnavailable("docling conversion failed")


def _convert_once(
    client: httpx.Client,
    *,
    pdf: bytes,
    filename: str,
    ocr: bool,
    formula: bool,
    page_range: str | None,
    timeout: float,
) -> DoclingResult:
    """One POST; classifies the response into success / unavailable / failed."""
    fields = build_request_fields(
        ocr=ocr,
        table_mode=settings.docling_table_mode,
        page_break=settings.docling_page_break,
        document_timeout=settings.docling_document_timeout,
        formula=formula,
        formula_preset=settings.docling_formula_preset,
        page_range=page_range,
    )
    started = time.perf_counter()
    try:
        response = client.post(
            CONVERT_PATH,
            data=fields,
            files={"files": (filename, pdf, "application/pdf")},
            timeout=timeout,
        )
    except httpx.TransportError as exc:
        raise DoclingUnavailable(f"{type(exc).__name__}: {exc}") from exc
    wall_ms = int((time.perf_counter() - started) * 1000)

    if response.status_code >= 500:
        raise DoclingUnavailable(
            f"http {response.status_code}: {_response_error_detail(response)}"
        )
    if response.status_code >= 400:
        raise _client_error(
            f"http {response.status_code}: {_response_error_detail(response)}"
        )

    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - an unparseable 200 is a failure
        # This is the shape the formula blow-up produced: connection closed / empty
        # body. Treat it as transient so the no-formula retry gets its chance.
        raise DoclingUnavailable(
            f"response was not JSON after {wall_ms}ms: {type(exc).__name__}"
        ) from exc

    if isinstance(payload, dict):
        status = str(payload.get("status") or "")
        if status and status != "success":
            errors = _errors_of(payload)
            detail = "; ".join(errors) if errors else "no detail"
            # Document-level failure delivered with HTTP 200 (this is how an
            # aborted conversion surfaces). DoclingFailed without the
            # ``client_error`` flag, so the caller may still retry without formulas.
            raise DoclingFailed(f"status={status}: {detail}")
    return parse_conversion_payload(
        payload,
        page_break=settings.docling_page_break,
        parser_version="",
    )


__all__ = [
    "CONVERT_PATH",
    "FORMULA_FALLBACK_REASON",
    "HEADING_HIERARCHY_OPTIONS",
    "STATIC_FIELDS",
    "VERSION_PATH",
    "DoclingError",
    "DoclingFailed",
    "DoclingResult",
    "DoclingUnavailable",
    "build_request_fields",
    "convert_markdown",
    "format_parser_version",
    "is_client_error",
    "is_transient_failure",
    "page_count_from_markdown",
    "parse_conversion_payload",
    "resolve_parser_version",
]
