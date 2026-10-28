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
