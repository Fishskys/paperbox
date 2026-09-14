"""Unit tests for PDF parsing, section detection and chunking (MVP-SPEC §6)."""

from __future__ import annotations

import pytest

from app.parsing.chunking import (
    MAX_TOKENS,
    Chunk,
    chunk_document,
    estimate_tokens,
)
from app.parsing.pdf import PageText, normalize_page_text
from app.parsing.structure import Section, detect_sections

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def page(number: int, *lines: str) -> PageText:
    """Build a PageText whose lines are joined like a real text layer."""
    return PageText(page=number, text="\n".join(lines))


def paragraph(words: int, seed: str = "word") -> str:
    return " ".join(f"{seed}{index}" for index in range(words))


# --------------------------------------------------------------------------- #
# pdf.normalize_page_text
# --------------------------------------------------------------------------- #


def test_normalize_keeps_single_newlines() -> None:
    """Hard-wrapped lines must stay on separate lines for section detection."""
    raw = "1 Introduction\nRecurrent networks are old.\n2 Background\nSee above."
    assert normalize_page_text(raw) == raw


def test_normalize_keeps_paragraph_breaks_and_drops_blank_noise() -> None:
    raw = "First paragraph.\n\n\n\nSecond paragraph.   \n"
    assert normalize_page_text(raw) == "First paragraph.\n\nSecond paragraph."


def test_normalize_handles_windows_line_endings() -> None:
    assert normalize_page_text("a\r\nb\r\n") == "a\nb"


# --------------------------------------------------------------------------- #
# structure.detect_sections
# --------------------------------------------------------------------------- #


def test_detects_numbered_and_named_headings_in_order() -> None:
    pages = [
        page(
            1,
            "Attention Is All You Need",
            "Abstract",
            "We propose the Transformer.",
            "1 Introduction",
            "Recurrent models are sequential.",
            "2 Background",
            "See section 3.2.",
            "3 Model Architecture",
            "Encoder and decoder stacks.",
        )
    ]
    sections = detect_sections(pages)
    labels = [section.label for section in sections]
    assert "Abstract" in labels
    assert "1 Introduction" in labels
    assert "2 Background" in labels
    assert "3 Model Architecture" in labels
    # reading order is preserved
    assert labels.index("1 Introduction") < labels.index("2 Background")
    assert labels.index("2 Background") < labels.index("3 Model Architecture")


def test_subsection_headings_are_detected() -> None:
    pages = [page(2, "3.2 Attention", "Scaled dot product.", "3.2.1 Scaled Dot-Product Attention", "Details.")]
    labels = [section.label for section in detect_sections(pages)]
    assert "3.2 Attention" in labels
    assert "3.2.1 Scaled Dot-Product Attention" in labels


def test_table_rows_are_not_headings() -> None:
    pages = [page(9, "32 16 16 5.01 25.4", "4096 4.75 26.2 90", "6.3 English Constituency Parsing", "Details.")]
    labels = [section.label for section in detect_sections(pages)]
    assert labels == ["Body", "6.3 English Constituency Parsing"]


def test_lowercase_math_fragment_is_not_a_heading() -> None:
    pages = [page(4, "i ∈ Rdmodel×dv", "3.3 Position-wise Feed-Forward Networks", "Details.")]
    labels = [section.label for section in detect_sections(pages)]
    assert "3.3 Position-wise Feed-Forward Networks" in labels
    assert all(not label.startswith("i ") for label in labels)


def test_section_page_ranges_cover_document() -> None:
    pages = [
        page(1, "1 Introduction", "Intro text."),
        page(2, "2 Method", "Method text."),
        page(3, "Still method text on page three."),
    ]
    sections = {section.label: section for section in detect_sections(pages)}
    assert sections["1 Introduction"].page_start == 1
    assert sections["1 Introduction"].page_end == 1
    assert sections["2 Method"].page_start == 2
    assert sections["2 Method"].page_end == 3


def test_heading_less_document_falls_back_to_body() -> None:
    pages = [page(1, "just text"), page(2, "more text")]
    sections = detect_sections(pages)
    assert [section.title for section in sections] == ["Body"]


def test_repeated_running_header_is_not_a_section() -> None:
    pages = [page(1, "IEEE TRANSACTIONS", "IEEE TRANSACTIONS", "real body text", "1 Introduction", "intro body")]
    labels = [section.label for section in detect_sections(pages)]
    assert labels.count("IEEE TRANSACTIONS") == 0
    assert "1 Introduction" in labels


def test_detect_sections_without_pages_returns_empty() -> None:
    assert detect_sections([]) == []


# --------------------------------------------------------------------------- #
# chunking
# --------------------------------------------------------------------------- #


def test_chunks_never_span_sections() -> None:
    pages = [page(1, "1 Introduction", paragraph(400, "intro"), "2 Method", paragraph(400, "method"))]
    sections = detect_sections(pages)
    chunks = chunk_document(pages, sections)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk.section in {"1 Introduction", "2 Method"}
    intro_words = " ".join(c.text for c in chunks if c.section == "1 Introduction")
    assert "method" not in intro_words
    method_words = " ".join(c.text for c in chunks if c.section == "2 Method")
    assert "intro" not in method_words


def test_chunk_page_range_matches_source_pages() -> None:
    pages = [
        page(4, "3 Method", paragraph(120, "m")),
        page(5, paragraph(120, "m")),
        page(6, paragraph(120, "m")),
    ]
    sections = detect_sections(pages)
    chunks = chunk_document(pages, sections)
    assert chunks
    for chunk in chunks:
        assert 4 <= chunk.page_start <= chunk.page_end <= 6
    # chunks follow reading order without overlapping page ranges backwards
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))


def test_chunk_token_target_and_hard_cap() -> None:
    pages = [page(1, "1 Introduction", *[paragraph(150, f"p{i}") for i in range(12)])]
    sections = detect_sections(pages)
    chunks = chunk_document(pages, sections, target_tokens=400, overlap_tokens=48)
    assert len(chunks) >= 3
    for chunk in chunks:
        # never above the embedding model's hard cap
        assert chunk.token_count <= MAX_TOKENS
        # within +30% of the target (overlap carry-over can only add a little)
        assert chunk.token_count <= int(400 * 1.3)
    # the bulk of the chunks should be reasonably close to the target
    assert sum(1 for c in chunks if c.token_count >= 200) >= len(chunks) - 2


def test_long_section_is_split_with_overlap() -> None:
    words = " ".join(f"w{index:04d}" for index in range(1500))
    pages = [page(1, "1 Introduction", words)]
    sections = detect_sections(pages)
    chunks = chunk_document(pages, sections, target_tokens=400, overlap_tokens=64)
    assert len(chunks) >= 3
    # The overlap window is larger than a few words, so the start of chunk N+1
    # must reappear in the tail of chunk N.
    for previous, following in zip(chunks, chunks[1:]):
        tail = set(previous.text.split()[-80:])
        head = following.text.split()[:30]
        assert set(head) <= tail, "expected the overlap window to be carried over"


def test_chunks_cover_the_text_without_gaps() -> None:
    """Consecutive chunks must overlap and never skip content."""
    words = " ".join(f"w{index:04d}" for index in range(1200))
    pages = [page(1, "1 Introduction", words)]
    sections = detect_sections(pages)
    chunks = chunk_document(pages, sections, target_tokens=400, overlap_tokens=48)
    assert len(chunks) >= 3
    cursor = -1
    for chunk in chunks:
        tokens = chunk.text.split()
        first, last = int(tokens[0][1:]), int(tokens[-1][1:])
        if cursor >= 0:
            assert first <= cursor + 1, "chunks must not skip text"
        cursor = last
    assert cursor == 1199


def test_oversized_single_paragraph_is_char_windowed() -> None:
    pages = [page(1, "1 Introduction", paragraph(4000, "huge"))]
    sections = detect_sections(pages)
    chunks = chunk_document(pages, sections)
    assert len(chunks) >= 8
    for chunk in chunks:
        assert chunk.token_count <= MAX_TOKENS


def test_chunk_document_without_sections_uses_body() -> None:
    pages = [page(1, paragraph(300, "plain")), page(2, paragraph(300, "plain"))]
    chunks = chunk_document(pages, [])
    assert chunks
    assert all(chunk.section == "Body" or chunk.section == "Body" for chunk in chunks)
    assert all(chunk.page_start <= chunk.page_end for chunk in chunks)


def test_chunk_document_empty_input() -> None:
    assert chunk_document([], []) == []


def test_blank_pages_are_ignored() -> None:
    pages = [PageText(page=1, text="   "), page(2, "1 Introduction", paragraph(80))]
    sections = detect_sections(pages)
    chunks = chunk_document(pages, sections)
    assert len(chunks) == 1
    assert chunks[0].page_start == 2


def test_invalid_parameters_rejected() -> None:
    pages = [page(1, "1 Introduction", paragraph(50))]
    with pytest.raises(ValueError):
        chunk_document(pages, [], target_tokens=0)
    with pytest.raises(ValueError):
        chunk_document(pages, [], overlap_tokens=-1)
    with pytest.raises(ValueError):
        chunk_document(pages, [], target_tokens=100, overlap_tokens=100)
    with pytest.raises(ValueError):
        chunk_document(pages, [], target_tokens=400, max_tokens=100)


def test_estimate_tokens_matches_spec() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_chunk_dataclass_defaults() -> None:
    chunk = Chunk(chunk_index=0, text="hello", page_start=1, page_end=1)
    assert chunk.token_count == 0
    assert chunk.is_overlap is False


def test_section_label_combines_number_and_title() -> None:
    section = Section(title="Attention", page_start=1, page_end=1, number="3.2")
    assert section.label == "3.2 Attention"


def test_normalize_page_text_drops_control_characters() -> None:
    """Regression: NUL bytes made PostgreSQL reject whole chunk inserts.

    Two arXiv PDFs failed ingestion with
    ``DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes``
    during the corpus import; the page text is the single choke point.
    """
    dirty = "Abstract\x00 with NUL\x01\x02 and bell\x07 bytes\nSecond\tline"

    cleaned = normalize_page_text(dirty)

    for code in ("\x00", "\x01", "\x02", "\x07"):
        assert code not in cleaned
    assert "\t" in cleaned
    assert cleaned.splitlines()[0] == "Abstract with NUL and bell bytes"


def test_normalize_page_text_keeps_line_structure() -> None:
    cleaned = normalize_page_text("First\n\n\nSecond  \nThird")

    assert cleaned == "First\n\nSecond\nThird"
