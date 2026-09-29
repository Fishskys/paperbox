"""Tests for the pypdf reading-order repair (plan §2 T5, decision 14).

Two layers:

* the pure geometry (:func:`reorder_fragments`, :func:`looks_multi_column`) driven
  with hand-written coordinates, so the two-column rule is checked without a PDF;
* the real path over **synthetic PDFs built in memory** -- including the exactly
  two cases that were measured before the change (``logs/eval/two-column/``):
  a column-major stream (already correct, must stay correct) and an interleaved
  stream (pypdf fuses both columns into single lines, must become correct).
"""

from __future__ import annotations

import io

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from app.parsing import layout
from app.parsing.layout import TextFragment
from app.parsing.pdf import PageText, extract_pages

LEFT = [
    "The comparator offsets are cancelled by",
    "chopping, so the residual offset is",
    "dominated by the sampling network and",
    "stays below 0.5 LSB over temperature.",
]
RIGHT = [
    "The measured INL stays within 0.6 LSB",
    "and the SFDR is 78 dB at Nyquist,",
    "which confirms the calibration loop",
    "works across the full supply range.",
]

PAGE_WIDTH = 612
LEFT_X, RIGHT_X = 50, 320
TOP_Y, LINE_H = 700, 12
LONG_TITLE = "A Very Long Paper Title That Spans Both Columns Of The Page Layout"


# --------------------------------------------------------------------------- #
# synthetic PDFs (same geometry as logs/eval/two-column/make_two_column.py)
# --------------------------------------------------------------------------- #


def _esc(text: str) -> bytes:
    return (
        text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)").encode("latin-1")
    )


def _draw(x: int, y: int, text: str, *, size: int = 9) -> bytes:
    return b"BT /F1 %d Tf 1 0 0 1 %d %d Tm (%s) Tj ET\n" % (size, x, y, _esc(text))


def _page(data: bytes) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=PAGE_WIDTH, height=792)
    stream = DecodedStreamObject()
    stream.set_data(data)
    page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                        }
                    )
                }
            )
        }
    )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def two_column_pdf(
    *, interleaved: bool, title: str | None = None, state: bytes = b""
) -> bytes:
    """One two-column page; ``interleaved`` chooses the content-stream order."""
    out = bytearray(state)
    if title:
        out += _draw(LEFT_X, 760, title)
    if interleaved:
        for index, (left, right) in enumerate(zip(LEFT, RIGHT, strict=True)):
            y = TOP_Y - index * LINE_H
            out += _draw(LEFT_X, y, left)
            out += _draw(RIGHT_X, y, right)
    else:
        for index, line in enumerate(LEFT):
            out += _draw(LEFT_X, TOP_Y - index * LINE_H, line)
        for index, line in enumerate(RIGHT):
            out += _draw(RIGHT_X, TOP_Y - index * LINE_H, line)
    return _page(bytes(out))


def single_column_pdf() -> bytes:
    """One page whose text is a single column, plus a page number footer."""
    out = bytearray()
    for index, line in enumerate(LEFT + RIGHT):
        out += _draw(LEFT_X, TOP_Y - index * LINE_H, line)
    out += _draw(LEFT_X, 60, "1")
    return _page(bytes(out))


def _fragment(text: str, x: float, y: float, *, size: float = 9.0) -> TextFragment:
    return TextFragment(text=text, x=x, y=y, size=size, page=1)


def _interleaved_fragments() -> list[TextFragment]:
    fragments: list[TextFragment] = []
    for index, (left, right) in enumerate(zip(LEFT, RIGHT, strict=True)):
        y = TOP_Y - index * LINE_H
        fragments.append(_fragment(left, LEFT_X, y))
        fragments.append(_fragment(right, RIGHT_X, y))
    return fragments


# --------------------------------------------------------------------------- #
# pure geometry
# --------------------------------------------------------------------------- #


def test_interleaved_page_is_reordered_left_column_then_right_column() -> None:
    outcome = layout.reorder_fragments(_interleaved_fragments(), width=PAGE_WIDTH)
    assert outcome.applied is True
    assert outcome.columns == 2
    assert outcome.split_x == RIGHT_X
    assert outcome.lines == LEFT + RIGHT


def test_single_column_page_is_left_untouched() -> None:
    fragments = [
        _fragment(line, LEFT_X, TOP_Y - index * LINE_H)
        for index, line in enumerate(LEFT + RIGHT)
    ]
    outcome = layout.reorder_fragments(fragments, width=PAGE_WIDTH)
    assert outcome.applied is False
    assert outcome.lines == []
    assert outcome.columns == 1
    assert outcome.split_x is None


def test_a_full_width_fragment_keeps_its_place_and_splits_the_segments() -> None:
    """A full-width abstract between the columns is emitted where it sits."""
    abstract = _fragment(
        "Abstract: this block is wide enough to span both columns of the page, "
        "so it is not a column line at all.",
        LEFT_X,
        750,
    )
    fragments = _interleaved_fragments() + [abstract]
    outcome = layout.reorder_fragments(fragments, width=PAGE_WIDTH)
    assert outcome.applied is True
    assert outcome.lines[0] == abstract.text
    assert outcome.lines[1:5] == LEFT
    assert outcome.lines[5:] == RIGHT
    assert outcome.breaks == [1, 5]


def test_looks_multi_column_is_quiet_on_single_column_pages() -> None:
    fragments = [
        _fragment(line, LEFT_X, TOP_Y - index * LINE_H)
        for index, line in enumerate(LEFT + RIGHT)
    ]
    assert layout.looks_multi_column(fragments, width=PAGE_WIDTH) is False


def test_too_few_fragments_is_never_reordered() -> None:
    fragments = [_fragment("A", 50, 700), _fragment("B", 320, 700)]
    outcome = layout.reorder_fragments(fragments, width=PAGE_WIDTH)
    assert outcome.applied is False


def test_blank_lines_survive_a_column_break() -> None:
    """A large vertical gap inside a column is still a paragraph break."""
    fragments = [
        _fragment(LEFT[0], LEFT_X, 700),
        _fragment(LEFT[1], LEFT_X, 688),
        _fragment(LEFT[2], LEFT_X, 640),  # big gap -> blank line
        _fragment(LEFT[3], LEFT_X, 628),
        _fragment(RIGHT[0], RIGHT_X, 700),
        _fragment(RIGHT[1], RIGHT_X, 688),
        _fragment(RIGHT[2], RIGHT_X, 676),
        _fragment(RIGHT[3], RIGHT_X, 664),
    ]
    outcome = layout.reorder_fragments(fragments, width=PAGE_WIDTH)
    assert outcome.applied is True
    assert outcome.lines == [LEFT[0], LEFT[1], "", LEFT[2], LEFT[3], *RIGHT]


def test_columns_wider_apart_than_the_page_middle_are_still_detected() -> None:
    """The gutter must be found by the gap, not by assuming an even split."""
    fragments = [
        _fragment(LEFT[i], 60, 700 - i * 12) for i in range(4)
    ] + [_fragment(RIGHT[i], 300, 700 - i * 12) for i in range(4)]
    outcome = layout.reorder_fragments(fragments, width=PAGE_WIDTH)
    assert outcome.applied is True
    assert outcome.lines == LEFT + RIGHT


# --------------------------------------------------------------------------- #
# the page text itself (pypdf's layout-mode text)
# --------------------------------------------------------------------------- #


def _layout_line(left: str, right: str = "") -> str:
    """One layout-mode line: the right column starts at a fixed character column."""
    if not right:
        return left
    return left.ljust(78) + right


def test_layout_mode_text_is_reordered_left_column_then_right_column() -> None:
    text = "\n".join(
        _layout_line(left, right) for left, right in zip(LEFT, RIGHT, strict=True)
    )
    reordered = layout.reorder_page_text(text)
    assert reordered == "\n".join(LEFT) + "\n\n" + "\n".join(RIGHT)


def test_layout_mode_line_without_a_gutter_is_a_full_width_element() -> None:
    text = "\n".join(
        [
            "A full width title that runs across the whole page",
            "",
            _layout_line(LEFT[0], RIGHT[0]),
            _layout_line(LEFT[1], RIGHT[1]),
        ]
    )
    reordered = layout.reorder_page_text(text)
    blocks = (reordered or "").split("\n\n")
    assert blocks[0] == "A full width title that runs across the whole page"
    assert blocks[1] == "\n".join(LEFT[:2])
    assert blocks[2] == "\n".join(RIGHT[:2])


def test_layout_mode_text_without_a_gutter_has_no_column_line() -> None:
    assert layout.reorder_page_text("one column of text\nand another line of it") is None


def test_a_wide_word_gap_outside_the_middle_is_not_a_gutter() -> None:
    line = "short start" + " " * 30 + "and a long tail that keeps the run off centre" * 3
    assert layout.reorder_page_text(line) is None


def test_three_column_lines_keep_reading_left_to_right() -> None:
    """A second gutter must not scramble what is already in order.

    The split is taken at the widest middle gap, so the first two columns can end
    up in one block; what matters is that nothing is emitted out of order and
    nothing is dropped.
    """
    line = "first column".ljust(40) + "second column".ljust(40) + "third column"
    reordered = layout.reorder_page_text(line)
    assert reordered is not None
    assert reordered.index("first column") < reordered.index("second column")
    assert reordered.index("second column") < reordered.index("third column")
    assert sum(map(len, reordered.split())) == len("firstcolumnsecondcolumnthirdcolumn")


def test_same_content_ignores_order_but_not_characters() -> None:
    assert layout.same_content("abc def", "def abc") is True
    assert layout.same_content("abc def", "abc de") is False
    assert layout.same_content("abc", "abcd") is False


# --------------------------------------------------------------------------- #
# page-level behaviour
# --------------------------------------------------------------------------- #


def test_interleaved_stream_is_repaired_end_to_end() -> None:
    data = two_column_pdf(interleaved=True)
    pages = extract_pages(data)
    # Before the repair pypdf fuses both columns onto one line.
    assert pages[0].text.split("\n")[0] == f"{LEFT[0]} {RIGHT[0]}"

    report = layout.reorder_pages(pages, data)
    assert report.reordered_pages == 1
    assert report.columns == [2]
    assert report.unverified_pages == []
    # Left column top-down, then the right column, with a paragraph break at the
    # boundary so the two columns cannot be joined into one paragraph downstream.
    assert report.pages[0].text.split("\n") == LEFT + [""] + RIGHT


def test_column_major_stream_is_unchanged() -> None:
    """The already-correct case must not move (regression gold standard)."""
    data = two_column_pdf(interleaved=False)
    pages = extract_pages(data)
    before = pages[0].text.split("\n")

    report = layout.reorder_pages(pages, data)
    # The order is unchanged; the only delta is the paragraph break that the
    # reorder inserts at the column boundary.
    body = report.pages[0].text.replace("\n\n", "\n").split("\n")
    assert body == before == LEFT + RIGHT
    assert report.unverified_pages == []


def test_single_column_page_is_not_touched_and_not_warned_about() -> None:
    data = single_column_pdf()
    pages = extract_pages(data)
    report = layout.reorder_pages(pages, data)
    assert report.reordered_pages == 0
    assert report.unverified_pages == []
    assert report.warnings == []
    assert report.pages[0].text == pages[0].text


def test_full_width_title_is_kept_in_place_and_columns_are_reordered() -> None:
    data = two_column_pdf(interleaved=True, title=LONG_TITLE)
    pages = extract_pages(data)
    report = layout.reorder_pages(pages, data)
    # The full-width title is not a column line: it is emitted where it sits and
    # the two columns below it are still put back in order.
    assert report.reordered_pages == 1
    assert report.unverified_pages == []
    blocks = [block for block in report.pages[0].text.split("\n\n") if block.strip()]
    assert blocks[0].strip() == LONG_TITLE
    assert blocks[1] == "\n".join(LEFT)
    assert blocks[2] == "\n".join(RIGHT)


def test_missing_coordinates_are_reported_instead_of_reordering() -> None:
    pages = [PageText(page=1, text="one"), PageText(page=2, text="two")]
    report = layout.reorder_pages(pages, b"")
    assert report.failed is True
    assert report.unverified_pages == [1, 2]
    assert report.pages == pages


def test_a_reorder_that_does_not_match_the_page_text_is_refused() -> None:
    """Safety net: the reorder may rearrange the page text, never replace it.

    The page text here is unrelated to the PDF, which is what a badly decoded font
    or a mismatch between extraction modes looks like: nothing lines up, so the
    repair is dropped and the page keeps the text the pipeline already had.
    """
    data = two_column_pdf(interleaved=True)
    pages = [PageText(page=1, text="Completely unrelated words about something else")]
    report = layout.reorder_pages(pages, data)
    assert report.reordered_pages == 0
    assert report.unverified_pages == [1]
    assert report.pages[0].text == pages[0].text
    assert "could not be put back in order" in report.warnings[0]


# --------------------------------------------------------------------------- #
# running headers / footers
# --------------------------------------------------------------------------- #


def _journal_pages(count: int = 3) -> list[PageText]:
    return [
        PageText(
            page=index,
            text=(
                "IEEE JOURNAL OF SOLID-STATE CIRCUITS, VOL. 55\n"
                "\n"
                f"The calibration loop converges in {index} cycles and the\n"
                "residual error stays inside one LSB.\n"
                "\n"
                "Manuscript received April 2024; revised June 2024.\n"
                f"{1000 + index}"
            ),
        )
        for index in range(1, count + 1)
    ]


def test_repeated_header_and_footer_are_dropped_but_body_text_survives() -> None:
    result = layout.strip_running_lines(_journal_pages())
    assert result.dropped == [
        "IEEE JOURNAL OF SOLID-STATE CIRCUITS, VOL. 55",
        "Manuscript received April 2024; revised June 2024.",
    ]
    # Bare page numbers repeat by position and shape, never by text.
    assert result.page_numbers == 3
    first = result.pages[0].text.split("\n")
    assert "IEEE JOURNAL OF SOLID-STATE CIRCUITS, VOL. 55" not in first
    assert "Manuscript received April 2024; revised June 2024." not in first
    assert "1001" not in first
    # The body of every page survives, including the page-specific numbers.
    assert "The calibration loop converges in 1 cycles and the" in first
    assert "residual error stays inside one LSB." in first


def test_a_body_sentence_repeated_away_from_the_margins_is_kept() -> None:
    repeated = "This sentence appears on every page but is not page furniture."
    pages = [
        PageText(
            page=index,
            text=(
                f"Header {index}\n"
                "\n"
                f"The first paragraph of page {index}.\n"
                "\n"
                f"{repeated}\n"
                "\n"
                f"A third paragraph of page {index} keeps it out of the margins.\n"
                "\n"
                f"Tail {index}"
            ),
        )
        for index in range(1, 4)
    ]
    result = layout.strip_running_lines(pages)
    assert result.dropped == []
    for page in result.pages:
        assert repeated in page.text


def test_short_documents_are_left_alone() -> None:
    pages = [
        PageText(page=1, text="Same line\n\nbody one"),
        PageText(page=2, text="Same line\n\nbody two"),
    ]
    result = layout.strip_running_lines(pages)
    assert result.dropped == []
    assert result.page_numbers == 0
    assert result.pages == pages


def test_prepare_pages_returns_pages_report_and_furniture() -> None:
    data = two_column_pdf(interleaved=True)
    pages, report, furniture = layout.prepare_pages(data, extract_pages(data))
    assert report.reordered_pages == 1
    assert furniture.dropped == []
    assert pages[0].text.split("\n") == LEFT + [""] + RIGHT
