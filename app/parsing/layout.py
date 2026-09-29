"""Reading-order repair for the pypdf backend (plan §2 T5, decision 14).

docling gets the reading order from its layout model; ``pypdf`` returns text in
**content-stream order**, which is wrong for the common two-column IEEE/ACM page
whose stream interleaves both columns line by line::

    left 1 / right 1 / left 2 / right 2 ...     ->   "left 1 right 1" (one line!)

The rule agreed with the owner is *left column top-down, then right column
top-down*, and this module is where that happens -- with coordinates, not by
guessing from the text.

Two design rules keep the repair safe:

1. **A single-column page is never rebuilt.** When no column gutter is found the
   original page text is kept verbatim, so the common case cannot regress.
2. **A layout we do not understand is left alone and reported.** If any text
   fragment crosses the candidate gutter (a full-width title/abstract on page 1,
   a wide table, a figure caption spanning both columns), or either side is too
   short to be a column, the page keeps its original order and the caller records
   ``reading order not verified`` in ``ParseBundle.degraded_reason``.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass, field

from pypdf import PdfReader

from app.core.logging import get_logger
from app.parsing.pdf import PageText

logger = get_logger(__name__)

#: Marker recorded in ``ParseBundle.degraded_reason`` when a page kept the order
#: pypdf produced although it may be wrong.
ORDER_UNVERIFIED_REASON = "reading order not verified"

#: Average glyph advance as a fraction of the font size. Used only to *estimate*
#: a fragment's right edge (pypdf's visitor does not report widths); 0.5em is the
#: usual Helvetica/Times average and is plenty to spot a two-column gutter.
_ADVANCE_RATIO = 0.5

_MIN_FRAGMENTS = 4
_MIN_COLUMN_FRAGMENTS = 3
_SPLIT_MIN_RATIO = 0.25
_SPLIT_MAX_RATIO = 0.75
_MIN_GUTTER_PT = 12.0
#: Spaces that separate two columns on one layout-mode line, and how central that
#: run has to be to count as the gutter rather than a wide word gap.
_MIN_COLUMN_RUN = 4
_GUTTER_RATIO = 0.04
_MIN_SHARED_BASELINES = 2
_Y_TOLERANCE_RATIO = 0.35
_BLANK_LINE_GAP_RATIO = 1.6
#: Vertical advance used when one PDF fragment contains several text lines.
_LINE_ADVANCE_RATIO = 1.15
#: How far past the gutter a fragment has to reach before it counts as a
#: full-width element. Fragment widths are *estimates* (see ``_ADVANCE_RATIO``),
#: so a strict test rejects nearly every real page: on page 2 of 1807.11311 the
#: two "crossing" runs overshot the gutter by 1pt and 10pt. A genuinely
#: full-width title/table overshoots by hundreds of points.
_CROSS_MARGIN_PT = 24.0
_CROSS_MARGIN_RATIO = 0.05
#: Percentile of the left column's estimated right edges used as its real edge,
#: so that a single over-estimated run cannot close the gutter on its own.
_LEFT_EDGE_PERCENTILE = 0.9
#: Kerning (thousandths of an em) in a ``TJ`` array that means "word space".
_TJ_SPACE_THRESHOLD = 250

#: A footer that is only a page number ("12", "- 12 -", "xii").
_PAGE_NUMBER = re.compile(r"^[\s\-\u2013\u2014\u00b7|*]*\d{1,4}[\s\-\u2013\u2014\u00b7|*]*$")


@dataclass(slots=True)
class TextFragment:
    """One run of text with the baseline coordinates it was drawn at."""

    text: str
    x: float
    y: float
    size: float
    page: int

    @property
    def right(self) -> float:
        """Estimated right edge (documented approximation, see ``_ADVANCE_RATIO``)."""
        return self.x + self.size * _ADVANCE_RATIO * len(self.text)


@dataclass(slots=True)
class PageLayout:
    """Coordinates of one page, in content-stream order."""

    page: int
    width: float
    fragments: list[TextFragment] = field(default_factory=list)
    #: pypdf's layout-mode text: same characters, x positions kept as padding. This
    #: is where the repaired page text comes from (see :func:`reorder_page_text`).
    text: str = ""


@dataclass(slots=True)
class ColumnLayout:
    """Result of looking at one page's fragments."""

    lines: list[str] = field(default_factory=list)
    columns: int = 1
    split_x: float | None = None
    applied: bool = False
    warning: str | None = None
    #: Indices in ``lines`` that start a new block, i.e. where the caller must put
    #: a paragraph break: a column boundary or a full-width element in between.
    breaks: list[int] = field(default_factory=list)


@dataclass(slots=True)
class LayoutReport:
    """Per-page outcome of :func:`reorder_pages`, plus what changed."""

    pages: list[PageText] = field(default_factory=list)
    columns: list[int] = field(default_factory=list)
    reordered_pages: int = 0
    #: Pages whose order may be wrong: a two-column layout was recognised but not
    #: repaired (full-width element crossing the gutter, ...).
    unverified_pages: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failed: bool = False


@dataclass(slots=True)
class RunningLinesResult:
    """Pages with running headers/footers removed."""

    pages: list[PageText] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    page_numbers: int = 0


# --------------------------------------------------------------------------- #
# coordinates
# --------------------------------------------------------------------------- #


def _split_fragment(
    text: str, x: float, y: float, size: float, page: int
) -> list[TextFragment]:
    """One visitor callback may carry several lines; give each its own baseline."""
    fragments: list[TextFragment] = []
    for index, piece in enumerate(text.split("\n")):
        cleaned = re.sub(r"[ \t]+", " ", piece).strip()
        if cleaned:
            fragments.append(
                TextFragment(
                    text=cleaned,
                    x=x,
                    y=y - index * size * _LINE_ADVANCE_RATIO,
                    size=size,
                    page=page,
                )
            )
    return fragments


def _operand_text(operand: object) -> str:
    """Text of a ``Tj``/``TJ`` operand, without pypdf's byte-string guessing.

    ``visitor_operand_after`` gets the **raw** operands: for a font without a
    ``/ToUnicode`` map pypdf hands over a ``ByteStringObject``, and ``str()`` on
    it applies a UTF-16 heuristic -- ``str(b"The comparator offsets are ca…")``
    comes back as ``"The comparator offsets are ca…"`` but a 66-byte title comes
    back as CJK-looking mojibake. The page's own encoding is WinAnsi (cp1252) in
    practice, so decode that explicitly and only honour a real BOM.
    """
    if isinstance(operand, str):
        return operand
    if isinstance(operand, (bytes, bytearray)):
        raw = bytes(operand)
        if raw[:2] == b"\xfe\xff":
            return raw[2:].decode("utf-16-be", errors="replace")
        if raw[:2] == b"\xff\xfe":
            return raw[2:].decode("utf-16-le", errors="replace")
        return raw.decode("cp1252", errors="replace")
    return str(operand)


def _tj_text(items: list[object]) -> str:
    """Text of one ``TJ`` array, turning big kerning moves back into spaces.

    In a ``TJ`` array the strings carry the glyphs and the numbers carry kerning
    in thousandths of an em. A number around -300 is how most producers write a
    word space, so ignoring the numbers glues whole lines together: page 2 of
    ``2606.09129`` comes back as ``"prehensiveoptimizationofstability"`` without
    this, and as ``"prehensive optimization of stability"`` with it.
    """
    out: list[str] = []
    for item in items:
        if isinstance(item, (int, float)):
            if float(item) <= -_TJ_SPACE_THRESHOLD and out and not out[-1].endswith(" "):
                out.append(" ")
            continue
        out.append(_operand_text(item))
    return "".join(out)


def _mult(m1: list[float], m2: list[float]) -> list[float]:
    """3x2 matrix multiply, same convention as pypdf's own text extractor."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    ]


def extract_page_layouts(data: bytes) -> list[PageLayout]:
    """``(page, width, fragments)`` for every page of ``data``.

    Coordinates come from ``visitor_operand_after`` -- the *live* text matrix at
    the moment text is shown -- and **not** from ``visitor_text``. Measured on
    pypdf 6.18: ``visitor_text`` hands over a memoised matrix that lags one
    text-show operation behind, so on a two-column page the second column comes
    back as ``(0, 0)``, i.e. exactly the fragments this module exists for.
    ``visitor_operand_after`` also exposes the ``Tj``/``TJ`` operands, so the text
    and its position stay together.

    Never raises: a page whose text layer cannot be visited yields no fragments,
    which means "do not reorder this page".
    """
    layouts: list[PageLayout] = []
    if not data:
        return layouts
    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            reader.decrypt("")
    except Exception as exc:  # noqa: BLE001 - reordering is best effort
        logger.warning("cannot read PDF coordinates: %s", exc)
        return layouts

    for index in range(len(reader.pages)):
        layout = PageLayout(page=index + 1, width=0.0)
        try:
            page = reader.pages[index]
            layout.width = float(page.mediabox.width)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cannot read page %d geometry: %s", index + 1, exc)
            layouts.append(layout)
            continue

        font_size = [12.0]

        def emit(text: str, cm_matrix: list[float], tm_matrix: list[float]) -> None:
            if not text or not text.strip():
                return
            matrix = _mult(list(tm_matrix), list(cm_matrix))
            try:
                x = float(matrix[4])
                y = float(matrix[5])
                scale = abs(float(matrix[0])) or 1.0
            except (TypeError, ValueError, IndexError):  # pragma: no cover
                return
            size = max(1.0, font_size[0] * scale)
            layout.fragments.extend(_split_fragment(text, x, y, size, index + 1))

        def operand_after(operator, operands, cm_matrix, tm_matrix) -> None:  # noqa: ANN001
            try:
                if operator == b"Tf" and len(operands) >= 2:
                    font_size[0] = abs(float(operands[1])) or 12.0
                elif operator in (b"Tj", b"'", b'"') and operands:
                    emit(_operand_text(operands[0]), cm_matrix, tm_matrix)
                elif operator == b"TJ" and operands:
                    items = operands[0]
                    if isinstance(items, (list, tuple)):
                        emit(_tj_text(items), cm_matrix, tm_matrix)
            except Exception as exc:  # noqa: BLE001 - one bad operand is not fatal
                logger.debug("cannot read a text operand on page %d: %s", index + 1, exc)

        try:
            layout.text = page.extract_text(extraction_mode="layout")
        except Exception as exc:  # noqa: BLE001
            logger.warning("cannot read page %d layout text: %s", index + 1, exc)
        try:
            # Two passes on purpose: the layout-mode text and the operand visitor do
            # not combine -- pypdf's layout mode never calls the visitor, so asking
            # for both at once yields a page with text and no coordinates.
            page.extract_text(visitor_operand_after=operand_after)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cannot visit page %d text: %s", index + 1, exc)
        layouts.append(layout)
    return layouts


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _cluster_lines(fragments: list[TextFragment]) -> list[list[TextFragment]]:
    """Group fragments into visual lines (same baseline), top of page first."""
    if not fragments:
        return []
    tolerance = max(1.0, _median([f.size for f in fragments]) * _Y_TOLERANCE_RATIO)
    lines: list[list[TextFragment]] = []
    for fragment in sorted(fragments, key=lambda item: (-item.y, item.x)):
        if lines and abs(lines[-1][0].y - fragment.y) <= tolerance:
            lines[-1].append(fragment)
        else:
            lines.append([fragment])
    for line in lines:
        line.sort(key=lambda item: item.x)
    return lines


def _percentile(values: list[float], fraction: float) -> float:
    """Value at ``fraction`` of the sorted ``values`` (cheap, no interpolation)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round(fraction * (len(ordered) - 1)))
    return ordered[min(max(index, 0), len(ordered) - 1)]


def _cross_margin(width: float) -> float:
    return max(_CROSS_MARGIN_PT, width * _CROSS_MARGIN_RATIO)


def _shared_baselines(
    lines: list[list[TextFragment]], split: float, gutter_min: float
) -> int:
    """Baselines holding fragments on both sides, separated by a real gutter.

    This is the test that a candidate split is a real column gutter: the two
    sides have to *face each other* line by line. A stray right-aligned footer,
    a table row or a centered heading splits a page into two x groups without ever
    producing such a baseline, so they cannot talk the detector into "repairing"
    a single-column page.

    (The earlier height-based test could not tell those apart: one full-width
    title at the top of a page made the right column look too short.)
    """
    shared = 0
    for line in lines:
        left = [fragment for fragment in line if fragment.x < split]
        right = [fragment for fragment in line if fragment.x >= split]
        if not left or not right:
            continue
        gap = min(fragment.x for fragment in right) - _percentile(
            [fragment.right for fragment in left], _LEFT_EDGE_PERCENTILE
        )
        if gap >= gutter_min:
            shared += 1
    return shared


def _detect_split(fragments: list[TextFragment], width: float) -> float | None:
    """X coordinate of the widest trustworthy column gutter, if there is one.

    Candidates come from the *left edges* of the fragments, not from estimated
    widths: those edges are exact (they are the text matrices) and a two-column
    page puts them into two tight clusters with the gutter in between. Scoring the
    candidate by the gap to the next left edge is what page 2 of ``1807.11311``
    needs -- its left column runs to x=296 and the right one starts at x=313,
    while estimated widths would put the "best" gutter a hundred points too far
    right and hand the first right-column fragments to the left column.
    """
    if width <= 0:
        return None
    gutter_min = max(_MIN_GUTTER_PT, width * _GUTTER_RATIO)
    lines = _cluster_lines(fragments)
    edges = sorted({round(fragment.x, 1) for fragment in fragments})
    best: tuple[float, float] | None = None
    for previous, candidate in zip(edges, edges[1:]):
        # Adjacent left edges are dense *inside* a column (every text run starts
        # its own), so only the gutter stands out -- but it can be as narrow as
        # 17pt where the columns' text is wide (page 2 of 1807.11311: 296 -> 313),
        # hence ``_MIN_GUTTER_PT`` and not the wider whitespace threshold.
        gap = candidate - previous
        if gap < _MIN_GUTTER_PT:
            continue
        if not (width * _SPLIT_MIN_RATIO <= candidate <= width * _SPLIT_MAX_RATIO):
            continue
        left = [fragment for fragment in fragments if fragment.x < candidate]
        right = [fragment for fragment in fragments if fragment.x >= candidate]
        if len(left) < _MIN_COLUMN_FRAGMENTS or len(right) < _MIN_COLUMN_FRAGMENTS:
            continue
        if _shared_baselines(lines, candidate, gutter_min) < _MIN_SHARED_BASELINES:
            continue
        if best is None or gap > best[1]:
            best = (candidate, gap)
    return best[0] if best is not None else None


def _lines_from_group(group: list[TextFragment]) -> list[str]:
    """Text lines of one column, top-down, with blank lines where gaps are large."""
    lines = _cluster_lines(group)
    if not lines:
        return []
    spacing = _median(
        [
            lines[index - 1][0].y - lines[index][0].y
            for index in range(1, len(lines))
            if lines[index - 1][0].y - lines[index][0].y > 0
        ]
    )
    threshold = spacing * _BLANK_LINE_GAP_RATIO if spacing > 0 else None
    out: list[str] = []
    for index, line in enumerate(lines):
        if index and threshold is not None:
            gap = lines[index - 1][0].y - line[0].y
            if gap > threshold:
                out.append("")
        text = re.sub(r"\s+", " ", " ".join(fragment.text for fragment in line)).strip()
        if text:
            out.append(text)
    return out

def reorder_fragments(fragments: list[TextFragment], *, width: float) -> ColumnLayout:
    """Apply the agreed reading order to one page's fragments.

    Pure function: the tests drive it with hand-written coordinates, so the
    two-column rule can be checked without a PDF.

    The page is cut into *segments* by full-width elements (title, abstract,
    figure, table, wide equation). Inside a segment the agreed rule applies --
    left column top-down, then right column top-down -- and a full-width element
    is emitted where it sits on the page. A page that is two-column from top to
    bottom has a single segment and therefore exactly the agreed order.
    """
    usable = [fragment for fragment in fragments if fragment.text.strip()]
    if len(usable) < _MIN_FRAGMENTS:
        return ColumnLayout()
    split = _detect_split(usable, width)
    if split is None:
        return ColumnLayout()

    margin = _cross_margin(width)
    lines: list[str] = []
    breaks: list[int] = []
    segment: list[list[TextFragment]] = []

    def open_block() -> None:
        if lines:
            breaks.append(len(lines))

    def flush_segment() -> None:
        if not segment:
            return
        left = [
            fragment
            for line in segment
            for fragment in line
            if fragment.x < split
        ]
        right = [
            fragment
            for line in segment
            for fragment in line
            if fragment.x >= split
        ]
        for group in (left, right):
            block = _lines_from_group(group)
            if not block:
                continue
            open_block()
            lines.extend(block)
        segment.clear()

    for line in _cluster_lines(usable):
        full_width = any(
            fragment.x < split and fragment.right >= split + margin for fragment in line
        )
        if full_width:
            flush_segment()
            text = re.sub(
                r"\s+", " ", " ".join(fragment.text for fragment in line)
            ).strip()
            if text:
                open_block()
                lines.append(text)
            continue
        segment.append(line)
    flush_segment()

    if not lines:
        return ColumnLayout()
    return ColumnLayout(
        lines=lines,
        columns=2,
        split_x=split,
        applied=True,
        breaks=breaks,
    )


def _norm_index(text: str) -> tuple[str, list[int]]:
    """Alphanumeric stream of ``text`` plus each stream char's index in ``text``."""
    chars: list[str] = []
    index: list[int] = []
    for position, char in enumerate(text):
        lowered = char.lower()
        if lowered.isalnum():
            chars.append(lowered)
            index.append(position)
    return "".join(chars), index


def _blocks(lines: list[str], breaks: list[int]) -> list[list[str]]:
    """Split ``lines`` at ``breaks`` and at the blank lines that are already there."""
    starts = {index for index in breaks if 0 < index < len(lines)}
    blocks: list[list[str]] = []
    current: list[str] = []
    for index, line in enumerate(lines):
        if index in starts and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _column_split(line: str) -> tuple[int, int] | None:
    """The space run that separates two columns on a layout-mode line.

    pypdf's layout mode keeps the x position of every text run by padding with
    spaces, so a line that carries two columns has one long run of spaces in the
    middle (75 spaces on the synthetic pair, 25-40 on a real IEEE page). The run's
    *middle* has to sit in the middle of the line: that keeps a wide gap between
    two words of a single column from being mistaken for a gutter.
    """
    best: tuple[int, int] | None = None
    start = None
    for index in range(len(line) + 1):
        char = line[index] if index < len(line) else "x"
        if char == " ":
            start = index if start is None else start
            continue
        if start is None:
            continue
        length = index - start
        middle = (start + index) / 2
        if (
            length >= _MIN_COLUMN_RUN
            and _SPLIT_MIN_RATIO * len(line) <= middle <= _SPLIT_MAX_RATIO * len(line)
            and (best is None or length > best[1] - best[0])
        ):
            best = (start, index)
        start = None
    return best


def _split_layout_line(line: str) -> list[str]:
    """The column fragments of one layout-mode line (one entry = one column)."""
    parts: list[str] = []
    rest = line
    while True:
        run = _column_split(rest)
        if run is None:
            break
        parts.append(rest[: run[0]].strip())
        rest = rest[run[1] :]
    parts.append(rest.strip())
    return [part for part in parts if part]


def reorder_page_text(text: str) -> str | None:
    """Left column top-down, then right column, over a page's layout-mode text.

    The characters come from pypdf's own layout-mode extraction, so they are the
    ones the rest of the pipeline already uses; only the *order* changes. A line
    with no middle gutter is not a column line -- a full-width title, abstract or
    table -- and is emitted where it sits, which also cuts the page into segments
    so that a left column never crosses a full-width element.

    Returns ``None`` when the page has no column line at all.
    """
    blocks: list[list[str]] = []
    left_column: list[str] = []
    right_column: list[str] = []
    split_lines = 0

    def flush() -> None:
        for column in (left_column, right_column):
            if column:
                blocks.append(list(column))
                column.clear()

    for raw in text.split("\n"):
        if not raw.strip():
            flush()
            continue
        parts = _split_layout_line(raw)
        if len(parts) < 2:
            flush()
            blocks.append([parts[0] if parts else raw.strip()])
            continue
        split_lines += 1
        left_column.append(parts[0])
        right_column.append(" ".join(parts[1:]))
    flush()
    if not split_lines:
        return None
    return "\n\n".join("\n".join(block) for block in blocks if block)


def same_content(candidate: str, original: str) -> bool:
    """True when the two texts carry the same characters (order aside).

    Counts instead of sequences: reordering is a permutation, so every letter of
    the page has to survive it. A missing letter means the reconstruction read the
    page differently -- and then the page keeps the text the pipeline already had.
    """
    left, _ = _norm_index(candidate)
    right, _ = _norm_index(original)
    return Counter(left) == Counter(right)


def looks_multi_column(
    fragments: list[TextFragment], *, width: float, min_lines: int = 2
) -> bool:
    """True when pypdf's own line building looks like it merged two columns.

    This is the *suspicion* signal needed when a layout could not be repaired:
    a single-column page never has two fragments on one baseline separated by a
    gutter, a two-column page whose stream interleaves them always does (this is
    exactly what the synthetic ``interleaved.pdf`` looks like). Without this
    check every single-column paper would be reported as "order not verified".
    """
    usable = [fragment for fragment in fragments if fragment.text.strip()]
    if len(usable) < _MIN_FRAGMENTS or width <= 0:
        return False
    gutter_min = max(_MIN_GUTTER_PT, width * _GUTTER_RATIO)
    return (
        _shared_baselines(_cluster_lines(usable), width / 2, gutter_min) >= min_lines
    )


def reorder_pages(pages: list[PageText], data: bytes) -> LayoutReport:
    """Replace two-column page text with left-column-then-right-column text.

    Pages without a trustworthy gutter keep the original text (rule 1); when such
    a page looks like it interleaves two columns it is listed in
    ``unverified_pages`` so the caller can record the warning (rule 2).
    """
    report = LayoutReport(pages=list(pages))
    layouts = extract_page_layouts(data)
    if len(layouts) != len(pages):
        report.failed = True
        report.unverified_pages = [page.page for page in pages]
        report.warnings.append(
            f"coordinates cover {len(layouts)} of {len(pages)} pages; order not verified"
        )
        return report

    for index, page in enumerate(pages):
        layout = layouts[index]
        outcome = reorder_fragments(layout.fragments, width=layout.width)
        report.columns.append(outcome.columns)
        if outcome.applied:
            reordered = reorder_page_text(layout.text)
            if reordered is not None and same_content(reordered, page.text):
                report.pages[index] = PageText(page=page.page, text=reordered)
                report.reordered_pages += 1
                continue
            report.unverified_pages.append(page.page)
            report.warnings.append(
                f"page {page.page}: two columns detected but the page text could not "
                "be put back in order; content-stream order kept"
            )
            continue
        if looks_multi_column(layout.fragments, width=layout.width):
            report.unverified_pages.append(page.page)
            report.warnings.append(
                f"page {page.page}: two columns detected but not repaired; "
                "content-stream order kept"
            )
    return report


# --------------------------------------------------------------------------- #
# running headers / footers
# --------------------------------------------------------------------------- #


def _non_empty_positions(lines: list[str]) -> list[int]:
    return [index for index, line in enumerate(lines) if line.strip()]


def strip_running_lines(
    pages: list[PageText],
    *,
    window: int = 2,
    min_pages: int = 3,
    ratio: float = 0.6,
    max_chars: int = 120,
) -> RunningLinesResult:
    """Drop repeated headers/footers, and bare page numbers.

    Only lines in the **first/last ``window`` non-empty positions** of a page are
    considered, and only when they repeat on at least ``ratio`` of the pages --
    a sentence that merely happens to repeat inside the body must survive, so
    there is deliberately no whole-document deduplication.

    Bare page numbers are a second rule: they differ on every page, so they can
    never be "repeated", yet docling drops them (it labels them ``Page-footer``)
    and the fallback side has to match.
    """
    result = RunningLinesResult(pages=list(pages))
    if len(pages) < min_pages:
        return result

    candidate_pages: dict[str, set[int]] = {}
    numeric_head_pages: set[int] = set()
    numeric_tail_pages: set[int] = set()
    for page in pages:
        lines = page.text.split("\n")
        positions = _non_empty_positions(lines)
        if not positions:
            continue
        head = positions[:window]
        tail = positions[-window:]
        for index in set(head) | set(tail):
            text = re.sub(r"\s+", " ", lines[index].strip())
            if text:
                candidate_pages.setdefault(text, set()).add(page.page)
        if any(_PAGE_NUMBER.match(lines[index].strip()) for index in head):
            numeric_head_pages.add(page.page)
        if any(_PAGE_NUMBER.match(lines[index].strip()) for index in tail):
            numeric_tail_pages.add(page.page)

    threshold = max(2, int(len(pages) * ratio + 0.5))
    dropped = {
        text
        for text, seen in candidate_pages.items()
        if len(seen) >= threshold and len(text) < max_chars
    }
    # A page number differs on every page, so "repeated text" can never catch it:
    # what repeats is its *position* and its shape. docling drops those lines
    # (they are labelled Page-header/Page-footer), and the fallback has to match.
    drop_numeric_head = len(numeric_head_pages) >= threshold
    drop_numeric_tail = len(numeric_tail_pages) >= threshold
    page_numbers = len(numeric_head_pages if drop_numeric_head else set()) + len(
        numeric_tail_pages if drop_numeric_tail else set()
    )

    if not dropped and not page_numbers:
        return result

    result.dropped = sorted(dropped)
    result.page_numbers = page_numbers
    stripped: list[PageText] = []
    for page in pages:
        lines = page.text.split("\n")
        positions = _non_empty_positions(lines)
        head = set(positions[:window])
        tail = set(positions[-window:])
        kept: list[str] = []
        for index, line in enumerate(lines):
            normalized = re.sub(r"\s+", " ", line.strip())
            if index in head or index in tail:
                if normalized in dropped:
                    continue
                if (
                    (drop_numeric_head and index in head)
                    or (drop_numeric_tail and index in tail)
                ) and _PAGE_NUMBER.match(normalized):
                    continue
            kept.append(line)
        stripped.append(PageText(page=page.page, text="\n".join(kept).strip()))
    result.pages = stripped
    logger.info(
        "dropped repeated page furniture",
        extra={
            "extra_fields": {
                "backend": "pypdf",
                "dropped_lines": result.dropped,
                "page_numbers": result.page_numbers,
            }
        },
    )
    return result


def prepare_pages(
    data: bytes, pages: list[PageText]
) -> tuple[list[PageText], LayoutReport, RunningLinesResult]:
    """Coordinates reorder, then running-line removal (in that order).

    Order matters: the strip works on text lines, while reordering needs the
    fragment coordinates of *all* lines, so the strip has to come last -- else a
    header that was removed from the text would be re-emitted by the reorder.
    """
    reordered = reorder_pages(pages, data)
    stripped = strip_running_lines(reordered.pages)
    return stripped.pages, reordered, stripped


__all__ = [
    "ORDER_UNVERIFIED_REASON",
    "ColumnLayout",
    "LayoutReport",
    "PageLayout",
    "RunningLinesResult",
    "TextFragment",
    "extract_page_layouts",
        "looks_multi_column",
        "prepare_pages",
    "reorder_fragments",
    "reorder_pages",
    "strip_running_lines",
]
