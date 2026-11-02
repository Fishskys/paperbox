"""Readable fallback text, page provenance and hierarchy, independent of Docling."""

import pymupdf

from app.parsing.pdf import extract_pages
from app.services.parser_service import parse_pdf


def _document():
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((50, 60), "Paper Title Across Both Columns", fontsize=20)
    page.insert_text((140, 90), "Alice Smith and Bob Jones", fontsize=11)
    page.insert_text((50, 140), "1 Introduction", fontsize=12)
    for index in range(4):
        page.insert_text((50, 170 + index * 14), f"Left column sentence number {index} remains together", fontsize=9)
        page.insert_text((325, 170 + index * 14), f"Right column sentence number {index} follows later", fontsize=9)
    page.insert_text((50, 260), "1.1 Details", fontsize=11)
    page.insert_text((50, 285), "This paragraph retains its own section.", fontsize=9)
    result = document.tobytes()
    document.close()
    return result


def test_fallback_reads_left_column_before_right_and_keeps_front_matter():
    bundle = parse_pdf(_document(), filename="columns.pdf", backend="pymupdf")
    text = bundle.markdown
    assert text.index("Left column sentence number 3") < text.index("Right column sentence number 0")
    assert text.index("Alice Smith and Bob Jones") < text.index("Left column sentence number 0")
    assert bundle.backend == "pymupdf"


def test_fallback_preserves_heading_levels_and_paragraph_text():
    bundle = parse_pdf(_document(), filename="sections.pdf", backend="pymupdf")
    assert "# Paper Title Across Both Columns" in bundle.markdown
    assert "## 1 Introduction" in bundle.markdown
    assert "### 1.1 Details" in bundle.markdown
    assert "This paragraph retains its own section." in bundle.markdown
    assert bundle.spans[0].page == 1


def test_legacy_backend_setting_produces_an_honest_native_stamp():
    bundle = parse_pdf(_document(), filename="legacy.pdf", backend="pypdf")
    assert bundle.backend == "pymupdf"
    assert bundle.parser_version.startswith("pymupdf ")
    assert len(extract_pages(_document())) == 1


def test_metadata_bundle_reuses_one_bounded_native_extraction(monkeypatch):
    from app.parsing import mupdf
    from app.parsing.pdf import PageText
    from app.services import metadata_reextract

    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((50, 50), "A Compact Converter", fontsize=20)
        page.insert_text((50, 90), "Alice Smith", fontsize=12)
        page.insert_text((50, 120), "Abstract", fontsize=12)
        page.insert_text((50, 140), "We present a compact converter.", fontsize=10)
        document.new_page().insert_text((50, 50), "Second page")
        document.new_page().insert_text((50, 50), "Outside metadata scope")
        document.set_metadata({"author": "Alice Smith"})
        data = document.tobytes()
    bundle = mupdf.invoke("metadata_bundle", data)
    assert [page["page"] for page in bundle["pages"]] == [1, 2]
    def unexpected(*args, **kwargs):
        raise AssertionError("metadata bundle must not start another native process")
    monkeypatch.setattr(mupdf, "invoke", unexpected)
    pages = [PageText(**page) for page in bundle["pages"]]
    embedded, body, evidence = metadata_reextract.pdf_candidates(pages, pdf_bytes=data, bundle=bundle)
    assert embedded.authors == body["authors"] == ["Alice Smith"]
    assert evidence["byline_complete"] is True
