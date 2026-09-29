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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper, PaperFile, new_uuid
from app.services import metadata_identifiers as ids
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
METHOD_ISSN = "issn"
METHOD_SHA256 = "sha256"
METHOD_TITLE_YEAR_AUTHOR = "title_year_author"
METHOD_TITLE = "title"
METHOD_FILENAME = "filename"
METHOD_MANUAL = "manual"

CONFIDENCE_IDENTIFIER = 1.0
CONFIDENCE_SHA256 = 1.0
CONFIDENCE_TITLE_YEAR_AUTHOR = 0.8
CONFIDENCE_TITLE_ONLY = 0.5
CONFIDENCE_FILENAME = 0.5

#: Identifier schemes worth probing in step 1, in priority order.
_MATCH_SCHEMES: tuple[tuple[str, str], ...] = (
    (ids.SCHEME_DOI, METHOD_DOI),
    (ids.SCHEME_ARXIV, METHOD_ARXIV),
    (ids.SCHEME_IEEE_ARTICLE_NUMBER, METHOD_IEEE_ARTICLE_NUMBER),
    (ids.SCHEME_ISSN, METHOD_ISSN),
)

#: How many title candidates to pull before comparing normalized titles.
_TITLE_CANDIDATE_LIMIT = 200


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


def find_by_identifier(
    session: Session, normalized: Mapping[str, str]
) -> tuple[Paper | None, str | None, str | None]:
    """Step 1: any identifier hit, in DOI > arXiv > IEEE number > ISSN order."""
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
        return paper, method, value
    return None, None, None


def find_by_sha256(session: Session, sha256: str | None) -> Paper | None:
    """Step 2: the exact same PDF bytes are already stored."""
    digest = (sha256 or "").strip().casefold()
    if len(digest) != 64:
        return None
    return paper_service.find_by_sha256(session, digest)


def title_candidates(session: Session, title: str | None) -> list[Paper]:
    """Live papers whose normalized title equals the incoming one.

    The SQL prefilter is a single *word* of the normalized title (the longest one,
    hence the most selective): normalized titles have no punctuation, so a
    multi-word pattern would miss "Low-Power SRAM: Leakage Reduction". The
    decisive comparison is the normalized equality below, done in Python.
    """
    normalized = paper_service.normalize_text(title)
    if not normalized:
        return []
    words = [word for word in normalized.split() if len(word) >= 3] or normalized.split()
    fragment = max(words, key=len)[:40]
    statement = (
        select(Paper)
        .where(Paper.deleted_at.is_(None), func.lower(Paper.title).like(f"%{fragment}%"))
        .limit(_TITLE_CANDIDATE_LIMIT)
    )
    rows = session.execute(statement).scalars().all()
    return [paper for paper in rows if paper_service.normalize_text(paper.title) == normalized]


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
    best: Paper | None = None
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
        if author_hit and year_hit and best is None:
            best = paper

    scored.sort(key=lambda item: item["confidence"], reverse=True)
    if best is not None:
        return best, CONFIDENCE_TITLE_YEAR_AUTHOR, "title + first author + year", scored
    return None, CONFIDENCE_TITLE_ONLY, "title only", scored


def find_by_filename(session: Session, filename: str | None) -> list[Paper]:
    """Step 5: a stored file with the same normalized name (weak signal)."""
    normalized = paper_service.normalize_text(filename)
    if not normalized:
        return []
    statement = (
        select(Paper)
        .join(PaperFile, PaperFile.paper_id == Paper.id)
        .where(Paper.deleted_at.is_(None), PaperFile.deleted_at.is_(None))
    )
    papers: list[Paper] = []
    seen: set[str] = set()
    for paper in session.execute(statement).scalars().all():
        if paper.id in seen:
            continue
        for record in paper.files:
            if record.deleted_at is not None:
                continue
            candidate = paper_service.normalize_text(record.filename)
            if candidate and candidate == normalized:
                seen.add(paper.id)
                papers.append(paper)
                break
    return papers


def match_record(
    session: Session, match_input: MatchInput, *, exclude_paper_id: str | None = None
) -> MatchResult:
    """Run the five matching steps and report the first decisive outcome.

    ``exclude_paper_id`` skips one paper in every step: the ingest pipeline calls
    this with the paper row it just created, which must never match itself.
    """
    normalized = match_input.normalized_identifiers()

    paper, method, value = find_by_identifier(session, normalized)
    if paper is not None and paper.id != exclude_paper_id:
        return MatchResult(
            status=STATUS_MATCHED,
            method=method,
            confidence=CONFIDENCE_IDENTIFIER,
            paper=paper,
            reason=f"{method} identifier {value}",
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

    paper, confidence, reason, candidates = find_by_title(
        session,
        title=match_input.title,
        authors=match_input.authors,
        year=match_input.year,
    )
    if paper is not None and paper.id != exclude_paper_id:
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
        return MatchResult(
            status=STATUS_AMBIGUOUS,
            method=METHOD_TITLE,
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
    "CONFIDENCE_IDENTIFIER",
    "CONFIDENCE_SHA256",
    "CONFIDENCE_TITLE_ONLY",
    "CONFIDENCE_TITLE_YEAR_AUTHOR",
    "MATCH_STATUSES",
    "METHOD_ARXIV",
    "METHOD_DOI",
    "METHOD_FILENAME",
    "METHOD_IEEE_ARTICLE_NUMBER",
    "METHOD_ISSN",
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
    "find_by_filename",
    "find_by_identifier",
    "find_by_sha256",
    "find_by_title",
    "match_input_from_values",
    "match_record",
    "shell_paper_id",
    "title_candidates",
]