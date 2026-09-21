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
from xml.etree import ElementTree

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


# --------------------------------------------------------------------------- #
# embedded metadata (Info dictionary + XMP) -- discovery layer 1, no network
# --------------------------------------------------------------------------- #
#: XMP namespaces whose elements carry bibliographic values.
_XMP_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "prism": "http://prismstandard.org/namespaces/basic/2.0/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
}

_DOI_IN_TEXT = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", re.IGNORECASE)
_ARXIV_IN_TEXT = re.compile(
    r"(?:arxiv[:\s/]*|abs/)([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Za-z\-]+)?/\d{7}(?:v\d+)?)",
    re.IGNORECASE,
)
_ISBN_IN_TEXT = re.compile(r"\b97[89][-\s]?(?:\d[-\s]?){9}\d\b")
_ISSN_IN_TEXT = re.compile(r"\b\d{4}-\d{3}[\dXx]\b")

#: XML namespace of the ``xml:lang`` attribute on ``rdf:li`` items.
_XML_NS = "http://www.w3.org/XML/1998/namespace"

#: XMP element names (namespace-local) that carry bibliographic values, plus the
#: pypdf accessor used when the packet itself cannot be parsed.
_XMP_ACCESSORS: dict[str, str] = {
    "title": "dc_title",
    "creator": "dc_creator",
    "description": "dc_description",
    "identifier": "dc_identifier",
    "language": "dc_language",
    "subject": "dc_subject",
    "Keywords": "pdf_keywords",
}
_XMP_LOCAL_NAMES: tuple[str, ...] = (
    "title",
    "creator",
    "description",
    "identifier",
    "language",
    "subject",
    "Keywords",
    "doi",
    "publicationName",
    "volume",
    "number",
    "startingPage",
    "endingPage",
    "publicationDate",
    "issn",
    "aggregationType",
)


@dataclass(slots=True)
class EmbeddedMetadata:
    """What a PDF says about itself in its Info dictionary / XMP packet.

    Every field is optional: plenty of PDFs carry only a producer string. The raw
    dictionary is kept so the value can be stored verbatim in
    ``paper_sources.raw`` (``source_type='pdf_embedded'``) and replayed later.
    """

    title: str | None = None
    authors: list[str] | None = None
    abstract: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    venue: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publication_date: str | None = None
    year: int | None = None
    language: str | None = None
    keywords: list[str] | None = None
    raw: dict | None = None

    def as_dict(self) -> dict:
        """Only the fields that actually carry a value (plus the raw snapshot)."""
        payload = {
            key: value
            for key, value in (
                ("title", self.title),
                ("authors", self.authors),
                ("abstract", self.abstract),
                ("doi", self.doi),
                ("arxiv_id", self.arxiv_id),
                ("venue", self.venue),
                ("volume", self.volume),
                ("issue", self.issue),
                ("pages", self.pages),
                ("publication_date", self.publication_date),
                ("year", self.year),
                ("language", self.language),
                ("keywords", self.keywords),
            )
            if value not in (None, "", [], {})
        }
        payload["raw"] = self.raw or {}
        return payload

    @property
    def is_empty(self) -> bool:
        """True when the PDF told us nothing usable."""
        return not any(
            value not in (None, "", [], {})
            for value in (
                self.title,
                self.authors,
                self.abstract,
                self.doi,
                self.arxiv_id,
                self.venue,
                self.volume,
                self.issue,
                self.pages,
                self.publication_date,
                self.language,
                self.keywords,
            )
        )


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [_text(item) for item in value]
        joined = ", ".join(part for part in parts if part)
        return joined or None
    text = str(value).strip()
    return text or None


def _xmp_value(xmp, attribute: str) -> object:
    """Read one XMP property, tolerating versions that do not expose it."""
    try:
        return getattr(xmp, attribute, None)
    except Exception:  # noqa: BLE001 - malformed XMP must not break ingestion
        return None


def _xmp_packet(xmp) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    """Parse the XMP packet into ``(simple, alt)`` value maps.

    pypdf only exposes a fixed list of properties (``dc:*``, ``pdf:Keywords``,
    ``xmp:*``) through its accessors; the PRISM namespace -- which is where a
    publisher puts ``doi``, ``volume``, ``publicationName`` and friends -- is not
    among them. Reading the RDF packet directly is what makes those fields usable
    without adding a dependency (``xml.etree`` is stdlib).
    """
    try:
        raw = xmp.stream.get_data()
    except Exception:  # noqa: BLE001 - no stream, nothing to read
        return {}, {}
    try:
        root = ElementTree.fromstring(raw)
    except Exception as exc:  # noqa: BLE001 - malformed packet
        logger.debug("cannot parse the XMP packet: %s", exc)
        return {}, {}

    known = set(_XMP_NS.values())
    simple: dict[str, list[str]] = {}
    alt: dict[str, dict[str, str]] = {}
    for element in root.iter():
        tag = str(element.tag)
        if "}" not in tag:
            continue
        namespace, local = tag[1:].split("}", 1)
        if namespace not in known:
            continue
        items = [
            child
            for child in element.iter()
            if str(child.tag).endswith("}li") and (child.text or "").strip()
        ]
        if items:
            is_alt = any(str(child.tag).endswith("}Alt") for child in element.iter())
            for item in items:
                text = (item.text or "").strip()
                if is_alt:
                    lang = item.get(f"{{{_XML_NS}}}lang") or "x-default"
                    alt.setdefault(local, {})[lang] = text
                else:
                    bucket = simple.setdefault(local, [])
                    if text not in bucket:
                        bucket.append(text)
            continue
        text = (element.text or "").strip()
        if text:
            bucket = simple.setdefault(local, [])
            if text not in bucket:
                bucket.append(text)
    return simple, alt


def _alt_value(alt: dict[str, dict[str, str]], local: str) -> str | None:
    """Preferred value of a language-keyed XMP element (``x-default`` first)."""
    values = alt.get(local)
    if not values:
        return None
    for key in ("x-default", "en-US", "en"):
        if values.get(key):
            return values[key]
    for value in values.values():
        if value:
            return value
    return None


def _simple_value(simple: dict[str, list[str]], local: str) -> str | None:
    values = simple.get(local)
    if not values:
        return None
    return _text(values)


def _split_authors(value: object) -> list[str]:
    """Author names from an XMP sequence or an Info ``/Author`` string.

    A list keeps its items verbatim (``["Alice Smith", "Bob Jones"]``); a string is
    split on ``;``, ``and``, ``&`` and newlines -- never on commas, because
    ``Smith, John`` is one author.
    """
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = _text(value)
    if not text:
        return []
    parts = re.split(r"\s*(?:;|\band\b|&|\n)\s*", text)
    return [part.strip() for part in parts if part.strip()]


def _split_keywords(value: object) -> list[str]:
    text = _text(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,;]", text) if part.strip()]


def _first_match(pattern: re.Pattern[str], *texts: str | None) -> str | None:
    """First match across ``texts``; a capture group wins over the whole match."""
    for text in texts:
        if not text:
            continue
        match = pattern.search(text)
        if match is not None:
            value = match.group(1) if match.groups() else match.group(0)
            return value.strip().rstrip(".,;")
    return None


def _year_from_date(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(19\d{2}|20\d{2})", str(value))
    if match is None:
        return None
    return int(match.group(1))


def _strip_version(arxiv_id: str | None) -> str | None:
    """``1706.03762v5`` -> ``1706.03762`` (the version is not part of the identity)."""
    if not arxiv_id:
        return None
    return re.sub(r"v\d+$", "", arxiv_id.strip(), flags=re.IGNORECASE) or None


def extract_embedded_metadata(data: bytes) -> EmbeddedMetadata:
    """Read the Info dictionary and the XMP packet of a PDF (discovery layer 1).

    Never raises: a broken or missing XMP packet simply contributes nothing, and
    an unreadable PDF returns an empty :class:`EmbeddedMetadata` (the caller still
    has the first-page heuristics as layer 2).
    """
    if not data:
        return EmbeddedMetadata(raw={})
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - metadata is best effort
        logger.debug("cannot read embedded metadata: %s", exc)
        return EmbeddedMetadata(raw={})
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            return EmbeddedMetadata(raw={})

    info: dict[str, object] = {}
    try:
        raw_info = reader.metadata or {}
        info = {str(key): _text(value) for key, value in raw_info.items()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("cannot read the PDF info dictionary: %s", exc)

    xmp = None
    try:
        xmp = reader.xmp_metadata
    except Exception as exc:  # noqa: BLE001
        logger.debug("cannot read the PDF XMP packet: %s", exc)

    simple: dict[str, list[str]] = {}
    alt: dict[str, dict[str, str]] = {}
    if xmp is not None:
        simple, alt = _xmp_packet(xmp)

    def xmp_text(local: str) -> str | None:
        """A ``dc``/``prism`` value, preferring the packet over pypdf accessors."""
        value = _alt_value(alt, local) or _simple_value(simple, local)
        if value:
            return value
        # Fallback: pypdf's own accessors (used when the packet is unparseable).
        accessor = _XMP_ACCESSORS.get(local)
        if accessor is None or xmp is None:
            return None
        raw = _xmp_value(xmp, accessor)
        if isinstance(raw, dict):
            return _alt_value({local: raw}, local)
        return _text(raw)

    xmp_fields: dict[str, object] = {}
    for local in _XMP_LOCAL_NAMES:
        value = xmp_text(local)
        if value:
            xmp_fields[local] = value
    if xmp is not None:
        try:
            for key, value in dict(xmp.custom_properties or {}).items():
                xmp_fields.setdefault(f"custom:{key}", _text(value))
        except Exception:  # noqa: BLE001 - pdfx extensions are optional
            pass

    searchable = " ".join(
        str(value) for value in list(info.values()) + list(xmp_fields.values()) if value
    )
    title = xmp_text("title") or _text(info.get("/Title")) or _text(info.get("Title"))
    creators = _xmp_value(xmp, "dc_creator") if xmp is not None else None
    if not creators:
        creators = simple.get("creator")
    authors = _split_authors(creators) or _split_authors(
        info.get("/Author") or info.get("Author")
    )
    abstract = xmp_text("description") or _text(info.get("/Subject"))
    keywords = _split_keywords(
        xmp_text("Keywords")
        or xmp_text("subject")
        or info.get("/Keywords")
    )
    start_page = xmp_text("startingPage")
    end_page = xmp_text("endingPage")
    pages = f"{start_page}-{end_page}" if start_page and end_page else start_page
    publication_date = xmp_text("publicationDate") or _text(info.get("/CreationDate"))
    issns = _ISSN_IN_TEXT.findall(searchable)
    if xmp_text("issn"):
        issns.extend(_ISSN_IN_TEXT.findall(str(xmp_text("issn"))))

    metadata = EmbeddedMetadata(
        title=title,
        authors=authors,
        abstract=abstract,
        doi=_first_match(
            _DOI_IN_TEXT,
            xmp_text("doi"),
            xmp_text("identifier"),
            title,
            searchable,
        ),
        arxiv_id=_strip_version(_first_match(_ARXIV_IN_TEXT, searchable)),
        venue=xmp_text("publicationName"),
        volume=xmp_text("volume"),
        issue=xmp_text("number"),
        pages=pages,
        publication_date=publication_date,
        year=_year_from_date(publication_date),
        language=xmp_text("language"),
        keywords=keywords,
        raw={
            "info": {key: value for key, value in info.items() if value},
            "xmp": {key: value for key, value in xmp_fields.items() if value},
            "issns": issns or None,
            "isbns": _ISBN_IN_TEXT.findall(searchable) or None,
        },
    )
    return metadata


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
