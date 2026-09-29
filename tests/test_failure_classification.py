"""Failure attribution codes (SPEC-P1 section A2).

Every code in :data:`app.core.errors.FAILURE_CODES` is exercised by constructing
the exception the pipeline would raise; no service is contacted. The last tests
build a real (blank) PDF with pypdf and drive the actual parsing/chunking path,
so the ``NO_TEXT_LAYER`` mapping is pinned to production behaviour rather than
to a hand-written stub.
"""

from __future__ import annotations

import io

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import FAILURE_CODES, Failure, classify_failure
from app.parsing.docling_client import DoclingFailed, DoclingUnavailable
from app.parsing.pdf import PdfParseError
from app.search.opensearch import SearchIndexError
from app.services import embedding_service
from app.services.ingestion_service import IngestionError, UnsupportedSource
from app.services.object_storage import ObjectStorageError

EXPECTED_CODES = {
    "NO_TEXT_LAYER",
    "ENCRYPTED_PDF",
    "CORRUPT_PDF",
    "DOWNLOAD_FAILED",
    "OVERSIZED",
    "UNSUPPORTED_TYPE",
    "DUPLICATE_FINGERPRINT",
    # Produced only when the docling backend is demanded *without* a fallback
    # (probes, and any future strict-parse caller): the pipeline itself degrades
    # to pypdf and records degraded_reason + a paper_degradations row instead.
    "PARSE_BACKEND_UNAVAILABLE",
    "PARSE_FAILED",
    "EMBEDDING_FAILED",
    "INDEX_FAILED",
    "STORAGE_FAILED",
    # Not produced by ``classify_failure``: the ingestion queue's startup
    # recovery stamps it on jobs that were mid-pipeline when the process died.
    "INTERRUPTED",
    "INTERNAL",
}


def code_of(exc: BaseException) -> str:
    return classify_failure(exc).code


# --------------------------------------------------------------------------- #
# the code list itself
# --------------------------------------------------------------------------- #
def test_failure_codes_are_exactly_the_specified_set() -> None:
    assert set(FAILURE_CODES) == EXPECTED_CODES
    assert len(FAILURE_CODES) == len(EXPECTED_CODES)


# --------------------------------------------------------------------------- #
# one case per code
# --------------------------------------------------------------------------- #
def test_no_text_layer_from_the_pipeline_error() -> None:
    failure = classify_failure(IngestionError("parsing produced no chunks"))
    assert failure.code == "NO_TEXT_LAYER"
    assert "OCR" in failure.message
    assert "IngestionError" in failure.message
    assert "parsing produced no chunks" in failure.message


def test_encrypted_pdf() -> None:
    failure = classify_failure(PdfParseError("encrypted PDF: password required"))
    assert failure.code == "ENCRYPTED_PDF"
    assert "PdfParseError" in failure.message
    assert "password required" in failure.message


def test_corrupt_pdf() -> None:
    failure = classify_failure(PdfParseError("invalid PDF: EOF marker not found"))
    assert failure.code == "CORRUPT_PDF"
    assert "PdfParseError" in failure.message


def test_docling_unreachable_without_a_fallback() -> None:
    failure = classify_failure(DoclingUnavailable("connection refused after 3 attempts"))
    assert failure.code == "PARSE_BACKEND_UNAVAILABLE"
    assert failure.message.startswith("PARSE_BACKEND_UNAVAILABLE: ")
    assert "connection refused" in failure.message
    assert "DoclingUnavailable" in failure.message


def test_docling_rejected_the_document() -> None:
    failure = classify_failure(DoclingFailed("HTTP 422: unsupported document"))
    assert failure.code == "PARSE_FAILED"
    assert failure.message.startswith("PARSE_FAILED: ")
    assert "422" in failure.message


def test_parse_failure_keeps_the_cause_chain() -> None:
    """The transport detail is all an operator gets: it must survive."""
    error = DoclingFailed("HTTP 502 bad gateway")
    error.__cause__ = httpx.ConnectError("all connection attempts failed")
    failure = classify_failure(error)
    assert failure.code == "PARSE_FAILED"
    assert "ConnectError" in failure.message


def test_download_failed_from_httpx() -> None:
    request = httpx.Request("GET", "https://example.org/paper.pdf")
    exc = httpx.ConnectTimeout("timed out", request=request)
    failure = classify_failure(exc)
    assert failure.code == "DOWNLOAD_FAILED"
    assert "ConnectTimeout" in failure.message


def test_download_failed_from_the_ingestion_wrapper() -> None:
    failure = classify_failure(IngestionError("download failed with HTTP 404"))
    assert failure.code == "DOWNLOAD_FAILED"


def test_oversized() -> None:
    failure = classify_failure(
        UnsupportedSource("file too large: 104857600 bytes exceeds 5242880 bytes")
    )
    assert failure.code == "OVERSIZED"


def test_unsupported_type() -> None:
    failure = classify_failure(UnsupportedSource("only PDF files are supported"))
    assert failure.code == "UNSUPPORTED_TYPE"


def test_unsupported_type_for_an_empty_payload() -> None:
    assert code_of(UnsupportedSource("uploaded file is empty")) == "UNSUPPORTED_TYPE"


def test_duplicate_fingerprint_from_integrity_error() -> None:
    exc = IntegrityError(
        "INSERT INTO papers ...",
        {},
        Exception(
            'duplicate key value violates unique constraint "uq_papers_fingerprint_live"'
        ),
    )
    failure = classify_failure(exc)
    assert failure.code == "DUPLICATE_FINGERPRINT"
    assert "uq_papers_fingerprint_live" in failure.message


def test_embedding_failed() -> None:
    failure = classify_failure(
        embedding_service.EmbeddingError("embedding server unreachable")
    )
    assert failure.code == "EMBEDDING_FAILED"
    assert "EmbeddingError" in failure.message


def test_index_failed() -> None:
    failure = classify_failure(SearchIndexError("bulk index failed: connection refused"))
    assert failure.code == "INDEX_FAILED"


def test_storage_failed() -> None:
    failure = classify_failure(ObjectStorageError("put_object failed for paperbox/x"))
    assert failure.code == "STORAGE_FAILED"


def test_internal_for_anything_else() -> None:
    failure = classify_failure(KeyError("nope"))
    assert failure.code == "INTERNAL"
    assert "KeyError" in failure.message


# --------------------------------------------------------------------------- #
# message shape
# --------------------------------------------------------------------------- #
def test_message_keeps_the_original_exception_and_text() -> None:
    failure = classify_failure(PdfParseError("cannot read page 3: boom"))
    assert failure.message.startswith("CORRUPT_PDF:")
    assert "PdfParseError: cannot read page 3: boom" in failure.message


def test_message_follows_the_cause_chain() -> None:
    try:
        try:
            raise ValueError("inner boom")
        except ValueError as inner:
            raise IngestionError("download failed") from inner
    except IngestionError as exc:
        failure = classify_failure(exc)

    assert failure.code == "DOWNLOAD_FAILED"
    assert "IngestionError: download failed" in failure.message
    assert "ValueError: inner boom" in failure.message


def test_failure_is_frozen() -> None:
    one = Failure("INTERNAL", "INTERNAL: RuntimeError: x")
    two = classify_failure(RuntimeError("x"))
    assert one == two
    with pytest.raises(Exception):
        one.code = "OTHER"


# --------------------------------------------------------------------------- #
# a real blank PDF: no text layer, so parsing/chunking yields nothing
# --------------------------------------------------------------------------- #
def blank_pdf_bytes(pages: int = 1) -> bytes:
    """Build a real PDF whose pages carry no text layer, using pypdf."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_blank_pdf_has_no_text_layer() -> None:
    from app.parsing.pdf import extract_pages

    pages = extract_pages(blank_pdf_bytes())

    assert len(pages) == 1
    assert pages[0].is_blank
    assert all(page.is_blank for page in pages)


def test_blank_pdf_chunks_to_nothing_and_classifies_as_no_text_layer() -> None:
    from app.parsing.chunking import chunk_document
    from app.parsing.pdf import extract_pages
    from app.parsing.structure import detect_sections, merge_short_sections

    pages = extract_pages(blank_pdf_bytes(pages=2))
    sections = merge_short_sections(detect_sections(pages))

    assert chunk_document(pages, sections) == []

    # The exact error the worker raises once chunking yields nothing.
    failure = classify_failure(IngestionError("parsing produced no chunks"))
    assert failure.code == "NO_TEXT_LAYER"
    assert "OCR" in failure.message
