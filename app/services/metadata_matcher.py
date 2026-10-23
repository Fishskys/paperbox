"""Matching an external record against the papers already in the library.

Five steps, first hit wins (docs/architecture/metadata-architecture.md section 7):

======  ==================================================  =========  =============
step    evidence                                            confidence  outcome
======  ==================================================  =========  =============
1       ``scheme + normalized_value`` in paper_identifiers  1.0        matched
2       ``paper_files.sha256``                              1.0        matched
3       normalized title + first author + year              0.8        matched
4       title only (or title + one signal)                  0.5        ambiguous
5       normalized file name                                0.5        ambiguous
6       nothing                                              --         unmatched
======  ==================================================  =========  =============

Step 4 never attaches automatically: with no UI to confirm a guess, a wrong
attachment is worse than a review-queue entry. Step 6 leaves the decision to the
caller -- ``apply`` creates a shell paper (``AWAITING_FILE``), ``dry_run`` reports
it as unmatched.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field
from pathlib import PurePath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper, PaperFile, PaperIdentifier, PaperSource, new_uuid
from app.services import metadata_identifiers as ids
from app.services import metadata_merge
from app.services import metadata_service
from app.services import paper_service

logger = get_logger(__name__)

STATUS_MATCHED = "matched"
STATUS_PENDING = "pending"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_REJECTED = "rejected"
STATUS_UNMATCHED = "unmatched"

MATCH_STATUSES: tuple[str, ...] = (
    STATUS_MATCHED,
    STATUS_PENDING,
    STATUS_AMBIGUOUS,
    STATUS_REJECTED,
)

METHOD_DOI = "doi"
METHOD_ARXIV = "arxiv"
METHOD_IEEE_ARTICLE_NUMBER = "ieee_article_number"
METHOD_SHA256 = "sha256"
METHOD_FILENAME_DOI_TITLE = "filename_doi_title"
METHOD_FILENAME_IEEE_ARTICLE_NUMBER_TITLE = "filename_ieee_article_number_title"
METHOD_IDENTIFIER_CONFLICT = "identifier_conflict"
METHOD_TITLE_YEAR_AUTHOR = "title_year_author"
METHOD_TITLE = "title"
METHOD_FILENAME = "filename"
METHOD_MANUAL = "manual"

CONFIDENCE_IDENTIFIER = 1.0
CONFIDENCE_WEAK_IDENTIFIER = 0.95
CONFIDENCE_SHA256 = 1.0
CONFIDENCE_FILENAME_DOI_TITLE = 0.95
CONFIDENCE_FILENAME_IEEE_ARTICLE_NUMBER_TITLE = 0.9
CONFIDENCE_TITLE_YEAR_AUTHOR = 0.8
CONFIDENCE_TITLE_ONLY = 0.5
CONFIDENCE_FILENAME = 0.5

#: Identifier schemes worth probing in step 1, in priority order.
_MATCH_SCHEMES: tuple[tuple[str, str], ...] = (
    (ids.SCHEME_DOI, METHOD_DOI),
    (ids.SCHEME_ARXIV, METHOD_ARXIV),
    (ids.SCHEME_IEEE_ARTICLE_NUMBER, METHOD_IEEE_ARTICLE_NUMBER),
)

#: Max number of ``paper_id IN (...)`` values per exact-title lookup (kept small so
#: the follow-up ``SELECT`` never builds an oversized ``IN`` list). The candidate
#: prefilter is already "titles containing every query word", so the ids that reach
#: the exact comparison are few; the batching exists only as a defensive bound.
_TITLE_ID_BATCH = 500


@dataclass(frozen=True)
class MatchInput:
    """Everything a source record knows that could identify a paper."""

    identifiers: Mapping[str, str] = dataclass_field(default_factory=dict)
    sha256: str | None = None
    title: str | None = None
    authors: Sequence[str] = ()
    year: int | str | None = None
    filename: str | None = None

    def normalized_identifiers(self) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for scheme, value in self.identifiers.items():
            candidate = ids.normalize_identifier(scheme, value)
            if candidate:
                normalized[scheme] = candidate
        return normalized

    def first_author(self) -> str | None:
        for name in self.authors:
            if str(name or "").strip():
                return str(name).strip()
        return None


@dataclass
class MatchResult:
    """Outcome of one matching attempt."""

    status: str
    method: str | None = None
    confidence: float | None = None
    paper: Paper | None = None
    candidates: list[dict[str, Any]] = dataclass_field(default_factory=list)
    reason: str = ""

    @property
    def paper_id(self) -> str | None:
        return self.paper.id if self.paper is not None else None

    @property
    def matched(self) -> bool:
        return self.status == STATUS_MATCHED and self.paper is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "match_status": self.status,
            "match_method": self.method,
            "match_confidence": self.confidence,
            "candidates": self.candidates,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class IdentifierHit:
    """One incoming identifier that already belongs to a live paper."""

    paper: Paper
    row: PaperIdentifier
    method: str
    value: str
    source_type: str | None

    @property
    def structured(self) -> bool:
        return metadata_merge.is_structured(self.source_type)


def _year(value: int | str | None) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def _first_author_of(paper: Paper) -> str | None:
    names = paper_service.paper_author_names(paper)
    return names[0] if names else None


def find_identifier_hits(
    session: Session, normalized: Mapping[str, str]
) -> list[IdentifierHit]:
    """Every live paper hit by an incoming paper-level identifier."""
    hits: list[IdentifierHit] = []
    for scheme, method in _MATCH_SCHEMES:
        value = normalized.get(scheme)
        if not value:
            continue
        row = ids.find_identifier(session, scheme, value)
        if row is None:
            continue
        paper = paper_service.get_paper(session, row.paper_id)
        if paper is None:
            # The identifier points at a soft-deleted paper: treat it as a miss
            # so the record can attach to a live one instead of a tombstone.
            continue
        source = (
            session.get(PaperSource, row.first_source_id)
            if row.first_source_id
            else None
        )
        hits.append(
            IdentifierHit(
                paper=paper,
                row=row,
                method=method,
                value=value,
                source_type=source.source_type if source is not None else None,
            )
        )
    return hits


def find_by_identifier(
    session: Session, normalized: Mapping[str, str]
) -> tuple[Paper | None, str | None, str | None]:
    """Backward-compatible first identifier hit in priority order."""
    hits = find_identifier_hits(session, normalized)
    if not hits:
        return None, None, None
    hit = hits[0]
    return hit.paper, hit.method, hit.value


def find_by_sha256(session: Session, sha256: str | None) -> Paper | None:
    """Step 2: the exact same PDF bytes are already stored."""
    digest = (sha256 or "").strip().casefold()
    if len(digest) != 64:
        return None
    return paper_service.find_by_sha256(session, digest)


def _papers_by_ids(session: Session, paper_ids: Sequence[str]) -> list[Paper]:
    """Load live papers in ``paper_ids`` order, de-duplicated."""
    ordered_ids = list(dict.fromkeys(str(item) for item in paper_ids if item))
    if not ordered_ids:
        return []
    rows = session.execute(
        select(Paper).where(Paper.id.in_(ordered_ids), Paper.deleted_at.is_(None))
    ).scalars()
    by_id = {paper.id: paper for paper in rows}
    return [by_id[paper_id] for paper_id in ordered_ids if paper_id in by_id]


def find_by_filename_identifier(
    session: Session, *, scheme: str, value: str | None
) -> list[Paper]:
    """Papers whose live PDF filename carries ``scheme:value``.

    DOI filenames use the strict complete-name convention parsed by
    :func:`metadata_service.doi_from_filename`. IEEE article numbers are only
    accepted as a standalone digit token inside such a DOI filename; the number
    comes from the external record and is never inferred as metadata from a DOI.
    """
    normalized = ids.normalize_identifier(scheme, value)
    if not normalized:
        return []

    statement = (
        select(Paper.id, PaperFile.filename)
        .join(PaperFile, PaperFile.paper_id == Paper.id)
        .where(Paper.deleted_at.is_(None), PaperFile.deleted_at.is_(None))
    )
    if scheme == ids.SCHEME_DOI:
        expected = f"{normalized.replace('/', '_', 1)}.pdf".casefold()
        statement = statement.where(func.lower(PaperFile.filename) == expected)
        rows = session.execute(statement).all()
        matched_ids = [
            str(paper_id)
            for paper_id, filename in rows
            if metadata_service.doi_from_filename(filename) == normalized
        ]
        return _papers_by_ids(session, matched_ids)

    if scheme == ids.SCHEME_IEEE_ARTICLE_NUMBER:
        statement = statement.where(PaperFile.filename.contains(normalized))
        rows = session.execute(statement).all()
        token = re.compile(rf"(?<!\d){re.escape(normalized)}(?!\d)")
        matched_ids = [
            str(paper_id)
            for paper_id, filename in rows
            if metadata_service.doi_from_filename(filename)
            and token.search(PurePath(filename).stem)
        ]
        return _papers_by_ids(session, matched_ids)
    return []


def find_by_filename_identity_and_title(
    session: Session, match_input: MatchInput
) -> tuple[list[Paper], str | None, float | None, list[dict[str, Any]]]:
    """Strongest filename identity supported by an exact normalized title."""
    wanted_title = paper_service.normalize_text(match_input.title)
    if not wanted_title:
        return [], None, None, []
    normalized = match_input.normalized_identifiers()
    probes = (
        (
            ids.SCHEME_DOI,
            METHOD_FILENAME_DOI_TITLE,
            CONFIDENCE_FILENAME_DOI_TITLE,
        ),
        (
            ids.SCHEME_IEEE_ARTICLE_NUMBER,
            METHOD_FILENAME_IEEE_ARTICLE_NUMBER_TITLE,
            CONFIDENCE_FILENAME_IEEE_ARTICLE_NUMBER_TITLE,
        ),
    )
    for scheme, method, confidence in probes:
        value = normalized.get(scheme)
        if not value:
            continue
        papers = find_by_filename_identifier(session, scheme=scheme, value=value)
        candidates = [
            paper
            for paper in papers
            if paper_service.normalize_text(paper.title) == wanted_title
        ]
        if not candidates:
            continue
        details = [
            {
                "paper_id": paper.id,
                "title": paper.title,
                "confidence": confidence,
                "title_match": True,
                "filename_identifier_matches": [scheme],
            }
            for paper in candidates
        ]
        return candidates, method, confidence, details
    return [], None, None, []


def title_candidates(session: Session, title: str | None) -> list[Paper]:
    """Live papers whose normalized title equals the incoming one.

    The SQL prefilter requires **every** significant word of the normalized title
    to appear in the stored title (AND of ``LIKE``s), then Python confirms exact
    normalized equality. A single "longest word" prefilter was wrong twice over:
    the longest word is usually a generic one (``microscaling``) that pulls in many
    candidates, and if it exceeded 40 characters the old ``[:40]`` truncation made
    the prefilter miss the true match entirely (a false negative that Python could
    then never rescue). Requiring all words is both more selective (fewer
    candidates survive) and safe (a true match contains every word).

    ``normalize_text`` keeps only ``[0-9a-z\\u4e00-\\u9fff]``, so a word never
    contains ``%``/``_`` -- no LIKE-escaping is needed. Only ``(id, title)`` is
    selected (the exact comparison needs nothing else), and there is no ``LIMIT``
    here: the row set is already "titles that contain every query word", and the
    decisive comparison below (normalized equality) is what actually narrows it --
    a pre-comparison LIMIT could drop the true match once the corpus grew.
    """
    normalized = paper_service.normalize_text(title)
    if not normalized:
        return []
    words = [word for word in normalized.split() if len(word) >= 3] or normalized.split()
    statement = select(Paper.id, Paper.title).where(Paper.deleted_at.is_(None))
    for word in words:
        statement = statement.where(func.lower(Paper.title).like(f"%{word}%"))
    # Deterministic order for stable tests; the exact filter below decides the set.
    statement = statement.order_by(Paper.created_at.desc())
    rows = session.execute(statement).all()
    matching_ids = [
        str(row[0])
        for row in rows
        if paper_service.normalize_text(row[1]) == normalized
    ]
    if not matching_ids:
        return []
    by_id: dict[str, Paper] = {}
    for start in range(0, len(matching_ids), _TITLE_ID_BATCH):
        batch = matching_ids[start : start + _TITLE_ID_BATCH]
        for paper in session.execute(
            select(Paper).where(Paper.id.in_(batch))
        ).scalars():
            by_id[paper.id] = paper
    return [by_id[paper_id] for paper_id in matching_ids if paper_id in by_id]


def find_by_title(
    session: Session, *, title: str | None, authors: Sequence[str], year: int | str | None
) -> tuple[Paper | None, float | None, str, list[dict[str, Any]]]:
    """Step 3/4: title first, then the author and year signals.

    Returns ``(paper, confidence, reason, candidates)``. ``paper`` is only set for
    the 0.8 case (all three signals agree); anything weaker is reported through
    ``candidates`` so the caller can queue it for review.
    """
    candidates = title_candidates(session, title)
    if not candidates:
        return None, None, "no title match", []

    wanted_author = paper_service.normalize_text(
        authors[0] if authors else None
    )
    wanted_year = _year(year)
    scored: list[dict[str, Any]] = []
    best: list[Paper] = []
    for paper in candidates:
        author = paper_service.normalize_text(_first_author_of(paper))
        author_hit = bool(wanted_author) and author == wanted_author
        year_hit = wanted_year is not None and paper.year == wanted_year
        score = 1.0 if (author_hit and year_hit) else 0.5
        scored.append(
            {
                "paper_id": paper.id,
                "title": paper.title,
                "confidence": score,
                "author_match": author_hit,
                "year_match": year_hit,
            }
        )
        if author_hit and year_hit:
            best.append(paper)

    scored.sort(key=lambda item: item["confidence"], reverse=True)
    if len(best) == 1:
        return best[0], CONFIDENCE_TITLE_YEAR_AUTHOR, "title + first author + year", scored
    if best:
        return (
            None,
            CONFIDENCE_TITLE_YEAR_AUTHOR,
            "multiple papers share title + first author + year",
            scored,
        )
    return None, CONFIDENCE_TITLE_ONLY, "title only", scored


def find_by_filename(session: Session, filename: str | None) -> list[Paper]:
    """Step 5: a stored file with the same normalized name (weak signal).

    The comparison itself is Python-side (``normalize_text`` is not expressible
    in portable SQL), but the fetch is a narrow ``(paper_id, filename)`` tuple
    query instead of full ``Paper`` entities with lazy per-paper file loads --
    the old shape was one entity scan plus N lazy queries per miss (review
    2026-10-05, P2-15).
    """
    normalized = paper_service.normalize_text(filename)
    if not normalized:
        return []
    rows = session.execute(
        select(Paper.id, PaperFile.filename)
        .join(PaperFile, PaperFile.paper_id == Paper.id)
        .where(Paper.deleted_at.is_(None), PaperFile.deleted_at.is_(None))
    ).all()
    matched_ids: list[str] = []
    seen: set[str] = set()
    for paper_id, record_filename in rows:
        if paper_id in seen:
            continue
        candidate = paper_service.normalize_text(record_filename)
        if candidate and candidate == normalized:
            seen.add(paper_id)
            matched_ids.append(paper_id)
    if not matched_ids:
        return []
    papers = (
        session.execute(
            select(Paper).where(Paper.id.in_(matched_ids), Paper.deleted_at.is_(None))
        )
        .scalars()
        .all()
    )
    by_id = {paper.id: paper for paper in papers}
    return [by_id[paper_id] for paper_id in matched_ids if paper_id in by_id]


def _title_matches(paper: Paper, incoming: str | None) -> bool:
    wanted = paper_service.normalize_text(incoming)
    return bool(wanted) and paper_service.normalize_text(paper.title) == wanted


def _titles_compatible(stored: str | None, incoming: str | None) -> bool:
    """Positive title support without treating minor subtitle drift as conflict."""
    left = paper_service.normalize_text(stored)
    right = paper_service.normalize_text(incoming)
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    left_words = {word for word in left.split() if len(word) >= 3}
    right_words = {word for word in right.split() if len(word) >= 3}
    if not left_words or not right_words:
        return False
    return len(left_words & right_words) / min(len(left_words), len(right_words)) >= 0.6


def _titles_clearly_conflict(stored: str | None, incoming: str | None) -> bool:
    """Conservative negative signal: both titles exist and share no real word."""
    left = paper_service.normalize_text(stored)
    right = paper_service.normalize_text(incoming)
    if not left or not right or left == right:
        return False
    left_words = {word for word in left.split() if len(word) >= 3}
    right_words = {word for word in right.split() if len(word) >= 3}
    return bool(left_words and right_words and left_words.isdisjoint(right_words))


def _primary_filename(paper: Paper) -> str | None:
    record = paper_service.original_file(paper)
    return record.filename if record is not None else None


def _filename_supports(
    paper: Paper, normalized: Mapping[str, str]
) -> list[str]:
    """Incoming identifiers explicitly carried by the candidate's main filename."""
    filename = _primary_filename(paper)
    if not filename:
        return []
    matches: list[str] = []
    doi = normalized.get(ids.SCHEME_DOI)
    if doi and metadata_service.doi_from_filename(filename) == doi:
        matches.append(ids.SCHEME_DOI)
    article_number = normalized.get(ids.SCHEME_IEEE_ARTICLE_NUMBER)
    if (
        article_number
        and metadata_service.doi_from_filename(filename)
        and re.search(rf"(?<!\d){re.escape(article_number)}(?!\d)", PurePath(filename).stem)
    ):
        matches.append(ids.SCHEME_IEEE_ARTICLE_NUMBER)
    return matches


def _strong_conflicts(
    session: Session,
    paper: Paper,
    match_input: MatchInput,
    normalized: Mapping[str, str],
) -> list[str]:
    """Evidence that must veto an otherwise positive automatic match."""
    conflicts: list[str] = []
    incoming_doi = normalized.get(ids.SCHEME_DOI)
    filename_doi = metadata_service.doi_from_filename(_primary_filename(paper))
    if incoming_doi and filename_doi and filename_doi != incoming_doi:
        conflicts.append(f"primary filename DOI is {filename_doi}")

    for row in ids.identifiers_for_paper(session, paper.id):
        incoming = normalized.get(row.scheme)
        if not incoming or row.normalized_value == incoming:
            continue
        source = (
            session.get(PaperSource, row.first_source_id)
            if row.first_source_id
            else None
        )
        if source is not None and metadata_merge.is_structured(source.source_type):
            conflicts.append(
                f"structured {row.scheme} is {row.normalized_value}"
            )

    filename_support = _filename_supports(paper, normalized)
    if (
        incoming_doi
        and not filename_support
        and not match_input.sha256
        and _titles_clearly_conflict(paper.title, match_input.title)
    ):
        conflicts.append("incoming title is unrelated to the candidate title")
    return list(dict.fromkeys(conflicts))


def _candidate_detail(
    paper: Paper,
    *,
    confidence: float,
    match_input: MatchInput,
    normalized: Mapping[str, str],
    identifier_matches: Sequence[str] = (),
    conflicts: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "paper_id": paper.id,
        "title": paper.title,
        "confidence": confidence,
        "title_match": _title_matches(paper, match_input.title),
        "identifier_matches": list(identifier_matches),
        "filename_identifier_matches": _filename_supports(paper, normalized),
        "identifier_conflicts": list(conflicts),
    }


def match_record(
    session: Session, match_input: MatchInput, *, exclude_paper_id: str | None = None
) -> MatchResult:
    """Run the five matching steps and report the first decisive outcome.

    ``exclude_paper_id`` skips one paper in every step: the ingest pipeline calls
    this with the paper row it just created, which must never match itself.
    """
    normalized = match_input.normalized_identifiers()

    identifier_hits = [
        hit
        for hit in find_identifier_hits(session, normalized)
        if hit.paper.id != exclude_paper_id
    ]
    hits_by_paper: dict[str, list[IdentifierHit]] = {}
    for hit in identifier_hits:
        hits_by_paper.setdefault(hit.paper.id, []).append(hit)
    if len(hits_by_paper) > 1:
        candidates = []
        for hits in hits_by_paper.values():
            paper = hits[0].paper
            conflicts = _strong_conflicts(session, paper, match_input, normalized)
            candidates.append(
                _candidate_detail(
                    paper,
                    confidence=CONFIDENCE_IDENTIFIER,
                    match_input=match_input,
                    normalized=normalized,
                    identifier_matches=[hit.row.scheme for hit in hits],
                    conflicts=conflicts,
                )
            )
        return MatchResult(
            status=STATUS_AMBIGUOUS,
            method=METHOD_IDENTIFIER_CONFLICT,
            confidence=CONFIDENCE_IDENTIFIER,
            candidates=candidates,
            reason="incoming identifiers point to different papers",
        )
    if hits_by_paper:
        hits = next(iter(hits_by_paper.values()))
        hit = hits[0]
        paper = hit.paper
        conflicts = _strong_conflicts(session, paper, match_input, normalized)
        structured = any(item.structured for item in hits)
        supported = _titles_compatible(paper.title, match_input.title) or bool(
            _filename_supports(paper, normalized)
        )
        confidence = (
            CONFIDENCE_IDENTIFIER if structured else CONFIDENCE_WEAK_IDENTIFIER
        )
        candidate = _candidate_detail(
            paper,
            confidence=confidence,
            match_input=match_input,
            normalized=normalized,
            identifier_matches=[item.row.scheme for item in hits],
            conflicts=conflicts,
        )
        if conflicts:
            return MatchResult(
                status=STATUS_AMBIGUOUS,
                method=hit.method,
                confidence=confidence,
                candidates=[candidate],
                reason="; ".join(conflicts),
            )
        if structured or supported:
            support = "structured source" if structured else "title or filename support"
            return MatchResult(
                status=STATUS_MATCHED,
                method=hit.method,
                confidence=confidence,
                paper=paper,
                candidates=[candidate],
                reason=f"{hit.method} identifier {hit.value} ({support})",
            )
        return MatchResult(
            status=STATUS_AMBIGUOUS,
            method=hit.method,
            confidence=confidence,
            candidates=[candidate],
            reason="weak or unknown-source identifier lacks title or filename support",
        )

    paper = find_by_sha256(session, match_input.sha256)
    if paper is not None and paper.id != exclude_paper_id:
        return MatchResult(
            status=STATUS_MATCHED,
            method=METHOD_SHA256,
            confidence=CONFIDENCE_SHA256,
            paper=paper,
            reason="identical file content",
        )

    filename_identity, method, confidence, candidates = find_by_filename_identity_and_title(
        session, match_input
    )
    filename_identity = [
        paper for paper in filename_identity if paper.id != exclude_paper_id
    ]
    candidates = [
        item for item in candidates if item.get("paper_id") != exclude_paper_id
    ]
    if len(filename_identity) == 1:
        paper = filename_identity[0]
        conflicts = _strong_conflicts(session, paper, match_input, normalized)
        if conflicts:
            return MatchResult(
                status=STATUS_AMBIGUOUS,
                method=method,
                confidence=confidence,
                candidates=[
                    _candidate_detail(
                        paper,
                        confidence=confidence or CONFIDENCE_TITLE_ONLY,
                        match_input=match_input,
                        normalized=normalized,
                        conflicts=conflicts,
                    )
                ],
                reason="; ".join(conflicts),
            )
        return MatchResult(
            status=STATUS_MATCHED,
            method=method,
            confidence=confidence,
            paper=paper,
            candidates=candidates,
            reason="filename identifier + normalized title",
        )
    if filename_identity:
        return MatchResult(
            status=STATUS_AMBIGUOUS,
            method=method,
            confidence=confidence,
            candidates=candidates,
            reason="multiple papers share filename identifier + normalized title",
        )

    paper, confidence, reason, candidates = find_by_title(
        session,
        title=match_input.title,
        authors=match_input.authors,
        year=match_input.year,
    )
    if paper is not None and paper.id != exclude_paper_id:
        conflicts = _strong_conflicts(session, paper, match_input, normalized)
        if conflicts:
            return MatchResult(
                status=STATUS_AMBIGUOUS,
                method=METHOD_TITLE_YEAR_AUTHOR,
                confidence=confidence,
                candidates=[
                    _candidate_detail(
                        paper,
                        confidence=confidence or CONFIDENCE_TITLE_ONLY,
                        match_input=match_input,
                        normalized=normalized,
                        conflicts=conflicts,
                    )
                ],
                reason="; ".join(conflicts),
            )
        return MatchResult(
            status=STATUS_MATCHED,
            method=METHOD_TITLE_YEAR_AUTHOR,
            confidence=confidence,
            paper=paper,
            reason=reason,
        )
    candidates = [
        item for item in candidates if item.get("paper_id") != exclude_paper_id
    ]
    if candidates:
        ambiguous_method = (
            METHOD_TITLE_YEAR_AUTHOR
            if confidence == CONFIDENCE_TITLE_YEAR_AUTHOR
            else METHOD_TITLE
        )
        return MatchResult(
            status=STATUS_AMBIGUOUS,
            method=ambiguous_method,
            confidence=confidence,
            candidates=candidates,
            reason=reason,
        )

    filename_matches = [
        item
        for item in find_by_filename(session, match_input.filename)
        if item.id != exclude_paper_id
    ]
    if filename_matches:
        return MatchResult(
            status=STATUS_AMBIGUOUS,
            method=METHOD_FILENAME,
            confidence=CONFIDENCE_FILENAME,
            candidates=[
                {"paper_id": item.id, "title": item.title, "confidence": CONFIDENCE_FILENAME}
                for item in filename_matches
            ],
            reason="same file name",
        )

    return MatchResult(status=STATUS_UNMATCHED, reason="no evidence matched")


def match_input_from_values(
    values: Mapping[str, Any],
    *,
    source_ref: str | None = None,
    filename: str | None = None,
) -> MatchInput:
    """Build a :class:`MatchInput` from a parsed record's claim values.

    ``values`` uses the same field names as the provenance ledger, so the importer
    does not have to keep two shapes in sync.
    """
    identifiers: dict[str, str] = {}
    for field, value in values.items():
        scheme = field.split(":", 1)[1] if field.startswith("identifier:") else None
        if scheme and value:
            identifiers[scheme] = str(value)
    authors = values.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    venue = values.get("venue")
    year = values.get("year")
    if year is None and isinstance(venue, Mapping):
        year = venue.get("year")
    return MatchInput(
        identifiers=identifiers,
        sha256=_sha256_from_ref(source_ref),
        title=values.get("title"),
        authors=list(authors),
        year=year,
        filename=filename,
    )


def _sha256_from_ref(source_ref: str | None) -> str | None:
    """``file:<path>:<sha256>`` source refs carry the digest (import reports)."""
    if not source_ref:
        return None
    parts = str(source_ref).split(":")
    if len(parts) < 3:
        return None
    digest = parts[-1].strip().casefold()
    return digest if len(digest) == 64 else None


def shell_paper_id() -> str:
    """A fresh paper id for a metadata-first shell (exported for the importer)."""
    return new_uuid()


__all__ = [
    "CONFIDENCE_FILENAME",
    "CONFIDENCE_FILENAME_DOI_TITLE",
    "CONFIDENCE_FILENAME_IEEE_ARTICLE_NUMBER_TITLE",
    "CONFIDENCE_IDENTIFIER",
    "CONFIDENCE_WEAK_IDENTIFIER",
    "CONFIDENCE_SHA256",
    "CONFIDENCE_TITLE_ONLY",
    "CONFIDENCE_TITLE_YEAR_AUTHOR",
    "MATCH_STATUSES",
    "METHOD_ARXIV",
    "METHOD_DOI",
    "METHOD_FILENAME",
    "METHOD_FILENAME_DOI_TITLE",
    "METHOD_FILENAME_IEEE_ARTICLE_NUMBER_TITLE",
    "METHOD_IEEE_ARTICLE_NUMBER",
    "METHOD_IDENTIFIER_CONFLICT",
    "METHOD_MANUAL",
    "METHOD_SHA256",
    "METHOD_TITLE",
    "METHOD_TITLE_YEAR_AUTHOR",
    "STATUS_AMBIGUOUS",
    "STATUS_MATCHED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "STATUS_UNMATCHED",
    "MatchInput",
    "MatchResult",
    "IdentifierHit",
    "find_by_filename",
    "find_by_filename_identifier",
    "find_by_filename_identity_and_title",
    "find_by_identifier",
    "find_identifier_hits",
    "find_by_sha256",
    "find_by_title",
    "match_input_from_values",
    "match_record",
    "shell_paper_id",
    "title_candidates",
]
