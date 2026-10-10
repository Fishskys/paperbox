"""External metadata import: IEEE raw JSON, CSL-JSON and the generic shape.

One importer for every source file, because the *storage model* does not care
which platform produced a record (decision 1). The format is detected from the
payload itself:

* a mapping with ``articles`` -> IEEE Xplore batch JSON;
* a list whose items carry ``DOI``/``type`` -> CSL-JSON;
* anything else (list of mappings) -> this project's generic shape, where the keys
  are the claim field names the ledger already uses.

Each record becomes a :class:`ParsedRecord`: the claim values to merge, the
identifiers to register, and the verbatim payload for ``paper_sources.raw``.
``import_records`` then matches, merges and reports -- writing nothing at all
unless ``apply=True`` (the API and the CLI both default to ``dry_run``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.services import metadata_identifiers as identifiers
from app.services import metadata_matcher as matcher
from app.services import metadata_merge as merge
from app.services import metadata_shell, metadata_sources as sources
from app.services import venue_service as venues

logger = get_logger(__name__)

FORMAT_IEEE_RAW = "ieee_raw"
FORMAT_CSL_JSON = "csl_json"
FORMAT_GENERIC = "generic"

#: IEEE ``index_terms`` keys -> ``papers_tags.kind``.
IEEE_TAG_KINDS: dict[str, str] = {
    "ieee_terms": "ieee_terms",
    "author_terms": "author_terms",
    "dynamic_index_terms": "dynamic_index_terms",
}

#: CSL-JSON ``type`` -> ``papers.paper_type``.
CSL_PAPER_TYPES: dict[str, str] = {
    "article-journal": venues.PAPER_TYPE_JOURNAL,
    "article-magazine": venues.PAPER_TYPE_JOURNAL,
    "paper-conference": venues.PAPER_TYPE_CONFERENCE,
    "proceedings-article": venues.PAPER_TYPE_CONFERENCE,
    "posted-content": venues.PAPER_TYPE_PREPRINT,
    "article": venues.PAPER_TYPE_JOURNAL,
    "standard": venues.PAPER_TYPE_STANDARD,
}

#: 月份词 → 月份号。IEEE 的 ``publication_date`` 用**缩写**（``"17-19 Oct. 2025"``、
#: ``"Feb. 2022"``、``"14-18 Sept. 2015"``），只有少数记录写全名（``"June 2014"``）。
#: 原实现只认全名，于是缩写记录静默丢掉月份（还被 ``digits[:4]`` 取到 ``1719`` 这种
#: 假年份）；全名记录则走上"把数字拼成年份"的崩溃路径。两条一起在 2026-10-10 修。
#: 三字母前缀即可覆盖全名（``june``/``july`` 由 ``jun``/``jul`` 区分，``sept.`` 由 ``sep``）。
_IEEE_MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_IEEE_MONTH_TOKEN_RE = re.compile("|".join(_IEEE_MONTH_NUMBERS))


def _month_in_text(lowered: str) -> int | None:
    """Month number for the first month word in an (already casefolded) date string."""
    match = _IEEE_MONTH_TOKEN_RE.search(lowered)
    return _IEEE_MONTH_NUMBERS[match.group(0)] if match else None


@dataclass
class ParsedRecord:
    """One importable record, normalized to claim values + identifiers."""

    values: dict[str, Any]
    source_ref: str
    raw: dict[str, Any]
    content_type: str | None = None
    title: str | None = None
    authors: list[str] = dataclass_field(default_factory=list)
    year: int | None = None
    filename: str | None = None
    sha256: str | None = None
    fetched_at: datetime | None = None
    format: str = FORMAT_GENERIC

    def identifiers(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for field, value in self.values.items():
            if field.startswith("identifier:") and value:
                found[field.split(":", 1)[1]] = str(value)
        return found

    def match_input(self) -> matcher.MatchInput:
        return matcher.match_input_from_values(
            self.values, source_ref=self.source_ref, filename=self.filename
        )


# --------------------------------------------------------------------------- #
# format detection
# --------------------------------------------------------------------------- #
def detect_format(payload: Any) -> str:
    """Which of the three accepted shapes ``payload`` is."""
    if isinstance(payload, Mapping):
        if isinstance(payload.get("articles"), (list, tuple)):
            return FORMAT_IEEE_RAW
        return FORMAT_GENERIC
    if isinstance(payload, (list, tuple)):
        for item in payload:
            if isinstance(item, Mapping) and ("DOI" in item or "type" in item):
                return FORMAT_CSL_JSON
        return FORMAT_GENERIC
    raise ValueError("unsupported metadata payload: expected an object or an array")


def records_from_payload(payload: Any) -> tuple[str, list[dict[str, Any]]]:
    """``(format, records)`` for a parsed JSON payload."""
    detected = detect_format(payload)
    if detected == FORMAT_IEEE_RAW:
        raw_records = list(payload.get("articles") or [])
    elif isinstance(payload, Mapping):
        raw_records = [dict(payload)]
    else:
        raw_records = list(payload)
    records = [dict(item) for item in raw_records if isinstance(item, Mapping)]
    return detected, records


def load_payload(data: bytes | str) -> Any:
    """Parse JSON bytes/text (accepts a UTF-8 BOM and a bare list)."""
    if isinstance(data, bytes):
        text = data.decode("utf-8-sig", errors="replace")
    else:
        text = data.lstrip("\ufeff")
    return json.loads(text)


# --------------------------------------------------------------------------- #
# small parsing helpers
# --------------------------------------------------------------------------- #
def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [_text(item) for item in value]
        joined = ", ".join(part for part in parts if part)
        return joined or None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def _pages(start: Any, end: Any) -> str | None:
    first, last = _text(start), _text(end)
    if first and last and first != last:
        return f"{first}-{last}"
    return first or last


#: 一个**独立**的四位数字（左右都不能紧邻别的数字）。用它在自由文本里找年份：
#: IEEE 的日期是区间（``"17-19 Oct. 2025"``），把数字拼起来会得到 ``17192025``，
#: 取前四位会得到 ``1719`` —— 两者都不是年份，年份是那个独立的四位数字。
_YEAR_TOKEN_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _year_in_text(text: str) -> int | None:
    """Pick a plausible year out of a free-form date string, or ``None``.

    先认 1900–2199（学术数据的现实范围，且能把 ``"17-19 Oct. 2025"`` 里的 2025
    同 ``1719`` 这类拼接产物区分开），再退回到 1000–2999。多个候选时取最左边那个，
    与旧实现"取前四位"的取向一致（``"2013-2015"`` 仍取 2013）。
    """
    tokens = [int(match.group(1)) for match in _YEAR_TOKEN_RE.finditer(text)]
    for candidate in tokens:
        if 1900 <= candidate <= 2199:
            return candidate
    for candidate in tokens:
        if 1000 <= candidate <= 2999:
            return candidate
    return None


def _month_precision_date(value: Any, year: Any) -> str | None:
    """``"July 2015"`` -> ``"2015-07-01"`` (IEEE reports month precision).

    也接受区间写法（``"17-19 Oct. 2025"`` -> ``"2025-10-01"``）：年份只认那个独立的
    四位数字，**不再把字符串里的数字拼起来**（那会把会议日期算成 ``17192025``，
    让 ``date()`` 抛 ``ValueError`` 并导致整份文件被 422 拒收 —— 2026-10-10 修）。
    """
    text = _text(value)
    fallback_year = _int(year)
    if text:
        lowered = text.casefold()
        month = _month_in_text(lowered)
        found = _year_in_text(text)
        if month is not None:
            chosen = found or fallback_year
            if chosen:
                return date(chosen, month, 1).isoformat()
        elif found:
            return date(found, 1, 1).isoformat()
    if fallback_year:
        return date(fallback_year, 1, 1).isoformat()
    return None


def _year_from(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _year_from(item)
            if found:
                return found
        return None
    if isinstance(value, Mapping):
        for key in ("date-parts", "year", "publication_year", "value"):
            if key in value:
                found = _year_from(value[key])
                if found:
                    return found
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        year = int(value)
        return year if 1000 <= year <= 2999 else None
    text = str(value).strip()
    return _year_in_text(text)


def _record_digest(record: Mapping[str, Any]) -> str:
    payload = json.dumps(record, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# IEEE raw JSON (section 12 of the design: field by field)
# --------------------------------------------------------------------------- #
def parse_ieee_record(record: Mapping[str, Any]) -> ParsedRecord:
    """Map one IEEE Xplore ``article`` object onto claim values."""
    doi = _text(record.get("doi"))
    arxiv_id = _text(record.get("arxiv_id"))
    article_number = record.get("article_number")
    issn = _text(record.get("issn"))
    content_type = _text(record.get("content_type"))
    title = _text(record.get("title"))
    year = _int(record.get("publication_year")) or _year_from(record.get("publication_date"))
    authors = _ieee_authors(record)
    filename = _text(record.get("filename"))
    sha256 = _text(record.get("sha256"))
    path = _text(record.get("path"))

    values: dict[str, Any] = {}
    if title:
        values["title"] = title
    abstract = _text(record.get("abstract"))
    if abstract:
        values["abstract"] = abstract
    if year:
        values["year"] = year
    if authors:
        values["authors"] = authors
    if doi:
        values["identifier:doi"] = doi
    if arxiv_id:
        values["identifier:arxiv"] = arxiv_id
    if article_number not in (None, ""):
        values["identifier:ieee_article_number"] = str(article_number)
    if issn:
        values["identifier:issn"] = issn
    publication_date = _month_precision_date(record.get("publication_date"), year)
    if publication_date:
        values["publication_date"] = publication_date
    volume = _text(record.get("volume"))
    if volume:
        values["volume"] = volume
    issue = _text(record.get("issue"))
    if issue:
        values["issue"] = issue
    pages = _pages(record.get("start_page"), record.get("end_page"))
    if pages:
        values["pages"] = pages
    paper_type = venues.paper_type_for_content_type(content_type)
    if paper_type:
        values["paper_type"] = paper_type
    publication_title = _text(record.get("publication_title"))
    if publication_title:
        venue: dict[str, Any] = {"name": publication_title}
        if year:
            venue["year"] = year
        if content_type:
            venue["content_type"] = content_type
        if issn:
            venue["issn"] = issn
        for source_key, target_key in (
            ("conference_location", "location"),
            ("conference_dates", "dates"),
            ("publication_number", "publication_number"),
            ("is_number", "is_number"),
        ):
            value = _text(record.get(source_key))
            if value:
                venue[target_key] = value
        values["venue"] = venue
    url = _text(record.get("html_url")) or _text(record.get("abstract_url")) or _text(
        record.get("pdf_url")
    )
    if url:
        values["url"] = url
    values.update(_ieee_index_terms(record))

    return ParsedRecord(
        values=values,
        source_ref=_source_ref(doi=doi, arxiv_id=arxiv_id, article_number=article_number, path=path, sha256=sha256, record=record),
        raw=dict(record),
        content_type=content_type,
        title=title,
        authors=authors,
        year=year,
        filename=filename,
        sha256=sha256,
        fetched_at=_fetched_at(record),
        format=FORMAT_IEEE_RAW,
    )


def _ieee_authors(record: Mapping[str, Any]) -> list[str]:
    """``authors[].full_name`` in ``author_order``; affiliations are not stored."""
    raw_authors = record.get("authors") or []
    if isinstance(raw_authors, str):
        return [part.strip() for part in raw_authors.split(";") if part.strip()]
    entries: list[tuple[int, str]] = []
    for index, item in enumerate(raw_authors):
        if isinstance(item, Mapping):
            name = _text(item.get("full_name")) or _text(item.get("name"))
            order = _int(item.get("author_order"))
            if name:
                entries.append((order if order is not None else index, name))
        else:
            name = _text(item)
            if name:
                entries.append((index, name))
    entries.sort(key=lambda pair: pair[0])
    return [name for _order, name in entries]


def _ieee_index_terms(record: Mapping[str, Any]) -> dict[str, Any]:
    """``index_terms`` -> ``tag:<kind>`` claims (decision 10)."""
    terms: dict[str, Any] = {}
    raw_terms = record.get("index_terms")
    if not isinstance(raw_terms, Mapping):
        return terms
    for key, kind in IEEE_TAG_KINDS.items():
        entry = raw_terms.get(key)
        if isinstance(entry, Mapping):
            values = entry.get("terms") or []
        else:
            values = entry or []
        if isinstance(values, str):
            values = [values]
        cleaned = [str(item).strip() for item in values if str(item or "").strip()]
        if cleaned:
            terms[f"tag:{kind}"] = cleaned
    return terms


def _fetched_at(record: Mapping[str, Any]) -> datetime | None:
    for key in ("insert_date", "fetched_at", "retrieved_at"):
        raw = record.get(key)
        if not raw:
            continue
        text = str(raw).strip().replace("Z", "+00:00")
        for candidate in (text, text[:19], text[:10]):
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------- #
# CSL-JSON
# --------------------------------------------------------------------------- #
def parse_csl_record(record: Mapping[str, Any]) -> ParsedRecord:
    """Map one CSL-JSON item onto claim values (the exchange format)."""
    doi = _text(record.get("DOI"))
    title = _text(record.get("title"))
    year = _year_from(record.get("issued")) or _year_from(record.get("published"))
    authors = _csl_authors(record)
    container = _text(record.get("container-title"))
    content_type = _text(record.get("type"))
    issn = _text(record.get("ISSN"))

    values: dict[str, Any] = {}
    if title:
        values["title"] = title
    abstract = _text(record.get("abstract"))
    if abstract:
        values["abstract"] = abstract
    if year:
        values["year"] = year
    if authors:
        values["authors"] = authors
    if doi:
        values["identifier:doi"] = doi
    if issn:
        values["identifier:issn"] = issn
    for key, target in (("volume", "volume"), ("issue", "issue"), ("page", "pages")):
        value = _text(record.get(key))
        if value:
            values[target] = value
    paper_type = CSL_PAPER_TYPES.get((content_type or "").casefold())
    if paper_type:
        values["paper_type"] = paper_type
    if container:
        venue: dict[str, Any] = {"name": container}
        if year:
            venue["year"] = year
        if issn:
            venue["issn"] = issn
        values["venue"] = venue
    url = _text(record.get("URL"))
    if url:
        values["url"] = url
    keywords = record.get("keyword")
    if isinstance(keywords, str):
        keywords = [part.strip() for part in keywords.split(",") if part.strip()]
    if keywords:
        values["tag:author_terms"] = [str(item).strip() for item in keywords if str(item or "").strip()]

    return ParsedRecord(
        values=values,
        source_ref=_source_ref(doi=doi, arxiv_id=None, article_number=None, path=None, sha256=None, record=record),
        raw=dict(record),
        content_type=content_type,
        title=title,
        authors=authors,
        year=year,
        format=FORMAT_CSL_JSON,
    )


def _csl_authors(record: Mapping[str, Any]) -> list[str]:
    raw = record.get("author") or []
    names: list[str] = []
    for item in raw:
        if isinstance(item, Mapping):
            literal = _text(item.get("literal"))
            if literal:
                names.append(literal)
                continue
            family = _text(item.get("family"))
            given = _text(item.get("given"))
            name = " ".join(part for part in (given, family) if part)
            if name:
                names.append(name)
        else:
            name = _text(item)
            if name:
                names.append(name)
    return names


# --------------------------------------------------------------------------- #
# this project's generic shape
# --------------------------------------------------------------------------- #
#: Generic keys that are claim fields verbatim.
_GENERIC_FIELDS: tuple[str, ...] = (
    "title",
    "abstract",
    "year",
    "authors",
    "venue",
    "volume",
    "issue",
    "pages",
    "publication_date",
    "paper_type",
    "language",
    "url",
    "identifier:doi",
    "identifier:arxiv",
    "identifier:ieee_article_number",
    "identifier:issn",
    "tag:ieee_terms",
    "tag:author_terms",
    "tag:dynamic_index_terms",
    "tag:source_tag",
)

#: Friendly aliases accepted in the generic shape.
_GENERIC_ALIASES: dict[str, str] = {
    "doi": "identifier:doi",
    "arxiv_id": "identifier:arxiv",
    "arxiv": "identifier:arxiv",
    "article_number": "identifier:ieee_article_number",
    "ieee_article_number": "identifier:ieee_article_number",
    "issn": "identifier:issn",
    "journal": "venue",
    "conference": "venue",
    "type": "paper_type",
    "content_type": "paper_type",
    "start_page": "pages",
    "keywords": "tag:author_terms",
    "tags": "tag:source_tag",
}


def parse_generic_record(record: Mapping[str, Any]) -> ParsedRecord:
    """The generic shape: keys are the claim field names (plus a few aliases)."""
    values: dict[str, Any] = {}
    for key, value in record.items():
        target = key if key in _GENERIC_FIELDS else _GENERIC_ALIASES.get(key)
        if target is None or value in (None, "", [], {}):
            continue
        if target in values and target == "pages":
            continue
        values[target] = value

    authors = values.get("authors") or []
    if isinstance(authors, str):
        authors = [part.strip() for part in authors.split(";") if part.strip()]
        values["authors"] = authors
    title = _text(values.get("title"))
    year = _year_from(values.get("year"))
    if year:
        values["year"] = year
    filename = _text(record.get("filename"))
    sha256 = _text(record.get("sha256"))
    path = _text(record.get("path"))

    return ParsedRecord(
        values=values,
        source_ref=_source_ref(
            doi=_text(values.get("identifier:doi")),
            arxiv_id=_text(values.get("identifier:arxiv")),
            article_number=values.get("identifier:ieee_article_number"),
            path=path,
            sha256=sha256,
            record=record,
        ),
        raw=dict(record),
        content_type=_text(record.get("content_type")),
        title=title,
        authors=list(authors),
        year=year,
        filename=filename,
        sha256=sha256,
        format=FORMAT_GENERIC,
    )


def parse_record(record: Mapping[str, Any], fmt: str) -> ParsedRecord:
    """Dispatch to the parser of ``fmt``."""
    if fmt == FORMAT_IEEE_RAW:
        return parse_ieee_record(record)
    if fmt == FORMAT_CSL_JSON:
        return parse_csl_record(record)
    return parse_generic_record(record)


def parse_records(payload: Any) -> list[ParsedRecord]:
    """Parse a whole payload (any of the three shapes)."""
    fmt, records = records_from_payload(payload)
    return [parse_record(record, fmt) for record in records]


def _source_ref(
    *,
    doi: str | None,
    arxiv_id: str | None,
    article_number: Any,
    path: str | None,
    sha256: str | None,
    record: Mapping[str, Any],
) -> str:
    """Stable identity of a record: DOI > arXiv > IEEE number > file > digest."""
    for candidate in (
        sources.doi_ref(doi),
        sources.arxiv_ref(arxiv_id),
        sources.ieee_ref(article_number),
    ):
        if candidate:
            return candidate
    if path:
        return sources.file_ref(path, sha256)
    return f"record:{_record_digest(record)[:32]}"


# --------------------------------------------------------------------------- #
# the import itself
# --------------------------------------------------------------------------- #
@dataclass
class ImportReport:
    """The report ``POST /api/metadata/import`` and the CLI return."""

    total: int = 0
    matched: int = 0
    created_shell: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    unchanged: int = 0
    conflicts: list[dict[str, Any]] = dataclass_field(default_factory=list)
    sources: list[dict[str, Any]] = dataclass_field(default_factory=list)
    dry_run: bool = True
    format: str = FORMAT_GENERIC

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "matched": self.matched,
            "created_shell": self.created_shell,
            "ambiguous": self.ambiguous,
            "unmatched": self.unmatched,
            "unchanged": self.unchanged,
            "conflicts": self.conflicts,
            "sources": self.sources,
            "dry_run": self.dry_run,
            "format": self.format,
        }


def import_records(
    session: Session,
    records: Sequence[ParsedRecord],
    *,
    apply: bool = False,
    source_type: str = sources.SOURCE_TYPE_IMPORT_FILE,
    importer: str = "metadata_import",
    limit: int | None = None,
) -> ImportReport:
    """Match, merge and report on ``records``; write only when ``apply`` is true.

    Idempotency comes from ``paper_sources``: a record whose ``(source_type,
    source_ref)`` is already stored is counted as ``unchanged`` and left alone, so
    re-running the same import cannot duplicate anything.
    """
    selected = list(records)
    if limit is not None:
        selected = selected[: max(0, limit)]
    report = ImportReport(total=len(selected), dry_run=not apply)
    if selected:
        report.format = selected[0].format

    for parsed in selected:
        _import_one(
            session,
            parsed,
            report,
            apply=apply,
            source_type=source_type,
            importer=importer,
        )
    if apply:
        session.flush()
    return report


def _import_one(
    session: Session,
    parsed: ParsedRecord,
    report: ImportReport,
    *,
    apply: bool,
    source_type: str,
    importer: str,
) -> None:
    existing = sources.find_source(session, source_type, parsed.source_ref)
    if (
        existing is not None
        and existing.paper_id
        and (existing.paper is None or existing.paper.deleted_at is None)
    ):
        # "already imported" only counts when the record still points at a live
        # paper: a soft-deleted one released its identifiers (deletion frees
        # them) and the re-imported PDF matches a NEW paper -- the stale
        # pointer must not pin the record forever (review 2026-10-05, P1-6).
        report.unchanged += 1
        report.sources.append(
            {
                "source_ref": parsed.source_ref,
                "match_status": existing.match_status,
                "paper_id": existing.paper_id,
                "match_method": existing.match_method,
                "note": "already imported",
            }
        )
        return

    result = matcher.match_record(session, parsed.match_input())
    if result.matched and result.paper is not None:
        report.matched += 1
        if not apply:
            # The dry run still reports what *would* change: the merge decisions
            # are computed against the live rows without writing anything.
            decisions = merge.merge_values(
                session,
                result.paper,
                parsed.values,
                source_type=source_type,
                dry_run=True,
            )
            report.conflicts.extend(
                {**item, "paper_id": result.paper.id}
                for item in merge.conflict_report(decisions)
            )
            report.sources.append(
                {
                    "source_ref": parsed.source_ref,
                    "match_status": sources.MATCH_STATUS_MATCHED,
                    "paper_id": result.paper.id,
                    "match_method": result.method,
                    "match_confidence": result.confidence,
                }
            )
            return
        source = sources.upsert_source(
            session,
            source_type=source_type,
            source_ref=parsed.source_ref,
            raw=parsed.raw,
            paper_id=result.paper.id,
            content_type=parsed.content_type,
            match_status=sources.MATCH_STATUS_MATCHED,
            match_method=result.method,
            match_confidence=result.confidence,
            fetched_at=parsed.fetched_at,
            importer=importer,
        )
        decisions = merge.merge_values(
            session,
            result.paper,
            parsed.values,
            source_type=source_type,
            source_id=source.id,
            confidence=result.confidence,
        )
        identifiers.refresh_primary(session, result.paper.id)
        identifiers.mirror_legacy_columns(session, result.paper)
        # An imported identifier is what upgrades a sha256/title fingerprint to the
        # real identity of the paper (decision 2); a collision is reported rather
        # than merged silently.
        _fingerprint, conflicting_id = identifiers.upgrade_fingerprint(
            session, result.paper, sha256=parsed.sha256
        )
        if conflicting_id:
            report.conflicts.append(
                {
                    "field": "fingerprint",
                    "kept": result.paper.fingerprint,
                    "rejected": identifiers.primary_fingerprint(
                        session, result.paper, sha256=parsed.sha256
                    ),
                    "source": source_type,
                    "reason": f"already claimed by paper {conflicting_id}",
                    "paper_id": result.paper.id,
                }
            )
        report.conflicts.extend(
            {**item, "paper_id": result.paper.id} for item in merge.conflict_report(decisions)
        )
        report.sources.append(
            {
                "source_ref": parsed.source_ref,
                "source_id": source.id,
                "match_status": sources.MATCH_STATUS_MATCHED,
                "paper_id": result.paper.id,
                "match_method": result.method,
                "match_confidence": result.confidence,
            }
        )
        return

    if result.status == matcher.STATUS_AMBIGUOUS:
        report.ambiguous += 1
        if apply:
            source = sources.upsert_source(
                session,
                source_type=source_type,
                source_ref=parsed.source_ref,
                raw=parsed.raw,
                content_type=parsed.content_type,
                match_status=sources.MATCH_STATUS_AMBIGUOUS,
                match_method=result.method,
                match_confidence=result.confidence,
                fetched_at=parsed.fetched_at,
                importer=importer,
            )
            report.sources.append(
                {
                    "source_ref": parsed.source_ref,
                    "source_id": source.id,
                    "match_status": sources.MATCH_STATUS_AMBIGUOUS,
                    "paper_id": None,
                    "match_method": result.method,
                    "candidates": result.candidates,
                }
            )
        else:
            report.sources.append(
                {
                    "source_ref": parsed.source_ref,
                    "match_status": sources.MATCH_STATUS_AMBIGUOUS,
                    "paper_id": None,
                    "match_method": result.method,
                    "candidates": result.candidates,
                }
            )
        return

    # Nothing matched: this is the metadata-first order -- create a shell paper
    # (or, on a dry run, report that one would be created).
    report.created_shell += 1
    if not apply:
        report.sources.append(
            {
                "source_ref": parsed.source_ref,
                "match_status": sources.MATCH_STATUS_MATCHED,
                "paper_id": None,
                "match_method": "shell",
                "note": "would create a shell paper (AWAITING_FILE)",
            }
        )
        return

    values = metadata_shell.shell_values_from_match(
        parsed.values, fallback_title=parsed.filename
    )
    paper, source = metadata_shell.create_shell(
        session,
        values,
        source_type=source_type,
        source_ref=parsed.source_ref,
        raw=parsed.raw,
        content_type=parsed.content_type,
        importer=importer,
    )
    report.sources.append(
        {
            "source_ref": parsed.source_ref,
            "source_id": source.id,
            "match_status": sources.MATCH_STATUS_MATCHED,
            "paper_id": paper.id,
            "match_method": "shell",
        }
    )


def import_payload(
    session: Session,
    payload: Any,
    *,
    apply: bool = False,
    source_type: str = sources.SOURCE_TYPE_IMPORT_FILE,
    importer: str = "metadata_import",
    limit: int | None = None,
) -> ImportReport:
    """Parse a payload and import it (the API/CLI entry point)."""
    parsed = parse_records(payload)
    return import_records(
        session,
        parsed,
        apply=apply,
        source_type=source_type,
        importer=importer,
        limit=limit,
    )


def import_file(
    session: Session,
    path,
    *,
    apply: bool = False,
    source_type: str = sources.SOURCE_TYPE_IMPORT_FILE,
    importer: str = "metadata_import",
    limit: int | None = None,
) -> ImportReport:
    """Read a JSON file and import it."""
    from pathlib import Path

    data = Path(path).read_bytes()
    return import_payload(
        session,
        load_payload(data),
        apply=apply,
        source_type=source_type,
        importer=importer,
        limit=limit,
    )


def formats_of(records: Iterable[ParsedRecord]) -> set[str]:
    """Distinct formats in a parsed batch (reporting helper)."""
    return {record.format for record in records}


__all__ = [
    "CSL_PAPER_TYPES",
    "FORMAT_CSL_JSON",
    "FORMAT_GENERIC",
    "FORMAT_IEEE_RAW",
    "IEEE_TAG_KINDS",
    "ImportReport",
    "ParsedRecord",
    "detect_format",
    "formats_of",
    "import_file",
    "import_payload",
    "import_records",
    "load_payload",
    "parse_csl_record",
    "parse_generic_record",
    "parse_ieee_record",
    "parse_record",
    "parse_records",
    "records_from_payload",
]