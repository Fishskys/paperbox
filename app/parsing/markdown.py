"""The shared markdown dialect of both parser backends (plan §1.1 / §1.2, T5).

docling produces markdown natively; the pypdf fallback has to produce *the same
shape* so that chunking (the next phase) only ever sees one dialect. This module
holds that dialect in one place:

* :func:`normalize_markdown` -- the normalization both backends get applied to.
* :func:`render_markdown` -- the pypdf side: headings from the fallback's own
  rules, ``<!-- page-break -->`` between pages, table fallback blocks.
* :func:`page_spans_from_markdown` -- page markers -> character offsets. **Both
  backends call this one function**, which is what keeps their page metadata
  comparable.
* :func:`prepare_docling_markdown` -- the docling side's post-processing: clamp
  a stray 7th level (decision 12) and promote the paper title to ``#`` (decision
  8, see the note below).

Known, accepted differences (plan §1.2, decision 15) -- never pretend these are
equal: the fallback renders tables as text rows, has no LaTeX for formulas and
cannot see images, while docling emits GFM tables, ``$$…$$`` and placeholders.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.core.logging import get_logger
from app.parsing.pdf import PageText
from app.parsing.layout import (
    ORDER_UNVERIFIED_REASON,
    prepare_pages,
)
from app.parsing.structure import (
    KNOWN_HEADINGS,
    SECTION_BODY,
    PageBlock,
    Section,
    _append_paragraphs,
    page_blocks,
)

logger = get_logger(__name__)

#: Page boundary marker. Both backends emit exactly this string; the marker count
#: is ``pages - 1`` (measured for every paper in the T0/T2 corpus).
PAGE_BREAK_DEFAULT = "<!-- page-break -->"

#: Fallback table rendering: the rows survive as text, the structure does not.
TABLE_FALLBACK_MARKER = "<!-- table (structure unavailable in fallback) -->"

#: ``ParseBundle.degraded_reason`` fragments.
DEGRADED_NO_FORMULA = "no formula latex"
DEGRADED_TABLE = "table structure"
DEGRADED_ORDER = ORDER_UNVERIFIED_REASON

_MAX_HEADING_LEVEL = 6
_NUMBERED_TITLE = re.compile(r"^(?:[IVX]+|\d+)(?:[.)]|\s)")


@dataclass(slots=True)
class PageSpan:
    """One page's slice of the markdown, 1-based page, half-open char range.

    ``char_start`` is inclusive and ``char_end`` exclusive, so
    ``markdown[span.char_start:span.char_end]`` is exactly that page's text --
    the plan's "inclusive" wording was written before the slicing use case was
    concrete; half-open slices are what chunking needs.
    """

    page: int
    char_start: int
    char_end: int


@dataclass(slots=True)
class ParseBundle:
    """What a parser returns to the pipeline (not persisted as such)."""

    markdown: str
    page_count: int
    spans: list[PageSpan]
    backend: str
    parser_version: str
    degraded_reason: str | None = None
    timings: dict[str, float] = field(default_factory=dict)
    headings: list[tuple[int, str]] = field(default_factory=list)
    #: The backend's own structured document (docling's ``DoclingDocument``),
    #: kept so T7 can store it next to the markdown for replay/A-B runs. The
    #: pypdf path has no equivalent and leaves it ``None``.
    raw_json: dict[str, Any] | None = None
    #: True when the bundle was replayed from the parse-artifact cache rather
    #: than produced by a backend (plan T7.1); the timings then describe the
    #: original parse plus ``cache_load_s``.
    cache_hit: bool = False


def normalize_markdown(text: str) -> str:
    """Apply the dialect's normalization (plan §1.2, last row).

    LF line endings, no trailing whitespace, runs of blank lines collapsed to
    one, runs of spaces collapsed to one, ``&gt;``-style escapes decoded, exotic
    spaces turned into plain ASCII spaces. Both backends go through this, so
    ``normalize_markdown`` must stay idempotent.
    """
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = html.unescape(normalized)
    for exotic, plain in (
        ("\u00a0", " "),  # no-break space
        ("\u2007", " "),  # figure space
        ("\u202f", " "),  # narrow no-break space
        ("\u2009", " "),  # thin space
        ("\u200b", ""),  # zero-width space
        ("\ufeff", ""),  # BOM
    ):
        normalized = normalized.replace(exotic, plain)

    lines: list[str] = []
    pending_blank = False
    for raw in normalized.split("\n"):
        line = re.sub(r"[ \t]{2,}", " ", raw.strip())
        if line:
            if pending_blank and lines:
                lines.append("")
            lines.append(line)
            pending_blank = False
        else:
            pending_blank = True
    return "\n".join(lines)


def heading_level(number: str | None, title: str) -> int | None:
    """Level for a detected heading, using the fallback's own rules (decision 14).

    The fallback deliberately does **not** learn docling's inferred hierarchy:
    ``2.1.3`` is 3 levels deep, ``III``/``A`` and the unnumbered known headings
    (``Abstract``, ``References``) are 1, and the placeholder ``Body`` section is
    not a heading at all. Levels are clamped to 1-6 (decision 12).
    """
    if title == SECTION_BODY:
        return None
    if not number:
        return 1
    if number[0].isdigit():
        return max(1, min(_MAX_HEADING_LEVEL, number.count(".") + 1))
    return 1


def _looks_like_section_heading(title: str) -> bool:
    """True for anything that introduces a section rather than the paper itself."""
    cleaned = title.strip().rstrip(".:").casefold()
    if cleaned in KNOWN_HEADINGS:
        return True
    return bool(_NUMBERED_TITLE.match(title.strip()))


def page_spans_from_markdown(
    markdown: str, *, page_break: str = PAGE_BREAK_DEFAULT
) -> tuple[int, list[PageSpan]]:
    """Split ``markdown`` at the page markers: ``(page_count, spans)``.

    Shared by both backends on purpose -- the page metadata of a docling parse
    and a pypdf parse are only comparable because they come out of the same
    function.
    """
    marker = (page_break or "").strip()
    if not markdown:
        return 0, []
    spans: list[PageSpan] = []
    page = 1
    start = 0
    offset = 0
    for line in markdown.split("\n"):
        line_start = offset
        offset += len(line) + 1  # +1 for the newline this split consumed
        if marker and line.strip() == marker:
            spans.append(PageSpan(page=page, char_start=start, char_end=line_start))
            page += 1
            start = offset
    spans.append(PageSpan(page=page, char_start=start, char_end=len(markdown)))
    return page, spans


def clamp_heading_levels(markdown: str, *, max_level: int = _MAX_HEADING_LEVEL) -> str:
    """Pull headings deeper than ``max_level`` up to it.

    A 7th level is a docling parsing error, not a real hierarchy (decision 12);
    clamping keeps the level usable as a chunk prefix without inventing content.
    """
    out: list[str] = []
    for line in markdown.split("\n"):
        match = re.match(r"^(#{1,})(\s+.*)$", line)
        if match and len(match.group(1)) > max_level:
            out.append("#" * max_level + match.group(2))
        else:
            out.append(line)
    return "\n".join(out)


def promote_paper_title(
    markdown: str, *, page_break: str = PAGE_BREAK_DEFAULT
) -> str:
    """Promote the paper title from ``##`` to ``#`` on the docling side.

    docling-serve 1.35.0's markdown export never emits ``#`` (measured, plan
    §0.6①): the paper title and a first-level section both come out as ``##``.
    The agreed rule (decision 8, approved 2026-09-29) is that **the first ``##``
    located before ``Abstract``/``I.`` is the paper title** -- so scanning stops
    at the first line of real content, and a document whose first heading is
    already a section (``## Abstract``, ``## 1 Introduction``) is left alone.

    The fallback side needs no such rule: ``detect_sections`` may or may not find
    the title as a heading, but it never emits a wrong ``#``.
    """
    lines = markdown.split("\n")
    marker = (page_break or "").strip()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or (marker and stripped == marker) or stripped.startswith("<!--"):
            continue
        match = re.match(r"^(#{1,6})\s+(.*\S)\s*$", stripped)
        if match is None:
            # Real content before any heading: there is no title to promote.
            return markdown
        level = len(match.group(1))
        title = match.group(2).strip()
        if level != 2 or _looks_like_section_heading(title):
            return markdown
        lines[index] = f"# {title}"
        return "\n".join(lines)
    return markdown


# --------------------------------------------------------------------------- #
# markdown -> structure (the way back)
# --------------------------------------------------------------------------- #

#: A markdown heading line: one to six ``#``, a space, a non-empty title.
_HEADING_LINE = re.compile(r"^(#{1,6})\s+(?P<title>\S.*?)\s*$")
#: Numbered heading in the fallback's own vocabulary: the same tokens
#: ``structure._NUMBERED_HEADING`` accepts, so a pypdf round trip recovers the
#: section number the fallback had already found.
_NUMERIC_HEADING = re.compile(
    r"^(?P<number>\d+(?:\.\d+)*|[IVXLC]+(?:-[A-Za-z])?)[.)]?\s+(?P<title>\S.*)$"
)
#: Sub-section letters ("A. Real-Time Monitor") only exist in docling's inferred
#: hierarchy; demanding the dot is what keeps "A Survey of X" in one piece.
_LETTER_HEADING = re.compile(r"^(?P<number>[A-Z])[.)]\s+(?P<title>\S.*)$")
#: Comment-only lines carry the dialect's structure (page breaks, table
#: placeholders), never content -- they do not become chunk text.
_COMMENT_LINE = re.compile(r"^<!--.*-->\s*$")


def split_heading_number(title: str) -> tuple[str | None, str]:
    """Split ``"I. INTRODUCTION"`` into ``("I", "INTRODUCTION")``.

    An unnumbered heading (``Abstract``, ``REFERENCES``) comes back unchanged as
    ``(None, title)``, and so does a title whose first token is not a numbering
    token (``A Survey of Loop Filters``) or whose remainder ends in a full stop
    -- the fallback's own rule (``structure._match_heading``), so section labels
    survive the round trip through markdown.
    """
    cleaned = title.strip()
    for pattern in (_NUMERIC_HEADING, _LETTER_HEADING):
        match = pattern.match(cleaned)
        if match is None:
            continue
        rest = match.group("title").strip()
        if rest and not rest.endswith("."):
            return match.group("number"), rest
        return None, cleaned
    return None, cleaned


def pages_and_sections_from_markdown(
    bundle: ParseBundle, *, page_break: str | None = None
) -> tuple[list[PageText], list[Section]]:
    """Rebuild the ``(pages, sections)`` pair ``chunk_document`` expects.

    The dialect carries everything section detection needs -- ``#`` levels for
    the hierarchy, the page marker for the page grid -- so **both** backends
    feed the same chunker: switching backends changes the text, not the chunking
    policy (plan §6.1 step 1). ``pages`` are the markdown's own page slices, so a
    chunk's ``page_start``/``page_end`` still come from a page span.

    The result mirrors ``structure.detect_sections`` on the fallback's pages:
    text before the first heading lands in a ``Body`` section, every heading
    starts a new one, empty sections are dropped (a heading-less document keeps
    that single ``Body``), a paragraph never spans a page, and paragraphs are
    split by the same helper, so hard-wrapped lines and dangling hyphens are
    stitched back together identically.
    """
    markdown = bundle.markdown or ""
    marker = (page_break or PAGE_BREAK_DEFAULT).strip()
    spans = list(bundle.spans) or page_spans_from_markdown(markdown, page_break=marker)[1]
    pages = [
        PageText(page=span.page, text=markdown[span.char_start : span.char_end])
        for span in spans
    ]
    if not pages:
        return [], []

    sections: list[Section] = []
    current = Section(title=SECTION_BODY, page_start=pages[0].page, page_end=pages[0].page)
    buffer: list[str] = []
    buffer_page: int | None = None
    touched = False

    def flush() -> None:
        """Give the buffered block to the current section, with its own page."""
        nonlocal buffer, buffer_page, touched
        if buffer and buffer_page is not None:
            _append_paragraphs(current, buffer_page, "\n".join(buffer).strip("\n"))
            touched = True
            current.page_end = max(current.page_end, buffer_page)
        buffer = []
        buffer_page = None

    span_index = 0
    offset = 0
    for raw_line in markdown.split("\n"):
        line_start = offset
        offset += len(raw_line) + 1  # +1 for the newline that split() consumed
        while span_index + 1 < len(spans) and line_start >= spans[span_index].char_end:
            span_index += 1
        page_number = spans[span_index].page
        stripped = raw_line.strip()
        if not stripped or stripped == marker:
            # A blank line or a page boundary closes the block; the block keeps
            # the page it started on, so a paragraph never spans two pages.
            flush()
            continue
        if _COMMENT_LINE.match(stripped):
            continue
        heading = _HEADING_LINE.match(stripped)
        if heading is None:
            if buffer_page is None:
                buffer_page = page_number
            elif buffer_page != page_number:
                flush()
                buffer_page = page_number
            buffer.append(raw_line)
            continue
        flush()
        if touched:
            current.page_end = max(current.page_end, page_number)
        sections.append(current)
        number, title = split_heading_number(heading.group("title"))
        current = Section(
            title=title,
            number=number,
            page_start=page_number,
            page_end=page_number,
        )
        touched = False
    flush()
    sections.append(current)

    for section in sections:
        section.paragraphs = [(page, text) for page, text in section.paragraphs if text]
    # Same rule as ``detect_sections``: drop the placeholder that only exists
    # because the document opens with a heading, keep it when nothing is left.
    populated = [section for section in sections if section.paragraphs]
    return pages, (populated or sections[:1])


def prepare_docling_markdown(
    markdown: str, *, page_break: str = PAGE_BREAK_DEFAULT
) -> str:
    """Normalize + clamp + title promotion, in that order (docling side)."""
    return normalize_markdown(
        promote_paper_title(clamp_heading_levels(markdown), page_break=page_break)
    )


def _render_block(block: PageBlock, out: list[str], headings: list[tuple[int, str]]) -> bool:
    """Append one page block to ``out``; returns True when it was a table."""
    if block.kind == "heading":
        level = heading_level(block.number, block.title or block.text)
        if level is None:
            return False
        out.extend(["", "#" * level + " " + block.text, ""])
        headings.append((level, block.text))
        return False
    if block.kind == "table":
        out.extend(["", TABLE_FALLBACK_MARKER, *block.lines, ""])
        return True
    out.extend(["", block.text, ""])
    return False


def render_markdown(
    pages: Sequence[PageText],
    *,
    pdf_bytes: bytes | None = None,
    page_break: str = PAGE_BREAK_DEFAULT,
    backend: str = "pypdf",
    parser_version: str = "",
    timings: dict[str, float] | None = None,
    extra_degraded: Sequence[str] = (),
) -> ParseBundle:
    """Render the pypdf path as the shared markdown dialect.

    Pass ``pdf_bytes`` to enable the two repairs that need coordinates or raw
    lines: two-column reading order (decision 14, ``app.parsing.layout``) and
    running header/footer removal. Without it the pages are rendered as given.

    ``degraded_reason`` is never ``None`` here: this *is* the degraded path, and
    the caller must be able to see why a paper is thinner than it would be with
    docling ("no formula latex", "table structure" when rows were rendered as
    text, "reading order not verified" when a page kept pypdf's order).
    """
    parsed_pages: list[PageText] = list(pages)
    measured: dict[str, float] = dict(timings or {})
    order_unverified = False

    if pdf_bytes is not None:
        started = time.perf_counter()
        parsed_pages, layout_report, furniture = prepare_pages(pdf_bytes, parsed_pages)
        measured["layout_s"] = round(time.perf_counter() - started, 4)
        if layout_report.failed:
            order_unverified = True
        elif layout_report.unverified_pages:
            order_unverified = True
            logger.warning(
                "two-column pages kept content-stream order",
                extra={
                    "extra_fields": {
                        "backend": backend,
                        "pages": layout_report.unverified_pages,
                    }
                },
            )
        if furniture.dropped or furniture.page_numbers:
            logger.info(
                "fallback dropped page furniture",
                extra={
                    "extra_fields": {
                        "backend": backend,
                        "repeated_lines": len(furniture.dropped),
                        "page_numbers": furniture.page_numbers,
                    }
                },
            )

    out: list[str] = []
    headings: list[tuple[int, str]] = []
    tables = 0
    for index, page in enumerate(parsed_pages):
        if index:
            out.extend(["", page_break.strip(), ""])
        for block in page_blocks(page):
            if _render_block(block, out, headings):
                tables += 1

    markdown = normalize_markdown("\n".join(out))
    page_count, spans = page_spans_from_markdown(markdown, page_break=page_break)

    reasons: list[str] = [DEGRADED_NO_FORMULA, *extra_degraded]
    if tables:
        reasons.append(DEGRADED_TABLE)
    if order_unverified:
        reasons.append(DEGRADED_ORDER)
    degraded_reason = "; ".join(dict.fromkeys(reason for reason in reasons if reason))

    return ParseBundle(
        markdown=markdown,
        page_count=page_count,
        spans=spans,
        backend=backend,
        parser_version=parser_version,
        degraded_reason=degraded_reason or None,
        timings=measured,
        headings=headings,
    )


__all__ = [
    "DEGRADED_NO_FORMULA",
    "DEGRADED_ORDER",
    "DEGRADED_TABLE",
    "PAGE_BREAK_DEFAULT",
    "TABLE_FALLBACK_MARKER",
    "PageSpan",
    "ParseBundle",
    "clamp_heading_levels",
    "heading_level",
    "normalize_markdown",
    "page_spans_from_markdown",
    "prepare_docling_markdown",
    "promote_paper_title",
    "render_markdown",
]
