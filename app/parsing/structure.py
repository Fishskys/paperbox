"""Section detection for extracted PDF pages (MVP-SPEC section 6).

The MVP works on the raw text layer, so detection is regex driven: numbered
headings (``2.1 Architecture``, ``III-B. Circuit Design``), well-known
unnumbered headings (``Abstract``, ``References``, ``Conclusion``) and short
all-caps lines. Sections are returned in reading order and also exposed as a
flat per-page span map so chunking can attach page numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.parsing.pdf import PageText

logger = get_logger(__name__)

#: Heading authority used when a paper does not start with an explicit heading.
SECTION_BODY = "Body"

_NUMBERED_HEADING = re.compile(
    r"^(?P<number>(?:\d+(?:\.\d+)*)|(?:[IVXivx]+(?:-[A-Za-z])?))[.)]?\s+(?P<title>\S.*)$"
)
_ROMAN_TAIL = re.compile(r"^[IVX]+(-[A-Z])?[.)]?$")
_ALL_CAPS = re.compile(r"^[A-Z][A-Z0-9 \-&/,:'()]+$")

#: Short unnumbered headings that must be recognized on their own.
KNOWN_HEADINGS = {
    "abstract": "Abstract",
    "introduction": "Introduction",
    "related work": "Related Work",
    "background": "Background",
    "method": "Method",
    "methods": "Methods",
    "methodology": "Methodology",
    "approach": "Approach",
    "experiments": "Experiments",
    "experimental setup": "Experimental Setup",
    "results": "Results",
    "results and discussion": "Results and Discussion",
    "discussion": "Discussion",
    "evaluation": "Evaluation",
    "conclusion": "Conclusion",
    "conclusions": "Conclusions",
    "conclusion and future work": "Conclusion and Future Work",
    "future work": "Future Work",
    "limitations": "Limitations",
    "acknowledgements": "Acknowledgements",
    "acknowledgments": "Acknowledgments",
    "references": "References",
    "bibliography": "Bibliography",
    "appendix": "Appendix",
}

_MAX_HEADING_WORDS = 14
_MAX_HEADING_CHARS = 110
_MAX_PAGE_HEADING_LOOKAHEAD = 3

#: A token that is only digits/punctuation, i.e. a table cell rather than a word.
_NUMERIC_TOKEN = re.compile(r"^[<>=~\u00b1+\-]?[\d.,%()\u00d7\u00b7\s]*\d[\d.,%()\u00d7\u00b7]*$")


def _looks_like_table_row(title: str) -> bool:
    """True when a heading candidate is really a row of table numbers."""
    tokens = title.split()
    if len(tokens) < 2:
        return False
    numeric = sum(1 for token in tokens if _NUMERIC_TOKEN.match(token))
    return numeric * 2 >= len(tokens)


@dataclass(slots=True)
class Section:
    """One detected section, with an inclusive 1-based page range."""

    title: str
    page_start: int
    page_end: int
    number: str | None = None
    paragraphs: list[tuple[int, str]] = field(default_factory=list)

    @property
    def label(self) -> str:
        """``number + title`` as stored on chunks (e.g. ``2.1 Architecture``)."""
        return f"{self.number} {self.title}".strip() if self.number else self.title


def _clean_line(line: str) -> str:
    return line.strip().strip("\u2022\u00b7").strip()


def _match_heading(line: str) -> tuple[str | None, str] | None:
    """Return ``(number, title)`` when ``line`` looks like a heading."""
    cleaned = _clean_line(line)
    if not cleaned or len(cleaned) > _MAX_HEADING_CHARS:
        return None
    words = cleaned.split()
    if len(words) > _MAX_HEADING_WORDS:
        return None

    match = _NUMBERED_HEADING.match(cleaned)
    if match:
        number = match.group("number")
        title = match.group("title").strip()
        if title and not title.endswith(".") and not _looks_like_table_row(title):
            if number[0].isdigit():
                return number, title
            # Roman numerals / single letters count as headings only when they
            # are uppercase and introduce a real title; this keeps "I think ..."
            # and math fragments such as "i ∈ Rdmodel×dv" out of the index.
            if number.isupper() and title[:1].isalpha() and len(words) <= 8:
                return number, title

    lowered = cleaned.rstrip(".:").casefold()
    if lowered in KNOWN_HEADINGS:
        return None, KNOWN_HEADINGS[lowered]

    if len(words) <= 8 and _ALL_CAPS.match(cleaned) and not cleaned.endswith("."):
        return None, cleaned
    return None


def _heading_is_page_header(lines: list[str], index: int) -> bool:
    """Reject running headers: a heading repeated within the next few lines."""
    candidate = _clean_line(lines[index])
    lookahead = lines[index + 1 : index + 1 + _MAX_PAGE_HEADING_LOOKAHEAD]
    return any(_clean_line(line) == candidate for line in lookahead)


def detect_sections(pages: list[PageText]) -> list[Section]:
    """Split ``pages`` into sections in reading order.

    When no heading is found the whole document becomes a single ``Body``
    section so chunking always has a section to attach chunks to.
    """
    if not pages:
        return []

    sections: list[Section] = []
    current = Section(title=SECTION_BODY, page_start=pages[0].page, page_end=pages[0].page)
    suppressed: set[str] = set()
    for page in pages:
        lines = page.text.split("\n")
        pending: list[str] = []
        touched = False
        for index, line in enumerate(lines):
            if _clean_line(line) in suppressed:
                continue
            heading = _match_heading(line)
            if heading is not None and _heading_is_page_header(lines, index):
                # Running header/footer: remember it so the identical line on the
                # next page is ignored too.
                suppressed.add(_clean_line(line))
                heading = None
            if heading is None:
                pending.append(line)
                continue
            number, title = heading
            body = "\n".join(pending).strip()
            if body:
                _append_paragraphs(current, page.page, body)
                touched = True
            pending = []
            if touched:
                current.page_end = page.page
            sections.append(current)
            current = Section(
                title=title, page_start=page.page, page_end=page.page, number=number
            )
            touched = False
        body = "\n".join(pending).strip()
        if body:
            _append_paragraphs(current, page.page, body)
            touched = True
        if touched:
            current.page_end = page.page
    sections.append(current)

    for section in sections:
        section.paragraphs = [(p, t) for p, t in section.paragraphs if t]
    # Drop the placeholder that only exists because the document opens with a
    # heading, but keep the single Body fallback for heading-less documents.
    populated = [section for section in sections if section.paragraphs]
    return populated or sections[:1]


def _append_paragraphs(section: Section, page_number: int, body: str) -> None:
    if not body:
        return
    for paragraph in body.split("\n\n"):
        text = _join_wrapped_lines(paragraph)
        if text:
            section.paragraphs.append((page_number, text))


def _join_wrapped_lines(block: str) -> str:
    """Turn a block of hard-wrapped PDF lines back into flowing prose.

    Wrapped lines end mid-sentence, so single newlines become spaces; a word
    broken with a trailing hyphen is stitched back together. Blank lines (real
    paragraph breaks) were already consumed by the caller.
    """
    text = re.sub(r"[-\u2010\u2011]\s*\n\s*", "", block)
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def merge_short_sections(
    sections: list[Section], *, target_chars: int = 1200
) -> list[Section]:
    """Merge trivially short sections into their predecessor.

    Running headers and per-page banners often produce one-line "sections";
    merging keeps chunk statistics sane without touching real headings.
    """
    if not sections:
        return []
    merged: list[Section] = [sections[0]]
    for section in sections[1:]:
        previous = merged[-1]
        previous_short = sum(len(text) for _, text in previous.paragraphs) < target_chars
        section_short = sum(len(text) for _, text in section.paragraphs) < target_chars
        if previous_short and section_short and section.title.isupper():
            previous.page_end = max(previous.page_end, section.page_end)
            previous.paragraphs.extend(section.paragraphs)
            previous.paragraphs.sort(key=lambda item: item[0])
            continue
        merged.append(section)
    return merged
