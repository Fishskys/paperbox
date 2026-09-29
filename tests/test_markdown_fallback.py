"""Tests for the shared markdown dialect (plan §1.1/§1.2, task T5).

The pypdf side has to produce the same *shape* docling produces, so these tests
pin the shape: one heading per section with the fallback's own level rules,
``<!-- page-break -->`` between pages, joined/dehyphenated paragraphs, a visible
marker where table structure was lost, and a ``degraded_reason`` that never hides
the losses. The docling-side helpers (level clamp, title promotion) are covered
here too, because both backends share this contract.
"""

from __future__ import annotations

from app.parsing.markdown import (
    DEGRADED_NO_FORMULA,
    DEGRADED_ORDER,
    DEGRADED_TABLE,
    PAGE_BREAK_DEFAULT,
    TABLE_FALLBACK_MARKER,
    clamp_heading_levels,
    heading_level,
    normalize_markdown,
    page_spans_from_markdown,
    prepare_docling_markdown,
    promote_paper_title,
    render_markdown,
)
from app.parsing.pdf import PageText
from tests.test_layout_columns import LEFT, RIGHT, single_column_pdf, two_column_pdf

PAGE_BREAK = PAGE_BREAK_DEFAULT


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #


def test_normalize_markdown_collapses_blank_lines() -> None:
    text = "First line.\n\n\n\nSecond line.\n\n\n"
    assert normalize_markdown(text) == "First line.\n\nSecond line."


def test_normalize_markdown_strips_trailing_space_and_collapses_runs() -> None:
    assert normalize_markdown("a   b  \n c\t\td \n") == "a b\nc d"
    assert normalize_markdown("crlf\r\nline\r\n") == "crlf\nline"


def test_normalize_markdown_unescapes_html_entities() -> None:
    assert normalize_markdown("a &gt; b &amp; c &lt; d") == "a > b & c < d"
    assert normalize_markdown("non\u00a0break\u200bspace") == "non breakspace"


def test_normalize_markdown_is_idempotent() -> None:
    once = normalize_markdown("# Title\n\n\n  Body &gt; here  \n\n")
    assert normalize_markdown(once) == once


# --------------------------------------------------------------------------- #
# heading levels (decision 14: the fallback keeps its own rules)
# --------------------------------------------------------------------------- #


def test_heading_level_follows_the_section_number_depth() -> None:
    assert heading_level(None, "Abstract") == 1
    assert heading_level("I", "Introduction") == 1
    assert heading_level("1", "Introduction") == 1
    assert heading_level("2.1", "Architecture") == 2
    assert heading_level("2.1.3", "Comparator") == 3


def test_heading_level_is_clamped_and_body_is_not_a_heading() -> None:
    assert heading_level("1.2.3.4.5.6.7", "Deep") == 6
    assert heading_level(None, "Body") is None


# --------------------------------------------------------------------------- #
# page spans (one implementation, shared with the docling side)
# --------------------------------------------------------------------------- #


def test_page_spans_from_markdown_marks_each_page() -> None:
    markdown = f"one\n\n{PAGE_BREAK}\n\ntwo\n\n{PAGE_BREAK}\n\nthree"
    page_count, spans = page_spans_from_markdown(markdown)
    assert page_count == 3
    assert [span.page for span in spans] == [1, 2, 3]
    assert markdown[spans[0].char_start : spans[0].char_end].strip() == "one"
    assert markdown[spans[1].char_start : spans[1].char_end].strip() == "two"
    assert markdown[spans[2].char_start : spans[2].char_end].strip() == "three"


def test_page_spans_of_a_single_page_document() -> None:
    page_count, spans = page_spans_from_markdown("only page")
    assert page_count == 1
    assert spans[0].char_start == 0
    assert spans[0].char_end == len("only page")
    assert page_spans_from_markdown("") == (0, [])


# --------------------------------------------------------------------------- #
# docling-side post-processing
# --------------------------------------------------------------------------- #


def test_promote_paper_title_takes_the_first_level_two_heading() -> None:
    raw = "\n\n## Delay Monitor Circuit for SRAM Nodes\n\ntext\n\n## Abstract\n\nmore\n"
    promoted = promote_paper_title(normalize_markdown(raw))
    assert promoted.startswith("# Delay Monitor Circuit for SRAM Nodes")
    assert "## Abstract" in promoted


def test_promote_paper_title_does_nothing_when_the_first_heading_is_a_section() -> None:
    for head in ("## Abstract", "## I. Introduction", "## 1 Introduction", "## References"):
        raw = f"{head}\n\ntext\n"
        assert promote_paper_title(raw) == raw


def test_promote_paper_title_does_nothing_when_content_comes_first() -> None:
    raw = "Some author affiliation\n\n## A Later Heading\n"
    assert promote_paper_title(raw) == raw


def test_promote_paper_title_ignores_page_markers() -> None:
    raw = f"## Real Title\n\n{PAGE_BREAK}\n\n## I. Introduction\n"
    assert promote_paper_title(raw).startswith("# Real Title")


def test_clamp_heading_levels_pulls_a_seventh_level_up() -> None:
    raw = "####### Too deep\n\n## Fine\n"
    assert clamp_heading_levels(raw) == "###### Too deep\n\n## Fine\n"


def test_prepare_docling_markdown_normalizes_clamps_and_promotes() -> None:
    raw = "\r\n\r\n## Paper Title  \r\n\r\n\r\n####### Deep\r\n"
    prepared = prepare_docling_markdown(raw)
    assert prepared == "# Paper Title\n\n###### Deep"


# --------------------------------------------------------------------------- #
# the pypdf renderer
# --------------------------------------------------------------------------- #


def _pages(*texts: str) -> list[PageText]:
    return [PageText(page=index, text=text) for index, text in enumerate(texts, start=1)]


def test_render_markdown_has_one_heading_per_detected_section() -> None:
    pages = _pages(
        "Abstract\n\nThe paper proposes a monitor circuit that tracks delay.\n\n"
        "I. Introduction\n\nDelay monitors are used to track process variation."
    )
    bundle = render_markdown(pages)
    assert "# Abstract" in bundle.markdown
    # ``_match_heading`` consumes the separator, which is why the label is
    # "I Introduction" -- exactly what chunk metadata (``Section.label``) carries.
    assert "# I Introduction" in bundle.markdown
    assert bundle.headings == [(1, "Abstract"), (1, "I Introduction")]


def test_render_markdown_uses_section_number_depth_for_levels() -> None:
    pages = _pages("2. Architecture\n\ntext\n\n2.1 Comparator\n\nmore text\n")
    bundle = render_markdown(pages)
    # ``2.`` is one dot short of a sub-section: level 1, while ``2.1`` is level 2.
    assert "# 2 Architecture" in bundle.markdown
    assert "## 2.1 Comparator" in bundle.markdown
    assert bundle.headings == [(1, "2 Architecture"), (2, "2.1 Comparator")]


def test_render_markdown_puts_a_page_marker_between_pages() -> None:
    bundle = render_markdown(_pages("page one text", "page two text", "page three text"))
    assert bundle.markdown.count(PAGE_BREAK) == 2
    assert bundle.page_count == 3
    assert [span.page for span in bundle.spans] == [1, 2, 3]
    assert bundle.markdown.index("page one text") < bundle.markdown.index(PAGE_BREAK)
    assert bundle.markdown.split(PAGE_BREAK)[1].strip() == "page two text"


def test_render_markdown_joins_wrapped_lines_and_dehyphenates() -> None:
    pages = _pages(
        "The residual offset is dominated by the sampl-\ning network and stays low."
    )
    bundle = render_markdown(pages)
    assert "sampling network and stays low." in bundle.markdown
    assert "-\n" not in bundle.markdown


def test_render_markdown_marks_fallback_tables() -> None:
    pages = _pages("Table I: measurements\n1.2 3.4 5.6\n2.0 4.3 6.1\nPlain prose after the table.")
    bundle = render_markdown(pages)
    assert TABLE_FALLBACK_MARKER in bundle.markdown
    assert "1.2 3.4 5.6" in bundle.markdown
    assert DEGRADED_TABLE in (bundle.degraded_reason or "")


def test_render_markdown_always_reports_the_formula_gap() -> None:
    bundle = render_markdown(_pages("Some body text."))
    assert DEGRADED_NO_FORMULA in (bundle.degraded_reason or "")
    assert bundle.backend == "pypdf"


def test_render_markdown_without_a_pdf_never_touches_the_layout() -> None:
    bundle = render_markdown(_pages("one", "two"))
    assert "layout_s" not in bundle.timings
    assert bundle.degraded_reason == DEGRADED_NO_FORMULA


# --------------------------------------------------------------------------- #
# end to end over the synthetic PDFs (T5 acceptance: two-column order)
# --------------------------------------------------------------------------- #


def test_two_column_page_is_reordered_left_then_right() -> None:
    data = two_column_pdf(interleaved=True)
    from app.parsing.pdf import extract_pages

    bundle = render_markdown(extract_pages(data), pdf_bytes=data)
    body = [line for line in bundle.markdown.split("\n") if line.strip()]
    # Two paragraphs: the whole left column, then the whole right column. Before
    # the repair the first line was both columns fused ("...cancelled by The
    # measured INL stays...").
    assert body == [" ".join(LEFT), " ".join(RIGHT)]
    assert bundle.degraded_reason == DEGRADED_NO_FORMULA
    assert "layout_s" in bundle.timings


def test_single_column_page_keeps_content_stream_order() -> None:
    from app.parsing.pdf import extract_pages

    data = single_column_pdf()
    pages = extract_pages(data)
    bundle = render_markdown(pages, pdf_bytes=data)
    body = bundle.markdown.split("\n")
    assert " ".join(LEFT[0].split()) in bundle.markdown
    assert body[0].startswith(LEFT[0])
    # A footer page number is dropped, which is why the text is not byte-equal.
    assert "1" not in body[-1:]


def test_unverified_order_is_reported_in_the_degraded_reason() -> None:
    from app.parsing.pdf import PageText

    pages = [PageText(page=1, text="A page"), PageText(page=2, text="Another page")]
    bundle = render_markdown(pages, pdf_bytes=b"")
    assert DEGRADED_ORDER in (bundle.degraded_reason or "")
    assert "layout_s" in bundle.timings
