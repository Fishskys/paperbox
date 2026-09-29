"""Paragraph-aware chunking (MVP-SPEC section 6).

Chunks never span sections, target ``target_tokens`` estimated with
``max(1, len(text) // 4)`` and are hard-capped at :data:`MAX_TOKENS` so the
512-token embedding model never truncates. Long sections are split on
paragraph boundaries with a trailing ``overlap_tokens`` window carried over
from the previous chunk; a single oversized paragraph falls back to a
character window so the cap always holds.

Two boundary policies share this code path:

``length`` (default, and the only behaviour when ``embed_fn`` is ``None``)
    A chunk grows until the next paragraph would push it past ``target_tokens``.
``semantic`` (plan T7.2, ``CHUNK_MODE=semantic``)
    Sentences are embedded with the *retrieval* model and the section is cut
    where the cosine similarity between neighbouring sentences dips, so a
    boundary lands on a topic change instead of on an arbitrary character
    count. Every ``length`` constraint still applies: the section boundary,
    the overlap window and the hard cap all win over a semantic dip, and a run
    of dips cannot produce chunks below ``semantic_min_tokens``. If embedding
    fails the section falls back to ``length`` and -- when the caller passed one
    -- reports the fact through ``on_degrade`` (plan T7.3), which is how a paper
    ends up in the degradation ledger instead of only in the log.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.parsing.markdown import ParseBundle, pages_and_sections_from_markdown
from app.parsing.pdf import PageText
from app.parsing.structure import SECTION_BODY, Section, merge_short_sections

logger = get_logger(__name__)

CHARS_PER_TOKEN = 4
MAX_TOKENS = 450
DEFAULT_TARGET_TOKENS = 400
DEFAULT_OVERLAP_TOKENS = 48
PARAGRAPH_SEPARATOR = "\n\n"
MIN_CHUNK_CHARS = 1
#: ``CHUNK_MODE`` values; ``length`` is the default so an unset switch keeps the
#: pre-T7.2 behaviour bit for bit.
CHUNK_MODE_LENGTH = "length"
CHUNK_MODE_SEMANTIC = "semantic"
CHUNK_MODES = frozenset({CHUNK_MODE_LENGTH, CHUNK_MODE_SEMANTIC})

#: Embedding callable injected into :func:`chunk_document`. It must return one
#: vector per input text, in order (``embedding_service.embed_texts`` matches).
EmbedFn = Callable[[Sequence[str]], Sequence[Sequence[float]]]

#: Cosine similarity below which a boundary is a candidate. ``0.0`` disables
#: every candidate, so nothing is cut.
SEMANTIC_SIMILARITY_THRESHOLD = 0.80
#: A dip must be the minimum of its ``2 * SEMANTIC_DIP_WINDOW + 1`` neighbours
#: to count, which keeps a generally-low section from being carved up.
SEMANTIC_DIP_WINDOW = 1
#: A semantic boundary is only honoured once the pending chunk holds at least
#: this many tokens; below it the dip is skipped (dips are cheap, chunks are
#: not). Half of ``DEFAULT_TARGET_TOKENS``.
SEMANTIC_MIN_TOKENS = 200

#: Degradation ledger vocabulary for this module (plan T7.3). The stage string is
#: validated against ``degradation_service.STAGES`` by the sink, so a typo here
#: fails loudly instead of inventing a stage.
DEGRADE_STAGE = "chunking"
#: Reported when a section could not be embedded and falls back to ``length``.
DEGRADE_SEMANTIC_FALLBACK = "semantic_fallback"

#: ``(stage, code, detail) -> None``; the chunker only needs the callable, so it
#: stays import-free of the service layer (and of a database in tests).
DegradeSink = Callable[[str, str, dict[str, Any]], None]

#: Sentence terminators; an ASCII terminator only ends a sentence when it is
#: followed by whitespace or the end of the text.
_SENTENCE_ENDINGS = ".!?。！？"
#: Terminators that end a sentence on their own (CJK text has no space after
#: them, so requiring whitespace would merge a whole Chinese paragraph).
_WIDE_SENTENCE_ENDINGS = "。！？"
#: Tokens that must not be treated as a sentence end when followed by a period
#: ("et al.", "Fig. 3", "e.g. the model").
_ABBREVIATIONS = frozenset(
    {
        "al",
        "cf",
        "e.g",
        "eq",
        "etc",
        "fig",
        "i.e",
        "no",
        "ref",
        "sec",
        "tab",
        "vs",
    }
)



def estimate_tokens(text: str) -> int:
    """Estimate tokens as ``max(1, len(text) // 4)`` (MVP-SPEC section 6)."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def _tokens_to_chars(tokens: int) -> int:
    return max(1, tokens * CHARS_PER_TOKEN)


def split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences, terminators kept.

    Deliberately simple: the corpus is overwhelmingly English and the pieces
    only have to be *reasonable* probes for an embedding comparison, not
    linguistically perfect. Newlines always end a sentence (headings, titles),
    an abbreviation or an initial ("et al.", "Fig. 5", "J. Smith") does not,
    and a terminator followed by a digit does not either ("Eq. 3").
    """
    sentences: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        buffer.append(char)
        index += 1
        if char == "\n":
            sentence = "".join(buffer).strip()
            if sentence:
                sentences.append(sentence)
            buffer = []
            continue
        if char not in _SENTENCE_ENDINGS:
            continue
        wide = char in _WIDE_SENTENCE_ENDINGS
        if not wide and index < length and not text[index].isspace():
            continue
        # Look at the token right before the terminator: "al." and "Fig." are
        # not ends, and neither is a single-letter initial.
        head = "".join(buffer[:-1]).strip()
        word = head.rsplit(" ", 1)[-1].strip("([\"'").lower() if head else ""
        if not wide and (word in _ABBREVIATIONS or len(word) == 1):
            continue
        following = index
        while following < length and text[following].isspace():
            following += 1
        if not wide and following < length and text[following].isdigit():
            continue
        sentence = "".join(buffer).strip()
        if sentence:
            sentences.append(sentence)
        buffer = []
    tail = "".join(buffer).strip()
    if tail:
        sentences.append(tail)
    return sentences


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two vectors; ``0.0`` when either is degenerate."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        af = float(a)
        bf = float(b)
        dot += af * bf
        left_norm += af * af
        right_norm += bf * bf
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def similarity_dips(
    vectors: Sequence[Sequence[float]],
    *,
    threshold: float = SEMANTIC_SIMILARITY_THRESHOLD,
    window: int = SEMANTIC_DIP_WINDOW,
) -> set[int]:
    """Return the indices ``i`` where a chunk boundary belongs *after* item ``i``.

    A position qualifies when its cosine similarity sits below ``threshold``
    **and** is the minimum of the surrounding ``2 * window + 1`` positions, so
    a section that merely has low similarity throughout is not carved up at
    every sentence. Ties count as dips; ``semantic_min_tokens`` bounds how
    often that can happen in practice.
    """
    if len(vectors) < 2:
        return set()
    similarities = [
        cosine_similarity(vectors[index], vectors[index + 1])
        for index in range(len(vectors) - 1)
    ]
    span = max(0, int(window))
    dips: set[int] = set()
    for index, similarity in enumerate(similarities):
        if similarity >= threshold:
            continue
        low = max(0, index - span)
        high = min(len(similarities), index + span + 1)
        if similarity <= min(similarities[low:high]):
            dips.add(index)
    return dips


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
    """Carry-over unit: a paragraph slice with the page it belongs to.

    ``break_before`` is set by :func:`_semantic_pieces` when the piece starts
    after a similarity dip: the assembler then prefers to flush there instead
    of holding a chunk open until it hits ``target_tokens``.
    """

    text: str
    page: int
    break_before: bool = False


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


def _report_fallback(
    on_degrade: DegradeSink | None,
    *,
    section: Section,
    sentences: int,
    detail: dict[str, Any],
) -> None:
    """Hand one fallback to the sink (a no-op without one)."""
    if on_degrade is None:
        return
    on_degrade(
        DEGRADE_STAGE,
        DEGRADE_SEMANTIC_FALLBACK,
        {"section": section.label, "sentences": sentences, **detail},
    )


def _semantic_pieces(
    section: Section,
    embed_fn: EmbedFn,
    *,
    target_chars: int,
    min_chars: int,
    threshold: float,
    window: int,
    on_degrade: DegradeSink | None = None,
) -> list[_Piece] | None:
    """Group the section's sentences into pieces cut at similarity dips.

    Returns ``None`` when there is nothing to decide (fewer than two
    sentences) or when embedding failed: the caller then keeps the plain
    paragraph stream under the length policy, so a broken embedding server
    degrades the chunking instead of failing the ingestion job. The failure is
    handed to ``on_degrade`` so it outlives the log line.
    """
    units: list[tuple[int, str, bool]] = []
    for page, paragraph in section.paragraphs:
        for index, sentence in enumerate(split_sentences(paragraph.strip())):
            units.append((page, sentence, index == 0))
    if len(units) < 2:
        return None

    try:
        vectors = embed_fn([sentence for _, sentence, _ in units])
    except Exception as exc:  # noqa: BLE001 - the job must survive this
        logger.warning(
            "semantic chunking fell back to length mode",
            extra={
                "extra_fields": {
                    "section": section.label,
                    "sentences": len(units),
                    "error": str(exc),
                }
            },
        )
        _report_fallback(
            on_degrade,
            section=section,
            sentences=len(units),
            detail={"error": f"{type(exc).__name__}: {exc}"},
        )
        return None
    if len(vectors) != len(units):
        logger.warning(
            "semantic chunking fell back to length mode",
            extra={
                "extra_fields": {
                    "section": section.label,
                    "sentences": len(units),
                    "vectors": len(vectors),
                }
            },
        )
        _report_fallback(
            on_degrade,
            section=section,
            sentences=len(units),
            detail={"vectors": len(vectors)},
        )
        return None

    dips = similarity_dips(vectors, threshold=threshold, window=window)

    pieces: list[_Piece] = []
    buffer: str = ""
    page_start = units[0][0]
    started_after_dip = False
    for index, (page, sentence, starts_paragraph) in enumerate(units):
        starts_after_dip = index > 0 and (index - 1) in dips
        too_long = len(buffer) + len(sentence) + 1 > target_chars
        if buffer and (
            (starts_after_dip and len(buffer) >= min_chars)
            or (too_long and len(buffer) >= min_chars)
        ):
            pieces.append(
                _Piece(text=buffer, page=page_start, break_before=started_after_dip)
            )
            buffer = ""
        if not buffer:
            page_start = page
            started_after_dip = starts_after_dip
            buffer = sentence
        else:
            # Paragraph breaks survive the regrouping: the sentences are joined
            # exactly like the paragraph stream would have been.
            separator = PARAGRAPH_SEPARATOR if starts_paragraph else " "
            buffer = f"{buffer}{separator}{sentence}"
    if buffer:
        pieces.append(
            _Piece(text=buffer, page=page_start, break_before=started_after_dip)
        )
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
            windows.append(
                _Piece(
                    text=trimmed,
                    page=piece.page,
                    # Only the first window starts where the piece started, so
                    # only it can carry "a dip sits right before me".
                    break_before=piece.break_before and not windows,
                )
            )
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
    embed_fn: EmbedFn | None = None,
    semantic_threshold: float = SEMANTIC_SIMILARITY_THRESHOLD,
    semantic_min_tokens: int = SEMANTIC_MIN_TOKENS,
    on_degrade: DegradeSink | None = None,
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
    # A dip below this many characters is ignored: chunks keep a floor size
    # even when the similarity signal is noisy.
    min_break_chars = _tokens_to_chars(semantic_min_tokens) if embed_fn is not None else 0
    if embed_fn is not None:
        semantic = _semantic_pieces(
            section,
            embed_fn,
            target_chars=window_chars,
            min_chars=min_break_chars,
            threshold=semantic_threshold,
            window=SEMANTIC_DIP_WINDOW,
            on_degrade=on_degrade,
        )
        if semantic is not None:
            pieces = semantic
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
        semantic_cut = piece.break_before and len(pending.text) >= min_break_chars
        if len(joined) <= target_chars and not semantic_cut:
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
    embed_fn: EmbedFn | None = None,
    semantic_threshold: float = SEMANTIC_SIMILARITY_THRESHOLD,
    semantic_min_tokens: int = SEMANTIC_MIN_TOKENS,
    on_degrade: DegradeSink | None = None,
) -> list[Chunk]:
    """Chunk ``pages`` using ``sections``; never crosses a section boundary.

    ``sections`` may be empty (or cover only part of the document): remaining
    text is chunked as ``Body`` so no content is dropped.

    ``embed_fn`` selects the boundary policy (plan T7.2): ``None`` keeps the
    length policy, a callable embeds each section's sentences and cuts where
    the similarity dips. Either way the section boundary, the overlap window
    and the ``max_tokens`` cap are honoured, and a section whose embedding call
    fails falls back to the length policy.

    ``on_degrade`` is the optional degradation sink (plan T7.3): it is called as
    ``(stage, code, detail)`` for every fallback, so a paper whose semantic
    chunking degraded can be found again once the embedding server is back.
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
    if not 0.0 < semantic_threshold <= 1.0:
        raise ValueError("semantic_threshold must be in (0, 1]")
    if semantic_min_tokens < 0:
        raise ValueError("semantic_min_tokens must not be negative")

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
                embed_fn=embed_fn,
                semantic_threshold=semantic_threshold,
                semantic_min_tokens=semantic_min_tokens,
                on_degrade=on_degrade,
            )
        )
    return chunks
def chunk_markdown(
    bundle: ParseBundle,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    *,
    max_tokens: int = MAX_TOKENS,
    embed_fn: EmbedFn | None = None,
    semantic_threshold: float = SEMANTIC_SIMILARITY_THRESHOLD,
    semantic_min_tokens: int = SEMANTIC_MIN_TOKENS,
    page_break: str | None = None,
    on_degrade: DegradeSink | None = None,
) -> list[Chunk]:
    """Chunk a parse bundle -- the entry the ingestion pipeline uses (plan 6.1 step 1).

    Both backends hand over the same markdown dialect, so the section and page
    grid is rebuilt from it (``markdown.pages_and_sections_from_markdown``) and
    the short-section merge the fallback has always applied
    (``structure.merge_short_sections``) runs on top. Everything downstream --
    never across a section, page spans, the overlap window, the ``MAX_TOKENS``
    cap, the semantic policy and the degradation sink -- is ``chunk_document``'s
    and therefore identical for docling and pypdf.

    Returns ``[]`` for an empty bundle, exactly like an empty ``pages`` list;
    the pipeline turns that into the ``NO_TEXT_LAYER`` failure.
    """
    pages, sections = pages_and_sections_from_markdown(
        bundle, page_break=page_break, on_degrade=on_degrade
    )
    return chunk_document(
        pages,
        merge_short_sections(sections),
        target_tokens,
        overlap_tokens,
        max_tokens=max_tokens,
        embed_fn=embed_fn,
        semantic_threshold=semantic_threshold,
        semantic_min_tokens=semantic_min_tokens,
        on_degrade=on_degrade,
    )
