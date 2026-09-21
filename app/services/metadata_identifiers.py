"""Identifiers: normalization, primary derivation and the dedupe skeleton.

``paper_identifiers`` is what makes cross-source matching possible: every source
(IEEE JSON, arXiv API, an embedded PDF DOI, a human) states its identifiers, and
the table keeps one normalized row per ``(scheme, value)``. The row flagged
``is_primary`` is the one that feeds ``papers.fingerprint``, following the chain
of decision 2::

    DOI > arXiv > normalized title + first author + year > file sha256

Only ``doi`` and ``arxiv`` can be primary; everything else (IEEE article number,
ISSN, sha256...) is context that helps matching but never names the paper. A
paper with none of the two keeps whatever fingerprint the caller built, which is
exactly the pre-existing behaviour for PDF-only ingests.

Normalization reuses :mod:`app.services.paper_service` so the values stored here
are byte-identical to the ones the fingerprint has always been built from.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper, PaperIdentifier, new_uuid
from app.services.paper_service import (
    build_fingerprint,
    normalize_arxiv_id,
    normalize_doi,
    normalize_text,
)

logger = get_logger(__name__)

SCHEME_DOI = "doi"
SCHEME_ARXIV = "arxiv"
SCHEME_IEEE_ARTICLE_NUMBER = "ieee_article_number"
SCHEME_ISSN = "issn"
SCHEME_ISBN = "isbn"
SCHEME_PMID = "pmid"
SCHEME_OPENALEX = "openalex"
SCHEME_SEMANTIC_SCHOLAR = "semantic_scholar"
SCHEME_URL = "url"
SCHEME_SHA256 = "sha256"

#: Open enum: adding a platform means adding a value, never a column.
SCHEMES: tuple[str, ...] = (
    SCHEME_DOI,
    SCHEME_ARXIV,
    SCHEME_IEEE_ARTICLE_NUMBER,
    SCHEME_ISSN,
    SCHEME_ISBN,
    SCHEME_PMID,
    SCHEME_OPENALEX,
    SCHEME_SEMANTIC_SCHOLAR,
    SCHEME_URL,
    SCHEME_SHA256,
)

#: Only these two can hold ``is_primary`` (decision 2).
PRIMARY_SCHEMES: tuple[str, ...] = (SCHEME_DOI, SCHEME_ARXIV)

_ISSN_KEEP = re.compile(r"[^0-9xX]")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize_identifier(scheme: str, value: str | None) -> str | None:
    """Normalize ``value`` for ``scheme``, or ``None`` when it is unusable.

    The result is what goes into ``paper_identifiers.normalized_value`` and what
    matching compares against, so it must be stable: ``10.1109/JSSC.2020.1`` and
    ``https://doi.org/10.1109/jssc.2020.1`` have to collide.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    key = (scheme or "").strip().lower()

    if key == SCHEME_DOI:
        return normalize_doi(raw)
    if key == SCHEME_ARXIV:
        return normalize_arxiv_id(raw)
    if key == SCHEME_IEEE_ARTICLE_NUMBER:
        digits = re.sub(r"[^0-9]", "", raw)
        return digits or None
    if key == SCHEME_ISSN or key == SCHEME_ISBN:
        candidate = _ISSN_KEEP.sub("", raw).upper()
        return candidate or None
    if key == SCHEME_PMID:
        digits = re.sub(r"[^0-9]", "", raw)
        return digits or None
    if key == SCHEME_URL:
        return raw.rstrip("/").casefold()
    if key == SCHEME_SHA256:
        candidate = raw.strip().casefold()
        return candidate if _SHA256.match(candidate) else None
    if key in (SCHEME_OPENALEX, SCHEME_SEMANTIC_SCHOLAR):
        return raw.rstrip("/").rsplit("/", 1)[-1].casefold() or None
    # Unknown scheme: a conservative textual normalization still beats storing
    # raw noise (whitespace/case differences would create duplicate rows).
    return normalize_text(raw) or raw.casefold()


def _value_of(row: Any, attribute: str) -> Any:
    """Read ``attribute`` off an ORM row or a plain mapping."""
    if isinstance(row, Mapping):
        return row.get(attribute)
    return getattr(row, attribute, None)


def primary_identifier(rows: Iterable[Any]) -> Any | None:
    """The identifier that should carry ``is_primary`` (DOI first, then arXiv).

    Accepts ORM rows or mappings with ``scheme`` / ``normalized_value`` keys.
    """
    candidates: dict[str, Any] = {}
    for row in rows:
        scheme = (_value_of(row, "scheme") or "").strip().lower()
        if scheme in PRIMARY_SCHEMES and _value_of(row, "normalized_value"):
            candidates.setdefault(scheme, row)
    for scheme in PRIMARY_SCHEMES:
        if scheme in candidates:
            return candidates[scheme]
    return None


def identifier_fingerprint(rows: Iterable[Any]) -> str | None:
    """Fingerprint implied by the primary identifier, or ``None``."""
    row = primary_identifier(rows)
    if row is None:
        return None
    scheme = (_value_of(row, "scheme") or "").strip().lower()
    value = _value_of(row, "normalized_value")
    if scheme == SCHEME_DOI:
        return build_fingerprint(doi=value)
    if scheme == SCHEME_ARXIV:
        return build_fingerprint(arxiv_id=value)
    return None


def build_fingerprint_from_identifiers(
    rows: Iterable[Any],
    *,
    title: str | None = None,
    first_author: str | None = None,
    year: int | str | None = None,
    sha256: str | None = None,
) -> str:
    """Fingerprint of a paper described by identifier rows (decision 2 chain).

    Falls back to the pre-existing ``paper_service.build_fingerprint`` ladder when
    no DOI/arXiv identifier is present, so PDF-only ingests behave exactly as
    before this layer existed.
    """
    from_identifiers = identifier_fingerprint(rows)
    if from_identifiers:
        return from_identifiers
    return build_fingerprint(
        title=title, first_author=first_author, year=year, sha256=sha256
    )


# --------------------------------------------------------------------------- #
# database helpers
# --------------------------------------------------------------------------- #
def find_identifier(
    session: Session, scheme: str, normalized_value: str
) -> PaperIdentifier | None:
    """Look up an identifier across the library (the matching step 1 probe)."""
    if not scheme or not normalized_value:
        return None
    statement = select(PaperIdentifier).where(
        PaperIdentifier.scheme == (scheme or "").strip().lower(),
        PaperIdentifier.normalized_value == normalized_value,
    )
    return session.execute(statement).scalars().first()


def identifiers_for_paper(session: Session, paper_id: str) -> list[PaperIdentifier]:
    """Every identifier of one paper, DOI/arXiv/… order preserved by insertion."""
    statement = (
        select(PaperIdentifier)
        .where(PaperIdentifier.paper_id == paper_id)
        .order_by(PaperIdentifier.created_at.asc(), PaperIdentifier.scheme.asc())
    )
    return list(session.execute(statement).scalars().all())


def upsert_identifier(
    session: Session,
    *,
    paper_id: str,
    scheme: str,
    value: str,
    first_source_id: str | None = None,
) -> PaperIdentifier | None:
    """Add one identifier to a paper, idempotently.

    Re-stating an identifier the paper already has is a no-op (the row is
    returned untouched). A value the *same* paper stores differently only in
    spelling also collapses: the normalized form is the identity.
    """
    normalized = normalize_identifier(scheme, value)
    if not normalized:
        return None
    existing = find_identifier(session, scheme, normalized)
    if existing is not None:
        if existing.paper_id == paper_id:
            return existing
        # Owned by another paper: the partial unique index forbids re-pointing it
        # here, and silently stealing an identifier would merge two papers behind
        # the caller's back. The matcher handles this as a conflict instead.
        logger.warning(
            "identifier %s:%s already belongs to paper %s",
            scheme,
            normalized,
            existing.paper_id,
            extra={
                "extra_fields": {
                    "scheme": scheme,
                    "value": normalized,
                    "owner_paper_id": existing.paper_id,
                    "requested_paper_id": paper_id,
                }
            },
        )
        return existing
    row = PaperIdentifier(
        id=new_uuid(),
        paper_id=paper_id,
        scheme=(scheme or "").strip().lower(),
        value=str(value).strip(),
        normalized_value=normalized,
        first_source_id=first_source_id,
    )
    session.add(row)
    session.flush()
    return row


def replace_identifier(
    session: Session,
    *,
    paper_id: str,
    scheme: str,
    value: str,
    first_source_id: str | None = None,
) -> PaperIdentifier | None:
    """Set *the* identifier of a scheme for a paper, replacing an older value.

    A human correcting a DOI means the old one is wrong, not that the paper has
    two DOIs: leaving both rows behind would also leave ``is_primary`` pointing at
    whichever came first. Only this path deletes identifier rows (a source stating
    a second DOI is a conflict the merge engine reports instead).
    """
    normalized = normalize_identifier(scheme, value)
    if not normalized:
        return None
    for row in identifiers_for_paper(session, paper_id):
        if row.scheme == (scheme or "").strip().lower() and row.normalized_value != normalized:
            session.delete(row)
    session.flush()
    return upsert_identifier(
        session,
        paper_id=paper_id,
        scheme=scheme,
        value=value,
        first_source_id=first_source_id,
    )


def refresh_primary(session: Session, paper_id: str) -> PaperIdentifier | None:
    """Re-derive ``is_primary`` across a paper's identifiers; return the winner."""
    rows = identifiers_for_paper(session, paper_id)
    winner = primary_identifier(rows)
    changed = False
    for row in rows:
        wanted = winner is not None and row.id == winner.id
        if bool(row.is_primary) != wanted:
            row.is_primary = wanted
            changed = True
    if changed:
        session.flush()
    return winner


def primary_fingerprint(
    session: Session, paper: Paper, *, sha256: str | None = None
) -> str:
    """Fingerprint implied by a paper's identifiers (decision 2 ladder).

    Falls back to the legacy ladder (title + first author + year, then sha256) when
    no DOI/arXiv identifier is registered, so a paper that only has a PDF keeps the
    fingerprint it has always had.
    """
    from app.services import paper_service

    rows = identifiers_for_paper(session, paper.id)
    authors = paper_service.paper_author_names(paper)
    return build_fingerprint_from_identifiers(
        rows,
        title=paper.title,
        first_author=authors[0] if authors else None,
        year=paper.year,
        sha256=sha256 or file_sha256(paper),
    )


def file_sha256(paper: Paper) -> str | None:
    """Content hash of the paper's primary file (the ``sha256:`` fallback input)."""
    from app.services import paper_service

    record = paper_service.original_file(paper)
    digest = getattr(record, "sha256", None) if record is not None else None
    if isinstance(digest, str) and digest.strip():
        return digest.strip()
    for item in getattr(paper, "files", ()) or ():
        candidate = getattr(item, "sha256", None)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def upgrade_fingerprint(
    session: Session, paper: Paper, *, sha256: str | None = None
) -> tuple[str, str | None]:
    """Re-derive ``papers.fingerprint`` from the identifiers.

    Returns ``(fingerprint, conflicting_paper_id)``. The conflicting id is set when
    another live paper already holds the fingerprint the identifiers imply: the row
    keeps the fingerprint it has, and the caller reports the collision instead of
    merging two papers behind the user's back.
    """
    from sqlalchemy.exc import IntegrityError

    from app.db.models import Paper as PaperModel

    candidate = primary_fingerprint(session, paper, sha256=sha256)
    if candidate == paper.fingerprint:
        return candidate, None

    conflict = session.execute(
        select(PaperModel).where(
            PaperModel.fingerprint == candidate,
            PaperModel.deleted_at.is_(None),
            PaperModel.id != paper.id,
        )
    ).scalars().first()
    if conflict is not None:
        return paper.fingerprint, conflict.id

    previous = paper.fingerprint
    paper.fingerprint = candidate
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return previous, None
    logger.info(
        "fingerprint upgraded",
        extra={
            "extra_fields": {"paper_id": paper.id, "from": previous, "to": candidate}
        },
    )
    return candidate, None


def mirror_legacy_columns(session: Session, paper: Paper) -> None:
    """Keep ``papers.doi`` / ``papers.arxiv_id`` in step with the identifiers.
    Decision 13 keeps those two columns as convenient mirrors (they are indexed
    and every existing query/report uses them), so they must never drift from the
    identifier table. They are only ever filled, never blanked here: a paper whose
    DOI was removed by a human edit is handled by the manual-update path.
    """
    updated = False
    for scheme, attribute in ((SCHEME_DOI, "doi"), (SCHEME_ARXIV, "arxiv_id")):
        if getattr(paper, attribute):
            continue
        for row in identifiers_for_paper(session, paper.id):
            if row.scheme == scheme and row.normalized_value:
                setattr(paper, attribute, row.normalized_value)
                updated = True
                break
    if updated:
        session.flush()


def identifier_rows_for_scheme(
    rows: Sequence[PaperIdentifier], scheme: str
) -> list[PaperIdentifier]:
    """Filter identifier rows by scheme (convenience for reporting)."""
    return [row for row in rows if row.scheme == scheme]


__all__ = [
    "PRIMARY_SCHEMES",
    "SCHEMES",
    "SCHEME_ARXIV",
    "SCHEME_DOI",
    "SCHEME_IEEE_ARTICLE_NUMBER",
    "SCHEME_ISBN",
    "SCHEME_ISSN",
    "SCHEME_OPENALEX",
    "SCHEME_PMID",
    "SCHEME_SEMANTIC_SCHOLAR",
    "SCHEME_SHA256",
    "SCHEME_URL",
    "build_fingerprint_from_identifiers",
    "file_sha256",
    "find_identifier",
    "identifier_fingerprint",
    "identifier_rows_for_scheme",
    "identifiers_for_paper",
    "mirror_legacy_columns",
    "normalize_identifier",
    "primary_fingerprint",
    "primary_identifier",
    "refresh_primary",
    "replace_identifier",
    "upgrade_fingerprint",
    "upsert_identifier",
]