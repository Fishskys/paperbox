"""Shared authority and conservative value comparison for merging and review."""

from collections.abc import Mapping
from datetime import date, datetime
import html
import re
from typing import Any
import unicodedata

from app.services import metadata_identifiers as identifiers
from app.services import metadata_sources as sources
from app.parsing.metadata_fields import abstract_problem, authors_problem, corroborates

QUALITY_VERSION = "pdf-fields-v4"
PDF_SOURCES = (sources.SOURCE_TYPE_PDF_EMBEDDED, sources.SOURCE_TYPE_PDF_HEURISTIC)
LOCAL_SOURCES = (*PDF_SOURCES, sources.SOURCE_TYPE_FILENAME)


def pdf_choice(field: str, current: Any, incoming: Any, kept_source: str | None, incoming_source: str | None,
               *, title: str | None = None, evidence: dict | None = None) -> tuple[str, str] | None:
    """Choose only from semantic validity or corroboration, never raw length."""
    if field not in ("abstract", "authors") or incoming_source not in PDF_SOURCES:
        return None
    evidence = evidence or {}
    problem = abstract_problem if field == "abstract" else lambda v: authors_problem(v, title=title)
    incoming_problem = problem(incoming)
    if incoming_problem:
        return "reject", incoming_problem
    if current not in (None, "", [], {}) and kept_source not in PDF_SOURCES:
        return None  # Bibliographic sources and manual edits retain their authority.
    if (field == "authors" and incoming_source == sources.SOURCE_TYPE_PDF_HEURISTIC
            and evidence and not evidence.get("byline_complete")):
        return "current", "unverified_byline_boundary"
    if current in (None, "", [], {}):
        return "incoming", "valid_blank_fill"
    if evidence.get("text_state", "ok") != "ok":
        return "current", "unreliable_text_layer"
    if field == "authors":
        byline = evidence.get("byline") or []
        if evidence.get("byline_complete") and byline and not authors_problem(byline, title=title):
            if byline_abbreviations(current, incoming):
                covers = lambda names: (equivalent_values("authors", names, byline)
                                        or byline_abbreviations(names, byline, expand_only=True))
                if covers(current) and covers(incoming):
                    return "current", "corroborated_author_abbreviation"
                return "current", "unverified_author_expansion"
            matches = lambda names: equivalent_values("authors", names, byline)
            if matches(incoming) and not matches(current):
                if (not problem(current) and len(current) > len(byline)
                        and all(any(equivalent_values("authors", [name], [other]) for other in current) for name in byline)):
                    return "current", "possibly_incomplete_byline"
                return "incoming", "corroborated_complete_byline"
            if matches(current):
                return "current", "preserve_corroborated_byline"
        return "current", "unverified_author_difference"
    body = evidence.get("abstract")
    if body and not abstract_problem(body):
        current_matches = corroborates(str(current), str(body))
        incoming_matches = corroborates(str(incoming), str(body))
        if evidence.get("abstract_complete"):
            current_full = equivalent_values("abstract", current, body)
            incoming_full = equivalent_values("abstract", incoming, body)
            if current_full and not incoming_full:
                return "current", "preserve_complete_body_abstract"
            if incoming_full and not current_full:
                return "incoming", "corroborated_complete_body_abstract"
        if incoming_matches and not current_matches:
            return "incoming", "corroborated_body_abstract"
        if current_matches and not incoming_matches:
            return "current", "preserve_body_abstract"
    if problem(current):
        return "incoming", "valid_abstract_replaces_invalid"
    return "current", "different_valid_abstracts"

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
        if field == "authors":
            # Ignore a word-final period, preserving dotted initials and word boundaries.
            value = re.sub(r"(?<![\w.])([^\W\d_]{2,})\.(?=\s|$)", r"\1", value)
            value = re.sub(r"(?<![^\W\d_])([^\W\d_]\.)\s+(?=[^\W\d_])", r"\1", value)
        return value.casefold() if field in ("title", "authors", "venue", "language") or field.startswith("tag:") else value
    return value


def equivalent_values(field: str, left: Any, right: Any) -> bool:
    return _canonical(field, left) == _canonical(field, right)


def title_has_markup(value: Any) -> bool:
    return isinstance(value, str) and (
        html.unescape(value) != value or bool(_PRESENTATION_TAG.search(value))
    )


def byline_spelling_variants(left: list[str], right: list[str]) -> bool:
    """Corroborate same-position PDF name variants; never hide their difference."""
    def key(name: str) -> str:
        value = unicodedata.normalize("NFD", html.unescape(name).casefold())
        return "".join(c for c in value if c.isalnum())
    return bool(left) and len(left) == len(right) and all(key(a) == key(b) for a, b in zip(left, right))


def byline_abbreviations(left: list[str], right: list[str], *, expand_only: bool = False) -> bool:
    """Match ordered PDF names to initials; not global equality or identity merging."""
    if not left or len(left) != len(right):
        return False
    expansions: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    expanded = False
    for first, second in zip(left, right):
        parsed = []
        for name in (first, second):
            normalized = _canonical("authors", name)
            words = list(re.finditer(r"[^\W\d_]+(?:'[^\W\d_]+)*", normalized))
            parsed.append((tuple(word[0] for word in words),
                           tuple("-" in normalized[x.end():y.start()] for x, y in zip(words, words[1:]))))
        (a, a_hyphens), (b, b_hyphens) = parsed
        if a_hyphens != b_hyphens:
            return False
        if a == b:
            if not equivalent_values("authors", [first], [second]):
                return False
            continue
        if len(a) < 2 or len(a) != len(b) or a[-1] != b[-1] or len(a[-1]) < 2:
            return False
        for x, y in zip(a[:-1], b[:-1]):
            if x != y and not (len(x) == 1 and y.startswith(x) or len(y) == 1 and x.startswith(y)):
                return False
            if expand_only and len(y) == 1 and len(x) > 1:
                return False
        # Repeated initials may stand for different people even in the same paper.
        for short, full in ((a, b), (b, a)):
            if any(len(x) == 1 and len(y) > 1 for x, y in zip(short[:-1], full[:-1])):
                expansions.setdefault(short, set()).add(full)
        expanded = True
    return expanded and all(len(names) == 1 for names in expansions.values())


def prefers_incoming(field: str, current: Any, incoming: Any) -> bool:
    if field == "abstract":
        prefix = str(current).rstrip()
        if prefix.endswith(("...", "…")):
            prefix = prefix.rstrip(".…").rstrip()
            return bool(prefix) and str(incoming).startswith(prefix) and len(str(incoming)) > len(str(current))
    return False


def needs_review(field: str, kept: Any, rejected: Any, kept_source: str | None, rejected_source: str | None, *, resolved_by_rule: bool = False, evidence: dict | None = None, title: str | None = None) -> bool:
    if equivalent_values(field, kept, rejected):
        return False
    # Extraction candidates are audit evidence, not bibliographic adjudications.
    # This also applies to historical claims written before the current policy.
    if rejected_source in LOCAL_SOURCES and (kept_source in LOCAL_SOURCES or kept in (None, "", [], {})):
        return False
    choice = pdf_choice(field, kept, rejected, kept_source, rejected_source, title=title, evidence=evidence)
    if choice is not None:
        return choice[0] in ("review", "incoming")
    if (rejected_source or "").strip().lower() in HISTORY_ONLY_SOURCES:
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
