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
