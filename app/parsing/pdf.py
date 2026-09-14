"""PDF text extraction (Phase 2, MVP-SPEC section 6).

``extract_pages`` turns raw PDF bytes into one :class:`PageText` per page so
that every downstream chunk keeps a 1-based page range. Text extraction relies
on ``pypdf`` only: the MVP deliberately avoids heavyweight layout/OCR stacks.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Iterable

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Control characters removed from extracted text. NUL (0x00) cannot be stored in
#: a PostgreSQL ``text`` column at all; the rest of the C0 range would be stored
#: but corrupts the JSON documents sent to OpenSearch. Tab and newline stay.
_CONTROL_CHAR_TABLE = {
    code: None for code in range(0x20) if code not in (0x09, 0x0A)
}
_CONTROL_CHAR_TABLE[0x7F] = None


class PdfParseError(RuntimeError):
    """Raised when a byte payload cannot be read as a PDF."""


@dataclass(slots=True)
class PageText:
    """Plain text of a single PDF page.

    ``page`` is 1-based because it is surfaced to users through chunk
    ``page_start``/``page_end``.
    """

    page: int
    text: str

    @property
    def is_blank(self) -> bool:
        return not self.text.strip()


def _page_text(page: object) -> str:
    """Extract text from one pypdf page, tolerating per-page failures."""
    try:
        extracted = page.extract_text()  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - depends on malformed PDFs
        logger.warning("page text extraction failed: %s", exc)
        return ""
    if not extracted:
        return ""
    return normalize_page_text(extracted)


def normalize_page_text(text: str) -> str:
    """Normalize one page's text layer while preserving line structure.

    Line structure must survive: section detection works line by line, so hard
    wrapping is kept as a single newline and blank lines stay blank. Re-joining
    wrapped lines into prose happens later, per paragraph, once the section
    boundaries are known (``app.parsing.structure``).

    Control characters are dropped here: PostgreSQL rejects NUL (0x00) outright
    (``DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes``, which
    made whole ingestion jobs fail on some arXiv PDFs) and the remaining C0
    controls poison the JSON sent to OpenSearch. Tab/newline survive.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.translate(_CONTROL_CHAR_TABLE)
    out: list[str] = []
    pending_blank = False
    for raw_line in normalized.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            pending_blank = True
            continue
        if out:
            out.append("\n\n" if pending_blank else "\n")
        out.append(line.strip())
        pending_blank = False
    return "".join(out).strip()


def extract_pages(data: bytes) -> list[PageText]:
    """Return the 1-based pages of ``data``.

    Raises:
        PdfParseError: when the payload is empty, not a PDF, encrypted with a
            password we do not have, or otherwise unreadable.
    """
    if not data:
        raise PdfParseError("empty PDF payload")
    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise PdfParseError(f"invalid PDF: {exc}") from exc
    except Exception as exc:
        raise PdfParseError(f"invalid PDF: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            unlocked = reader.decrypt("")
        except Exception as exc:  # pragma: no cover - exotic encryption
            raise PdfParseError(f"encrypted PDF: {exc}") from exc
        if not unlocked:
            raise PdfParseError("encrypted PDF: password required")

    try:
        count = len(reader.pages)
    except Exception as exc:
        raise PdfParseError(f"cannot read PDF page tree: {exc}") from exc

    pages: list[PageText] = []
    for index in range(count):
        try:
            page = reader.pages[index]
        except Exception as exc:  # pragma: no cover - malformed page tree
            raise PdfParseError(f"cannot read page {index + 1}: {exc}") from exc
        pages.append(PageText(page=index + 1, text=_page_text(page)))
    return pages


def extract_text(pages: Iterable[PageText]) -> str:
    """Join page texts with blank lines (convenience for metadata parsing)."""
    return "\n\n".join(page.text for page in pages if page.text)


@dataclass(slots=True)
class SizedLine:
    """One text line with the font size it was rendered at."""

    text: str
    size: float


def extract_sized_lines(data: bytes, page_index: int = 0) -> list[SizedLine]:
    """Return the lines of one page together with their font size.

    Titles are rendered in the largest font on page 1, so metadata extraction
    uses this instead of guessing from line lengths. Returns an empty list when
    the page has no usable text layer.
    """
    if not data:
        return []
    try:
        reader = PdfReader(io.BytesIO(data))
        if page_index >= len(reader.pages):
            return []
        page = reader.pages[page_index]
    except Exception as exc:  # noqa: BLE001 - metadata extraction is best effort
        logger.warning("sized line extraction failed: %s", exc)
        return []

    fragments: list[tuple[str, float]] = []

    def visitor(text, cm, tm, font_dict, font_size) -> None:  # noqa: ANN001, ARG001
        if text:
            try:
                size = float(font_size or 0.0)
            except (TypeError, ValueError):
                size = 0.0
            fragments.append((text, size))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception as exc:  # noqa: BLE001
        logger.warning("visitor text extraction failed: %s", exc)
        return []

    lines: list[SizedLine] = []
    buffer: list[str] = []
    sizes: list[float] = []

    def flush() -> None:
        if buffer:
            text = re.sub(r"\s+", " ", "".join(buffer)).strip()
            if text:
                lines.append(SizedLine(text=text, size=max(sizes) if sizes else 0.0))
        buffer.clear()
        sizes.clear()

    for text, size in fragments:
        for index, piece in enumerate(text.split("\n")):
            if index > 0:
                flush()
            if piece.strip():
                buffer.append(piece)
                sizes.append(size)
    flush()
    return lines
