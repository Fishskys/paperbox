"""Tests for the docling-serve client (plan §2 T4).

No network and no services: every request goes through ``httpx.MockTransport``,
and the multipart body is parsed back so the *wire contract* (form field names,
values, repeated fields) is asserted directly -- docling-serve silently ignores
query parameters, so a field that is not in the body does not exist.
"""

from __future__ import annotations

import email
import json

import httpx
import pytest

from app.core.config import Settings, settings
from app.parsing import docling_client as dc

PAGE_BREAK = "<!-- page-break -->"
VERSION_PATH = "/version"
CONVERT_PATH = "/v1/convert/file"

#: The real 7-page paper's shape: 6 markers between the 7 pages.
MARKDOWN_3_PAGES = (
    "# Title\n\nAbstract\n\n<!-- page-break -->\n\n## 1 Introduction\n\n<!-- page-break -->\n\ntext\n"
)
VERSION_PAYLOAD = {"docling-serve": "1.35.0", "docling": "2.130.0", "docling-core": "2.x"}
PARSER_VERSION = "docling-serve 1.35.0 / docling 2.130.0"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _parse_multipart(request: httpx.Request) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Read the multipart form back off the wire.

    Returns ``(fields, filenames)``; repeated fields accumulate in a list.
    """
    body = request.read()
    content_type = request.headers["content-type"]
    message = email.message_from_bytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    fields: dict[str, list[str]] = {}
    filenames: dict[str, str] = {}
    for part in message.walk():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        fields.setdefault(name, []).append(payload.decode("utf-8", "replace"))
        filename = part.get_filename()
        if filename:
            filenames[name] = filename
    return fields, filenames


def _success_response(
    markdown: str = MARKDOWN_3_PAGES,
    *,
    processing_time: float | None = 1.25,
    json_content: dict | None = None,
    status: str = "success",
    errors: list[str] | None = None,
) -> httpx.Response:
    payload: dict = {
        "document": {
            "filename": "paper.pdf",
            "md_content": markdown,
            "json_content": json_content,
        },
        "status": status,
        "errors": errors or [],
        "timings": {},
        "confidence": {},
    }
    if processing_time is not None:
        payload["processing_time"] = processing_time
    return httpx.Response(200, json=payload)


class _Server:
    """Fake docling-serve: queued conversion responses + a working /version."""

    def __init__(self, *responses, version: tuple[str, str] | None = ("1.35.0", "2.130.0")):
        self.queue = list(responses)
        self.version = version
        self.conversions: list[dict[str, list[str]]] = []
        self.filenames: list[dict[str, str]] = []
        self.version_calls = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport(), base_url="http://docling.test")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == VERSION_PATH:
            self.version_calls += 1
            if self.version is None:
                return httpx.Response(500, text="nope")
            return httpx.Response(
                200,
                json={"docling-serve": self.version[0], "docling": self.version[1]},
            )
        assert request.url.path == CONVERT_PATH, request.url.path
        fields, filenames = _parse_multipart(request)
        self.conversions.append(fields)
        self.filenames.append(filenames)
        if not self.queue:
            raise AssertionError("unexpected extra conversion request")
        response = self.queue.pop(0)
        return response(request) if callable(response) else response

    @property
    def requests(self) -> int:
        return len(self.conversions)


@pytest.fixture(autouse=True)
def _docling_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every knob the client reads, so a developer's .env cannot leak in."""
    for name, value in {
        "docling_url": "http://docling.test",
        "docling_ocr": False,
        "docling_formula_enrichment": True,
        "docling_timeout": 660.0,
        "docling_document_timeout": 600.0,
        "docling_max_retries": 1,
        "docling_table_mode": "accurate",
        "docling_formula_preset": "codeformulav2",
        "docling_page_break": PAGE_BREAK,
        "docling_image_tag": "",
    }.items():
        monkeypatch.setattr(settings, name, value)


# --------------------------------------------------------------------------- #
# pure pieces: page counting, field building, version strings
# --------------------------------------------------------------------------- #


def test_page_count_from_markdown_counts_markers_between_pages() -> None:
    assert dc.page_count_from_markdown(MARKDOWN_3_PAGES, PAGE_BREAK) == 3
    assert dc.page_count_from_markdown("no marker here\n", PAGE_BREAK) == 1
    # A marker that is not alone on its line is prose, not a page boundary.
    assert dc.page_count_from_markdown(f"see {PAGE_BREAK} above\n", PAGE_BREAK) == 1


def test_request_fields_carry_the_frozen_contract() -> None:
    fields = dc.build_request_fields(
        ocr=False,
        table_mode="accurate",
        page_break=PAGE_BREAK,
        document_timeout=600.0,
        formula=True,
        formula_preset="codeformulav2",
    )
    assert fields["to_formats"] == ["md", "json"]
    assert fields["md_page_break_placeholder"] == PAGE_BREAK
    assert fields["do_ocr"] == "false"
    assert fields["do_formula_enrichment"] == "true"
    assert fields["code_formula_preset"] == "codeformulav2"
    assert fields["table_mode"] == "accurate"
    assert fields["document_timeout"] == "600"
    # Without a deadline the server keeps every core busy long after the caller left.
    assert fields["md_compact_tables"] == "true"
    assert fields["do_pdf_heading_hierarchy"] == "true"
    assert fields["do_table_structure"] == "true"
    assert fields["include_images"] == "false"
    assert fields["image_export_mode"] == "placeholder"
    assert fields["abort_on_error"] == "false"
    assert fields["force_ocr"] == "false"
    assert "page_range" not in fields

    options = json.loads(fields["pdf_heading_hierarchy_options"])
    assert options == dc.HEADING_HIERARCHY_OPTIONS
    assert options["enabled"] is True
    assert options["max_level"] == 6
    # Stable serialisation: the server keys its converter cache on this string.
    assert fields["pdf_heading_hierarchy_options"] == json.dumps(
        dc.HEADING_HIERARCHY_OPTIONS, separators=(",", ":"), sort_keys=True
    )


def test_formula_off_drops_the_preset() -> None:
    fields = dc.build_request_fields(
        ocr=True,
        table_mode="fast",
        page_break=PAGE_BREAK,
        document_timeout=None,
        formula=False,
        formula_preset="codeformulav2",
    )
    assert fields["do_formula_enrichment"] == "false"
    # Sending the preset without enrichment made the server answer 404.
    assert "code_formula_preset" not in fields
    assert fields["do_ocr"] == "true"
    assert fields["table_mode"] == "fast"
    assert "document_timeout" not in fields


def test_page_range_is_a_repeated_field() -> None:
    fields = dc.build_request_fields(
        ocr=False,
        table_mode="accurate",
        page_break="<!-- page-break -->",
        document_timeout=600.0,
        formula=False,
        formula_preset="codeformulav2",
        page_range="1, 3",
    )
    assert fields["page_range"] == ["1", "3"]


def test_format_parser_version_handles_partial_payloads() -> None:
    assert dc.format_parser_version(VERSION_PAYLOAD) == PARSER_VERSION
    assert dc.format_parser_version({"docling": "2.130.0"}) == "docling 2.130.0"
    assert dc.format_parser_version({}) == ""
    assert dc.format_parser_version(None) == ""
    assert dc.format_parser_version("1.35.0") == ""


def test_parse_conversion_payload_rejects_empty_markdown() -> None:
    with pytest.raises(dc.DoclingFailed):
        dc.parse_conversion_payload(
            {"document": {"md_content": "   "}, "status": "success", "errors": []},
            page_break=PAGE_BREAK,
            parser_version="",
        )
    with pytest.raises(dc.DoclingFailed):
        dc.parse_conversion_payload(
            {"errors": ["boom"]}, page_break=PAGE_BREAK, parser_version=""
        )


def test_parse_conversion_payload_reads_json_content() -> None:
    result = dc.parse_conversion_payload(
        {
            "document": {"md_content": MARKDOWN_3_PAGES, "json_content": {"texts": []}},
            "status": "success",
            "errors": [],
            "processing_time": 2.5,
        },
        page_break=PAGE_BREAK,
        parser_version=PARSER_VERSION,
    )
    assert result.page_count == 3
    assert result.raw_json == {"texts": []}
    assert result.processing_time == 2.5
    assert result.degraded_reason is None


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #


def test_convert_returns_markdown_page_count_and_version() -> None:
    server = _Server(_success_response())
    result = dc.convert_markdown(
        b"%PDF-1.4 fake", filename="paper.pdf", client=server.client()
    )

    assert result.markdown == MARKDOWN_3_PAGES
    assert result.page_count == 3
    assert result.processing_time == 1.25
    # Provenance comes from the live service, not from the configuration.
    assert result.parser_version == PARSER_VERSION
    assert server.version_calls == 1
    assert result.formula_fallback is False
    assert result.degraded_reason is None
    assert result.notes == ()

    fields = server.conversions[0]
    assert fields["do_formula_enrichment"] == ["true"]
    assert fields["to_formats"] == ["md", "json"]
    assert fields["md_page_break_placeholder"] == [PAGE_BREAK]
    assert fields["pdf_heading_hierarchy_options"] == [
        json.dumps(dc.HEADING_HIERARCHY_OPTIONS, separators=(",", ":"), sort_keys=True)
    ]
    assert server.filenames[0]["files"] == "paper.pdf"


def test_convert_falls_back_to_the_image_tag_for_provenance() -> None:
    server = _Server(_success_response(), version=None)
    settings.docling_image_tag = "paperbox-docling-cpu:v1.35.0-formula"
    result = dc.convert_markdown(
        b"%PDF-1.4 fake", filename="paper.pdf", client=server.client()
    )
    assert result.parser_version == "docling-serve (paperbox-docling-cpu:v1.35.0-formula)"


def test_convert_honours_explicit_options_and_page_range() -> None:
    server = _Server(_success_response())
    dc.convert_markdown(
        b"%PDF-1.4 fake",
        filename="paper.pdf",
        ocr=True,
        formula=False,
        page_range="1,3",
        client=server.client(),
    )
    fields = server.conversions[0]
    assert fields["do_ocr"] == ["true"]
    assert fields["do_formula_enrichment"] == ["false"]
    assert fields["page_range"] == ["1", "3"]


# --------------------------------------------------------------------------- #
# failures: classification and retries
# --------------------------------------------------------------------------- #


def test_server_error_is_retried_once_then_raises_unavailable() -> None:
    server = _Server(httpx.Response(502, text="bad gateway"), httpx.Response(502, text="bad gateway"))
    with pytest.raises(dc.DoclingUnavailable):
        dc.convert_markdown(
            b"%PDF-1.4 fake",
            filename="paper.pdf",
            formula=False,
            client=server.client(),
        )
    # One retry (DOCLING_MAX_RETRIES=1), no formula attempt.
    assert server.requests == 2


def test_client_error_is_not_retried_and_is_flagged() -> None:
    server = _Server(httpx.Response(400, json={"detail": "not a pdf"}))
    with pytest.raises(dc.DoclingFailed) as excinfo:
        dc.convert_markdown(
            b"nonsense", filename="paper.txt", client=server.client()
        )
    assert server.requests == 1
    assert dc.is_client_error(excinfo.value) is True
    assert dc.is_transient_failure(excinfo.value) is False
    assert "not a pdf" in str(excinfo.value)


def test_transient_failure_without_formulas_retries_then_succeeds() -> None:
    server = _Server(httpx.Response(503, text="warming up"), _success_response())
    result = dc.convert_markdown(
        b"%PDF-1.4 fake",
        filename="paper.pdf",
        formula=False,
        client=server.client(),
    )
    assert result.formula_fallback is False
    assert server.requests == 2
    for fields in server.conversions:
        assert fields["do_formula_enrichment"] == ["false"]


def test_connection_error_is_classified_as_unavailable() -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    server = _Server(_boom, _boom, _boom)
    with pytest.raises(dc.DoclingUnavailable):
        dc.convert_markdown(
            b"%PDF-1.4 fake", filename="paper.pdf", client=server.client()
        )
    # One shot with formulas, then the no-formula attempt with its retry.
    assert server.requests == 3


def test_unparseable_body_is_transient() -> None:
    server = _Server(httpx.Response(200, text="<html>gateway</html>"), _success_response())
    result = dc.convert_markdown(
        b"%PDF-1.4 fake", filename="paper.pdf", client=server.client()
    )
    assert result.formula_fallback is True
    assert server.conversions[1]["do_formula_enrichment"] == ["false"]


# --------------------------------------------------------------------------- #
# formula degradation (plan decision 16)
# --------------------------------------------------------------------------- #


def test_formula_blowup_falls_back_to_text_formulas() -> None:
    """The measured T2c failure shape: empty 502 body after ~151s."""
    server = _Server(httpx.Response(502, text=""), _success_response())
    result = dc.convert_markdown(
        b"%PDF-1.4 fake", filename="paper.pdf", client=server.client()
    )

    assert result.markdown == MARKDOWN_3_PAGES
    assert result.formula_fallback is True
    assert result.notes and "fallback" in result.notes[0]
    assert server.conversions[0]["do_formula_enrichment"] == ["true"]
    assert server.conversions[1]["do_formula_enrichment"] == ["false"]
    assert "code_formula_preset" not in server.conversions[1]


def test_formula_fallback_is_recorded_in_degraded_reason() -> None:
    server = _Server(httpx.Response(502, text=""), _success_response())
    result = dc.convert_markdown(
        b"%PDF-1.4 fake", filename="paper.pdf", client=server.client()
    )
    assert result.degraded_reason == dc.FORMULA_FALLBACK_REASON == "formulas=text"


def test_document_failure_on_http_200_also_falls_back() -> None:
    server = _Server(
        _success_response("", status="failure", errors=["timed out"], processing_time=None),
        _success_response(),
    )
    result = dc.convert_markdown(
        b"%PDF-1.4 fake", filename="paper.pdf", client=server.client()
    )
    assert result.formula_fallback is True
    assert server.requests == 2


def test_formula_attempt_is_not_retried_before_the_fallback() -> None:
    """The formula failure mode is deterministic: retrying wastes minutes."""
    server = _Server(httpx.Response(502, text=""), _success_response())
    dc.convert_markdown(b"%PDF-1.4 fake", filename="paper.pdf", client=server.client())
    assert server.requests == 2


def test_all_attempts_failing_raises_the_last_error() -> None:
    server = _Server(
        httpx.Response(502, text=""), httpx.Response(502, text=""), httpx.Response(502, text="")
    )
    with pytest.raises(dc.DoclingUnavailable):
        dc.convert_markdown(
            b"%PDF-1.4 fake", filename="paper.pdf", client=server.client()
        )
    assert server.requests == 3


def test_client_error_skips_the_formula_fallback() -> None:
    server = _Server(httpx.Response(422, json={"detail": "unsupported option"}))
    with pytest.raises(dc.DoclingFailed):
        dc.convert_markdown(
            b"%PDF-1.4 fake", filename="paper.pdf", client=server.client()
        )
    assert server.requests == 1


def test_max_retries_zero_means_one_shot_per_attempt() -> None:
    settings.docling_max_retries = 0
    server = _Server(httpx.Response(502, text=""), httpx.Response(502, text=""))
    with pytest.raises(dc.DoclingUnavailable):
        dc.convert_markdown(
            b"%PDF-1.4 fake", filename="paper.pdf", client=server.client()
        )
    assert server.requests == 2


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def test_unconfigured_url_degrades_without_dialing() -> None:
    settings.docling_url = ""

    def _explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("client must not dial when DOCLING_URL is empty")

    server = _Server(_explode)
    with pytest.raises(dc.DoclingUnavailable):
        dc.convert_markdown(
            b"%PDF-1.4 fake", filename="paper.pdf", client=server.client()
        )
    assert server.requests == 0


def test_resolve_parser_version_never_raises() -> None:
    server = _Server(version=None)
    assert dc.resolve_parser_version(client=server.client()) == "docling-serve (version unknown)"
    settings.docling_image_tag = "img:v1"
    assert dc.resolve_parser_version(client=server.client()) == "docling-serve (img:v1)"


def test_settings_validate_the_parser_backend() -> None:
    assert Settings(parser_backend="DOCLING").parser_backend == "docling"
    assert Settings(parser_backend="pypdf").parser_backend == "pypdf"
    with pytest.raises(ValueError):
        Settings(parser_backend="docling-serve")


def test_settings_require_the_client_timeout_to_exceed_the_document_timeout() -> None:
    with pytest.raises(ValueError):
        Settings(docling_timeout=600.0, docling_document_timeout=600.0)
    assert Settings(docling_timeout=660.0, docling_document_timeout=600.0).docling_timeout == 660


def test_settings_reject_a_bad_table_mode() -> None:
    with pytest.raises(ValueError):
        Settings(docling_table_mode="turbo")
