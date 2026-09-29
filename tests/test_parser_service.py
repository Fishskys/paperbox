"""parser_service: backend selection, degradation, non-silent fallback (plan T6)."""

from __future__ import annotations

import io
import logging

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.core.config import settings
from app.parsing.docling_client import DoclingFailed, DoclingResult, DoclingUnavailable
from app.parsing.pdf import PdfParseError
from app.services import parser_service

# --------------------------------------------------------------------------- #
# fixtures: a real one-page PDF for the pypdf path, and converter fakes
# --------------------------------------------------------------------------- #

PDF_TEXT = "The comparator offsets are cancelled by chopping."


def _pdf_bytes(text: str = PDF_TEXT) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                        }
                    )
                }
            )
        }
    )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


DOCLING_MARKDOWN = (
    "# The Effect of Chopping on Comparator Offsets\n"
    "\n"
    "Body paragraph one.\n"
    "\n"
    "<!-- page-break -->\n"
    "\n"
    "## Measurement Setup\n"
    "\n"
    "Body paragraph two.\n"
)


def _ok_converter(result: DoclingResult) -> object:
    def convert(data: bytes, *, filename: str, page_range: str | None = None) -> DoclingResult:
        return result

    return convert


def _always_raises(exc: Exception) -> object:
    def convert(data: bytes, *, filename: str, page_range: str | None = None) -> DoclingResult:
        raise exc

    return convert


# --------------------------------------------------------------------------- #
# backend selection
# --------------------------------------------------------------------------- #


def test_docling_is_used_when_it_works() -> None:
    result = DoclingResult(
        markdown=DOCLING_MARKDOWN,
        page_count=2,
        parser_version="fake 1.0",
        processing_time=1.25,
    )
    bundle = parser_service.parse_pdf(
        b"not read by the fake", filename="a.pdf", backend="docling",
        converter=_ok_converter(result),  # type: ignore[arg-type]
    )
    assert bundle.backend == "docling"
    assert bundle.parser_version == "fake 1.0"
    assert bundle.degraded_reason is None
    assert bundle.page_count == 2
    assert len(bundle.spans) == 2
    # markdown went through the shared dialect (normalized, markers kept)
    assert bundle.markdown.startswith("# The Effect of Chopping")
    assert "<!-- page-break -->" in bundle.markdown
    # slices line up with the page spans (half-open, chunking-ready)
    for span in bundle.spans:
        assert bundle.markdown[span.char_start : span.char_end]
    assert bundle.timings["docling_s"] >= 0
    assert bundle.timings["docling_processing_s"] == 1.25


def test_default_backend_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "parser_backend", "pypdf")
    bundle = parser_service.parse_pdf(_pdf_bytes(), filename="a.pdf")
    assert bundle.backend == "pypdf"
    assert "chopping" in bundle.markdown


def test_explicit_backend_wins_over_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "parser_backend", "pypdf")
    result = DoclingResult(markdown="## Only Heading\n", page_count=1, parser_version="f")
    bundle = parser_service.parse_pdf(
        b"ignored", filename="a.pdf", backend="docling",
        converter=_ok_converter(result),  # type: ignore[arg-type]
    )
    assert bundle.backend == "docling"


def test_unknown_explicit_backend_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown parser backend"):
        parser_service.parse_pdf(_pdf_bytes(), filename="a.pdf", backend="banana")


def test_explicit_pypdf_never_calls_docling() -> None:
    def crash(data: bytes, *, filename: str, page_range: str | None = None) -> DoclingResult:
        raise AssertionError("docling must not be called")

    bundle = parser_service.parse_pdf(
        _pdf_bytes(), filename="a.pdf", backend="pypdf", converter=crash
    )
    assert bundle.backend == "pypdf"
    assert "chopping" in bundle.markdown


# --------------------------------------------------------------------------- #
# degradation
# --------------------------------------------------------------------------- #


def test_falls_back_to_pypdf_when_docling_is_unavailable() -> None:
    bundle = parser_service.parse_pdf(
        _pdf_bytes(),
        filename="a.pdf",
        backend="docling",
        converter=_always_raises(DoclingUnavailable("connection refused")),  # type: ignore[arg-type]
    )
    assert bundle.backend == "pypdf"
    assert "chopping" in bundle.markdown
    assert bundle.degraded_reason is not None
    assert "docling" in bundle.degraded_reason
    assert "connection refused" in bundle.degraded_reason


def test_docling_failed_also_falls_back() -> None:
    bundle = parser_service.parse_pdf(
        _pdf_bytes(),
        filename="a.pdf",
        backend="docling",
        converter=_always_raises(DoclingFailed("400 client error")),  # type: ignore[arg-type]
    )
    assert bundle.backend == "pypdf"
    assert "DoclingFailed" in (bundle.degraded_reason or "")


def test_fallback_is_not_silent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        parser_service.parse_pdf(
            _pdf_bytes(),
            filename="report-42.pdf",
            backend="docling",
            converter=_always_raises(DoclingUnavailable("timeout")),  # type: ignore[arg-type]
        )
    assert any(
        record.name == "app.services.parser_service"
        and record.levelno >= logging.WARNING
        and "fell back to pypdf" in record.getMessage()
        and getattr(record, "extra_fields", {}).get("filename") == "report-42.pdf"
        for record in caplog.records
    )


def test_fallback_reason_is_prepended_to_the_pypdf_reasons() -> None:
    bundle = parser_service.parse_pdf(
        _pdf_bytes(),
        filename="a.pdf",
        backend="docling",
        converter=_always_raises(DoclingUnavailable("down")),  # type: ignore[arg-type]
    )
    assert (bundle.degraded_reason or "").startswith("docling unavailable ")
    assert "no formula latex" in (bundle.degraded_reason or "")


def test_formula_fallback_reason_survives_the_docling_path() -> None:
    result = DoclingResult(
        markdown="## A Section\n\nBody.\n",
        page_count=1,
        parser_version="f",
        formula_fallback=True,
    )
    bundle = parser_service.parse_pdf(
        b"ignored", filename="a.pdf", backend="docling",
        converter=_ok_converter(result),  # type: ignore[arg-type]
    )
    assert bundle.backend == "docling"
    assert bundle.degraded_reason == "formulas=text"


def test_page_range_is_passed_to_the_converter() -> None:
    seen: dict[str, object] = {}

    def convert(data: bytes, *, filename: str, page_range: str | None = None) -> DoclingResult:
        seen["page_range"] = page_range
        return DoclingResult(markdown="## H\n\nBody.\n", page_count=1, parser_version="f")

    parser_service.parse_pdf(
        b"ignored", filename="a.pdf", backend="docling", converter=convert, page_range="1-3"
    )
    assert seen["page_range"] == "1-3"


# --------------------------------------------------------------------------- #
# error propagation (never silently swallowed)
# --------------------------------------------------------------------------- #


def test_bad_pdf_raises_through_the_pypdf_path() -> None:
    with pytest.raises(PdfParseError):
        parser_service.parse_pdf(b"this is not a pdf", filename="broken.pdf", backend="pypdf")


def test_bad_pdf_raises_through_a_failed_fallback() -> None:
    """A PDF that even pypdf cannot read must surface, not come back empty."""
    with pytest.raises(PdfParseError):
        parser_service.parse_pdf(
            b"this is not a pdf",
            filename="broken.pdf",
            backend="docling",
            converter=_always_raises(DoclingUnavailable("down")),  # type: ignore[arg-type]
        )


def test_two_consecutive_parses_do_not_deadlock_on_the_semaphore() -> None:
    for _ in range(2):
        bundle = parser_service.parse_pdf(_pdf_bytes(), filename="a.pdf", backend="pypdf")
        assert bundle.page_count == 1