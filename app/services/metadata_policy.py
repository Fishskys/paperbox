"""Shared authority and conservative value comparison for merging and review."""

from collections.abc import Mapping
from datetime import date, datetime
import html
import re
from typing import Any
import unicodedata

from app.services import metadata_identifiers as identifiers
from app.services import metadata_sources as sources

SOURCE_AUTHORITY = {
    sources.SOURCE_TYPE_MANUAL: 3,
    sources.SOURCE_TYPE_IEEE_API: 2,
    sources.SOURCE_TYPE_ARXIV_API: 2,
    sources.SOURCE_TYPE_CROSSREF: 2,
    sources.SOURCE_TYPE_IMPORT_FILE: 2,
    sources.SOURCE_TYPE_PDF_EMBEDDED: 1,
    sources.SOURCE_TYPE_PDF_HEURISTIC: 0,
    sources.SOURCE_TYPE_FILENAME: 0,
}
TITLE_TRUST = {**SOURCE_AUTHORITY, sources.SOURCE_TYPE_PDF_HEURISTIC: 1}
HISTORY_ONLY_SOURCES = (sources.SOURCE_TYPE_PDF_HEURISTIC, sources.SOURCE_TYPE_MANUAL)
_PRESENTATION_TAG = re.compile(r"</?(?:sup|sub|inf|italic|italics|i|b|bold|em|strong)(?:\s[^<>]*?)?\s*/?>", re.I)
_TYPOGRAPHY = str.maketrans({"–": "-", "—": "-", "‐": "-", "‑": "-", "‘": "'", "’": "'", "“": '"', "”": '"'})


def source_authority(source_type: str | None) -> int | None:
    return SOURCE_AUTHORITY.get((source_type or "").strip().lower())


def title_trust(source_type: str | None) -> int | None:
    return TITLE_TRUST.get((source_type or "").strip().lower())


def field_authority(field: str, source_type: str | None) -> int | None:
    return title_trust(source_type) if field == "title" else source_authority(source_type)


def _canonical(field: str, value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _canonical(field, item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_canonical(field, item) for item in value]
        # Tags are sets; author order and cardinality remain meaningful.
        return sorted(set(items)) if field.startswith("tag:") and all(isinstance(x, str) for x in items) else items
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    if isinstance(value, str):
        if field.startswith("identifier:"):
            try:
                return identifiers.normalize_identifier(field.split(":", 1)[1], value) or value
            except ValueError:
                return value
        value = _PRESENTATION_TAG.sub("", html.unescape(value))
        value = " ".join(unicodedata.normalize("NFKC", value).translate(_TYPOGRAPHY).split())
        return value.casefold() if field in ("title", "authors", "venue", "language") or field.startswith("tag:") else value
    return value


def equivalent_values(field: str, left: Any, right: Any) -> bool:
    return _canonical(field, left) == _canonical(field, right)


def prefers_incoming(field: str, current: Any, incoming: Any) -> bool:
    if field == "abstract":
        return len(str(incoming)) > len(str(current))
    if field == "authors":
        return isinstance(current, (list, tuple)) and isinstance(incoming, (list, tuple)) and len(incoming) > len(current)
    return False


def needs_review(field: str, kept: Any, rejected: Any, kept_source: str | None, rejected_source: str | None, *, resolved_by_rule: bool = False) -> bool:
    if (rejected_source or "").strip().lower() in HISTORY_ONLY_SOURCES:
        return False
    if equivalent_values(field, kept, rejected):
        return False
    kept_rank = field_authority(field, kept_source)
    rejected_rank = field_authority(field, rejected_source)
    if kept_rank is None or rejected_rank is None:
        return True
    if kept_rank > rejected_rank:
        return False
    if resolved_by_rule and kept_rank == rejected_rank and kept_rank > 0 and prefers_incoming(field, rejected, kept):
        return False
    # A higher-priority historical claim may expose a wrongly selected current value.
    return True
