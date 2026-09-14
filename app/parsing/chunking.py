"""Paragraph-aware chunking (MVP-SPEC section 6).

Chunks never span sections, target ``target_tokens`` estimated with
``max(1, len(text) // 4)`` and are hard-capped at :data:`MAX_TOKENS` so the
512-token embedding model never truncates. Long sections are split on
paragraph boundaries with a trailing ``overlap_tokens`` window carried over
from the previous chunk; a single oversized paragraph falls back to a
character window so the cap always holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.parsing.pdf import PageText
from app.parsing.structure import SECTION_BODY, Section

logger = get_logger(__name__)

CHARS_PER_TOKEN = 4
MAX_TOKENS = 450
DEFAULT_TARGET_TOKENS = 400
DEFAULT_OVERLAP_TOKENS = 48
PARAGRAPH_SEPARATOR = "\n\n"
MIN_CHUNK_CHARS = 1


def estimate_tokens(text: str) -> int:
    """Estimate tokens as ``max(1, len(text) // 4)`` (MVP-SPEC section 6)."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def _tokens_to_chars(tokens: int) -> int:
    return max(1, tokens * CHARS_PER_TOKEN)


@dataclass(slots=True)
class Chunk:
    """One embeddable chunk of a section."""

    chunk_index: int
    text: str
    page_start: int
    page_end: int
    section: str | None = None
    section_title: str | None = None
    token_count: int = 0
    char_count: int = 0
    is_overlap: bool = False
    _spans: list[tuple[int, int, str]] = field(default_factory=list, repr=False)


@dataclass(slots=True)
class _Piece:
    """Carry-over unit: a paragraph slice with the page it belongs to."""

    text: str
    page: int


@dataclass(slots=True)
class _Pending:
    text: str
    page_start: int
    page_end: int
    spans: list[tuple[int, int, str]]


def _section_pieces(section: Section) -> list[_Piece]:
    pieces: list[_Piece] = []
    for page, paragraph in section.paragraphs:
        text = paragraph.strip()
        if text:
            pieces.append(_Piece(text=text, page=page))
    return pieces


def _split_oversized_piece(piece: _Piece, max_chars: int) -> list[_Piece]:
    """Split a paragraph that alone exceeds ``max_chars`` into char windows.

    Windows are cut on a sentence or word boundary where possible and are sized
    so the chunk assembler still has room to prepend its overlap carry-over.
    """
    if len(piece.text) <= max_chars:
        return [piece]
    text = piece.text
    windows: list[_Piece] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(length, start + max_chars)
        window = text[start:end]
        if end < length:
            cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "), window.rfind("\n"))
            if cut < max_chars // 2:
                cut = window.rfind(" ")
            if cut > max_chars // 2:
                window = window[: cut + 1]
        trimmed = window.strip()
        if trimmed:
            windows.append(_Piece(text=trimmed, page=piece.page))
        advance = len(window)
        start += advance if advance > 0 else max_chars
    return windows


def _overlap_suffix(text: str, overlap_tokens: int) -> str:
    limit = _tokens_to_chars(overlap_tokens)
    if limit <= 0 or len(text) <= limit:
        return text if overlap_tokens > 0 else ""
    tail = text[-limit:]
    for separator in ("\n", ". ", " "):
        index = tail.find(separator)
        if index != -1:
            trimmed = tail[index + len(separator) :].strip()
            if trimmed:
                return trimmed
    return tail.strip()


def _finalize(pending: _Pending, section: Section) -> Chunk | None:
    text = pending.text.strip(MIN_CHUNK_CHARS * "\n").strip()
    if len(text) < MIN_CHUNK_CHARS:
        return None
    pages = [page for page, _, _ in pending.spans] or [pending.page_start]
    page_start = min(pages)
    page_end = max(pages)
    return Chunk(
        chunk_index=-1,
        text=text,
        page_start=page_start,
        page_end=page_end,
        section=section.label,
        section_title=section.title,
        token_count=estimate_tokens(text),
        char_count=len(text),
        is_overlap=False,
        _spans=list(pending.spans),
    )


def _chunk_section(
    section: Section,
    *,
    target_tokens: int,
    overlap_tokens: int,
    max_tokens: int,
    next_index: int,
) -> list[Chunk]:
    pieces = _section_pieces(section)
    if not pieces:
        return []
    max_chars = _tokens_to_chars(max_tokens)
    target_chars = _tokens_to_chars(target_tokens)
    overlap_chars = _tokens_to_chars(overlap_tokens)
    # Leave room for the overlap carry-over inside the hard cap, otherwise an
    # oversized paragraph could never be overlapped at all.
    window_chars = max(1, min(target_chars, max_chars - overlap_chars))
    expanded: list[_Piece] = []
    for piece in pieces:
        expanded.extend(_split_oversized_piece(piece, window_chars))

    chunks: list[Chunk] = []
    pending: _Pending | None = None
    overlap_carry = ""

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        chunk = _finalize(pending, section)
        pending = None
        if chunk is not None:
            chunks.append(chunk)

    for piece in expanded:
        if pending is None:
            prefix = f"{overlap_carry}{PARAGRAPH_SEPARATOR}" if overlap_carry else ""
            overlap_carry = ""
            pending = _Pending(
                text=f"{prefix}{piece.text}",
                page_start=piece.page,
                page_end=piece.page,
                spans=[(piece.page, len(piece.text), piece.text)],
            )
            continue

        joined = f"{pending.text}{PARAGRAPH_SEPARATOR}{piece.text}"
        if len(joined) <= target_chars:
            pending.text = joined
            pending.spans.append((piece.page, len(piece.text), piece.text))
            pending.page_start = min(pending.page_start, piece.page)
            pending.page_end = max(pending.page_end, piece.page)
            continue

        flush()
        overlap_carry = _overlap_suffix(chunks[-1].text, overlap_tokens)
        prefix = f"{overlap_carry}{PARAGRAPH_SEPARATOR}" if overlap_carry else ""
        while overlap_carry and len(prefix) + len(piece.text) > max_chars:
            trimmed = overlap_carry[len(overlap_carry) // 2 :].strip()
            if trimmed == overlap_carry.strip():
                # Cannot shrink any further: drop the carry-over instead of
                # spinning forever on a one-character suffix.
                overlap_carry = ""
                prefix = ""
                break
            overlap_carry = trimmed
            prefix = f"{overlap_carry}{PARAGRAPH_SEPARATOR}" if overlap_carry else ""
        pending = _Pending(
            text=f"{prefix}{piece.text}",
            page_start=piece.page,
            page_end=piece.page,
            spans=[(piece.page, len(piece.text), piece.text)],
        )
        overlap_carry = ""
    flush()

    for chunk in chunks:
        chunk.chunk_index = next_index
        next_index += 1
    return chunks


def chunk_document(
    pages: list[PageText],
    sections: list[Section],
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    *,
    max_tokens: int = MAX_TOKENS,
) -> list[Chunk]:
    """Chunk ``pages`` using ``sections``; never crosses a section boundary.

    ``sections`` may be empty (or cover only part of the document): remaining
    text is chunked as ``Body`` so no content is dropped.
    """
    if not pages:
        return []
    if target_tokens <= 0:
        raise ValueError("target_tokens must be positive")
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens must not be negative")
    if overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens must be smaller than target_tokens")
    if max_tokens <= 0 or max_tokens < target_tokens:
        raise ValueError("max_tokens must be positive and >= target_tokens")

    effective_sections = [section for section in sections if _section_pieces(section)]
    if not effective_sections:
        body = Section(
            title=SECTION_BODY,
            page_start=pages[0].page,
            page_end=pages[-1].page,
            paragraphs=[(page.page, page.text) for page in pages if page.text.strip()],
        )
        effective_sections = [body]

    chunks: list[Chunk] = []
    for section in effective_sections:
        chunks.extend(
            _chunk_section(
                section,
                target_tokens=target_tokens,
                overlap_tokens=overlap_tokens,
                max_tokens=max_tokens,
                next_index=len(chunks),
            )
        )
    return chunks
