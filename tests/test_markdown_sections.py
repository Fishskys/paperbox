"""Rebuilding sections from the markdown dialect (plan §6.1 step 1, 2026-09-29).

The pipeline no longer detects sections on pypdf's raw pages: both backends hand
the chunker the *same* markdown, so the section and page grid has to be
recoverable from it. These tests pin that recovery -- front matter lands in
``Body``, every heading opens a section, a paragraph keeps the page it started
on, dialect comments never become chunk text, and a round trip through
``render_markdown`` reproduces the fallback's own sections.
"""

from __future__ import annotations

from app.parsing.chunking import MAX_TOKENS, chunk_markdown
from app.parsing.markdown import (
    PAGE_BREAK_DEFAULT,
    ParseBundle,
    page_spans_from_markdown,
    pages_and_sections_from_markdown,
    render_markdown,
    split_heading_number,
)
from app.parsing.pdf import PageText
from app.parsing.structure import SECTION_BODY, detect_sections, merge_short_sections

PAGE_BREAK = PAGE_BREAK_DEFAULT


def bundle_for(markdown: str, *, backend: str = "docling") -> ParseBundle:
    page_count, spans = page_spans_from_markdown(markdown, page_break=PAGE_BREAK)
    return ParseBundle(
        markdown=markdown,
        page_count=page_count,
        spans=spans,
        backend=backend,
        parser_version="test",
    )


# --------------------------------------------------------------------------- #
# number/title split
# --------------------------------------------------------------------------- #


def test_split_heading_number_recognises_the_fallbacks_tokens() -> None:
    assert split_heading_number("I. INTRODUCTION") == ("I", "INTRODUCTION")
    assert split_heading_number("IV. RESULTS AND DISCUSSION") == ("IV", "RESULTS AND DISCUSSION")
    assert split_heading_number("2.1 Architecture") == ("2.1", "Architecture")
    assert split_heading_number("2 Related Work") == ("2", "Related Work")


def test_split_heading_number_handles_docling_subsection_letters() -> None:
    assert split_heading_number("A. Real-Time Monitor") == ("A", "Real-Time Monitor")


def test_split_heading_number_leaves_unnumbered_and_ambiguous_titles_alone() -> None:
    assert split_heading_number("Abstract") == (None, "Abstract")
    assert split_heading_number("REFERENCES") == (None, "REFERENCES")
    # "A" without the dot is a word, not a number -- the fallback's own rule.
    assert split_heading_number("A Survey of Loop Filters") == (None, "A Survey of Loop Filters")
    # A title that ends in a full stop is a sentence, not a heading.
    assert split_heading_number("1 See figure below.") == (None, "1 See figure below.")


# --------------------------------------------------------------------------- #
# markdown -> (pages, sections)
# --------------------------------------------------------------------------- #

FRONT_MATTER = "Jane Doe, John Roe"
PAGE_ONE = f"""# A Paper Title

{FRONT_MATTER}

## Abstract

This paper does something useful.

## I. INTRODUCTION

<!-- page-break -->"""
PAGE_TWO = f"""## I. INTRODUCTION

<!-- table (structure unavailable in fallback) -->
Method | Result

Motor designs are compared here.
"""

TWO_PAGE_PAPER = PAGE_ONE + "\n" + PAGE_TWO


def test_front_matter_and_every_heading_open_a_section() -> None:
    pages, sections = pages_and_sections_from_markdown(bundle_for(TWO_PAGE_PAPER))
    assert len(pages) == 2
    # ``Section.label`` is "number title" -- the dot in "I. INTRODUCTION" is the
    # markdown's punctuation, and the fallback's own labels never carried it.
    assert [section.label for section in sections] == [
        "A Paper Title",
        "Abstract",
        "I INTRODUCTION",
    ]
    assert sections[0].paragraphs == [(1, FRONT_MATTER)]
    assert sections[1].paragraphs == [(1, "This paper does something useful.")]


def test_paragraph_keeps_the_page_it_started_on() -> None:
    _, sections = pages_and_sections_from_markdown(bundle_for(TWO_PAGE_PAPER))
    intro = sections[-1]
    assert intro.page_start == intro.page_end == 2
    # Two blocks, both attributed to the page they were printed on.
    assert [page for page, _ in intro.paragraphs] == [2, 2]
    assert intro.paragraphs[0][1] == "Method | Result"
    assert intro.paragraphs[1][1] == "Motor designs are compared here."


def test_dialect_comments_never_become_text() -> None:
    _, sections = pages_and_sections_from_markdown(bundle_for(TWO_PAGE_PAPER))
    text = "\n".join(text for section in sections for _, text in section.paragraphs)
    assert "<!--" not in text
    assert PAGE_BREAK not in text
    # The table rows themselves are content and survive the fallback's marker.
    assert "Method | Result" in text


def test_a_heading_without_a_body_is_dropped() -> None:
    markdown = "## Abstract\n\nText.\n\n## REFERENCES\n\n## APPENDIX\n\nMore.\n"
    _, sections = pages_and_sections_from_markdown(bundle_for(markdown))
    assert [section.label for section in sections] == ["Abstract", "APPENDIX"]


def test_a_document_without_headings_keeps_one_body_section() -> None:
    markdown = "First paragraph.\n\nSecond paragraph.\n"
    _, sections = pages_and_sections_from_markdown(bundle_for(markdown))
    assert [section.title for section in sections] == [SECTION_BODY]
    assert [text for _, text in sections[0].paragraphs] == [
        "First paragraph.",
        "Second paragraph.",
    ]


def test_pages_are_the_markdown_slices_of_their_own_spans() -> None:
    pages, _ = pages_and_sections_from_markdown(bundle_for(TWO_PAGE_PAPER))
    assert [page.page for page in pages] == [1, 2]
    assert PAGE_BREAK not in pages[1].text
    assert "Motor designs are compared here." in pages[1].text


def test_wrapped_lines_are_stitched_like_the_fallback_does() -> None:
    markdown = "## I. INTRODUCTION\n\nWe present a motor that sup-\nports dual mode operation\non one die.\n"
    _, sections = pages_and_sections_from_markdown(bundle_for(markdown))
    assert sections[0].paragraphs == [
        (1, "We present a motor that supports dual mode operation on one die.")
    ]


# --------------------------------------------------------------------------- #
# round trip parity with the fallback path
# --------------------------------------------------------------------------- #

FALLBACK_PAGES = [
    PageText(
        page=1,
        text="\n".join(
            [
                "LOW POWER SRAM DESIGN",
                "Jane Doe",
                "",
                "Abstract—This paper describes a low power SRAM.",
                "",
                "I. INTRODUCTION",
                "SRAM is everywhere and it keeps scaling down.",
            ]
        ),
    ),
    PageText(
        page=2,
        text="\n".join(
            [
                "II. CIRCUIT TECHNIQUES",
                "We cut the bitline swing in half to save energy.",
                "",
                "III. RESULTS",
                "The measured power dropped by 40 percent.",
            ]
        ),
    ),
    PageText(
        page=3,
        text="\n".join(
            [
                "IV. CONCLUSION",
                "Low power SRAM design still has room to improve.",
            ]
        ),
    ),
]


def test_round_trip_through_markdown_reproduces_the_fallback_sections() -> None:
    """The pypdf path must not change shape just because chunking moved to markdown."""
    original = merge_short_sections(detect_sections(FALLBACK_PAGES))
    bundle = render_markdown(FALLBACK_PAGES, backend="pypdf")
    _, from_markdown = pages_and_sections_from_markdown(bundle)
    # The adapter returns what the markdown says; the merge the pipeline applies
    # (inside ``chunk_markdown``) is what brings it back to the fallback's shape.
    rebuilt = merge_short_sections(from_markdown)

    assert [section.label for section in rebuilt] == [section.label for section in original]
    assert [len(section.paragraphs) for section in rebuilt] == [
        len(section.paragraphs) for section in original
    ]
    assert [text for section in rebuilt for _, text in section.paragraphs] == [
        text for section in original for _, text in section.paragraphs
    ]
    assert [section.page_start for section in rebuilt] == [
        section.page_start for section in original
    ]


def test_round_trip_chunks_match_the_pages_path() -> None:
    """Both entries agree on the same document: same count, sections and pages."""
    from app.parsing.chunking import chunk_document

    sections = merge_short_sections(detect_sections(FALLBACK_PAGES))
    from_pages = chunk_document(list(FALLBACK_PAGES), sections)
    from_markdown = chunk_markdown(render_markdown(FALLBACK_PAGES, backend="pypdf"))

    assert [chunk.text for chunk in from_markdown] == [chunk.text for chunk in from_pages]
    assert [chunk.section for chunk in from_markdown] == [
        chunk.section for chunk in from_pages
    ]
    assert [(chunk.page_start, chunk.page_end) for chunk in from_markdown] == [
        (chunk.page_start, chunk.page_end) for chunk in from_pages
    ]


# --------------------------------------------------------------------------- #
# chunk_markdown
# --------------------------------------------------------------------------- #


def test_chunk_markdown_stays_inside_sections_and_respects_the_cap() -> None:
    long_body = " ".join(f"Sentence number {index} about the design." for index in range(200))
    markdown = f"## I. INTRODUCTION\n\n{long_body}\n\n## II. RESULTS\n\nShort result.\n"
    chunks = chunk_markdown(bundle_for(markdown))
    assert len(chunks) > 1
    assert {chunk.section for chunk in chunks} == {"I INTRODUCTION", "II RESULTS"}
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.token_count <= MAX_TOKENS for chunk in chunks)
    assert all(chunk.page_start <= chunk.page_end >= 1 for chunk in chunks)


def test_chunk_markdown_indexes_chunks_across_sections() -> None:
    body = " ".join(f"Line {index}." for index in range(120))
    markdown = f"## I. ONE\n\n{body}\n\n## II. TWO\n\n{body}\n"
    chunks = chunk_markdown(bundle_for(markdown))
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    sections = [chunk.section for chunk in chunks]
    assert sections == sorted(sections, key=lambda name: 0 if name == "I. ONE" else 1)


def test_chunk_markdown_on_an_empty_bundle_returns_nothing() -> None:
    empty = ParseBundle(
        markdown="", page_count=0, spans=[], backend="docling", parser_version="test"
    )
    assert chunk_markdown(empty) == []


def test_chunk_markdown_rebuilds_spans_when_the_bundle_has_none() -> None:
    markdown = f"## I. ONE\n\nFirst page text.\n\n{PAGE_BREAK}\n\nSecond page text.\n"
    bundle = ParseBundle(
        markdown=markdown, page_count=2, spans=[], backend="pypdf", parser_version="test"
    )
    chunks = chunk_markdown(bundle)
    assert chunks
    assert max(chunk.page_end for chunk in chunks) == 2
