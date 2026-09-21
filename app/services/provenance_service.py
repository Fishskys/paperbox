"""Field-level provenance: who said what, when, and which value currently wins.

Every value that reaches ``papers`` is supposed to have a claim here. The table is
append-only: a new claim demotes the previous current row instead of deleting it,
which is what makes both the "who overwrote me" view (``GET
/api/papers/{id}/metadata``) and rollback possible.

Three kinds of decision are recorded in ``decided_by``:

* ``initial`` -- a source simply stated a value;
* ``structured_override`` -- a structured source corrected a ``pdf_heuristic``
  value (the single exception to rule R2);
* ``manual`` -- a human overrode everything (decision 12), never subject to R2.

``write_field`` is the only place that knows how a claim maps onto a ``papers``
column; keeping it in one registry is what stops the merge engine, the importer
and the manual-update endpoint from drifting apart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper, PaperFieldProvenance, new_uuid
from app.services import metadata_identifiers as identifiers
from app.services import venue_service as venues

logger = get_logger(__name__)

DECIDED_INITIAL = "initial"
DECIDED_STRUCTURED_OVERRIDE = "structured_override"
DECIDED_MANUAL = "manual"

DECIDED_BY_VALUES: tuple[str, ...] = (
    DECIDED_INITIAL,
    DECIDED_STRUCTURED_OVERRIDE,
    DECIDED_MANUAL,
)

FIELD_TITLE = "title"
FIELD_ABSTRACT = "abstract"
FIELD_LANGUAGE = "language"
FIELD_YEAR = "year"
FIELD_VENUE = "venue"
FIELD_VOLUME = "volume"
FIELD_ISSUE = "issue"
FIELD_PAGES = "pages"
FIELD_AUTHORS = "authors"
FIELD_PUBLICATION_DATE = "publication_date"
FIELD_PAPER_TYPE = "paper_type"
FIELD_URL = "url"

#: Every scalar field whose value lives in exactly one ``papers`` column.
SIMPLE_FIELDS: dict[str, str] = {
    FIELD_TITLE: "title",
    FIELD_ABSTRACT: "abstract",
    FIELD_LANGUAGE: "language",
    FIELD_YEAR: "year",
    FIELD_VOLUME: "volume",
    FIELD_ISSUE: "issue",
    FIELD_PAGES: "pages",
    FIELD_PUBLICATION_DATE: "publication_date",
    FIELD_PAPER_TYPE: "paper_type",
    FIELD_URL: "url",
}

#: Fields whose claim value is structured rather than a scalar.
STRUCTURED_FIELDS: tuple[str, ...] = (FIELD_VENUE, FIELD_AUTHORS, FIELD_YEAR)


def identifier_field(scheme: str) -> str:
    """``doi`` -> ``identifier:doi`` (the claim value is the identifier value)."""
    return f"identifier:{(scheme or '').strip().lower()}"


def tag_field(kind: str) -> str:
    """``ieee_terms`` -> ``tag:ieee_terms``."""
    return f"tag:{(kind or '').strip().lower()}"


def scheme_of_identifier_field(field: str) -> str | None:
    """Inverse of :func:`identifier_field` (``None`` for other fields)."""
    if field.startswith("identifier:"):
        return field.split(":", 1)[1] or None
    return None


def kind_of_tag_field(field: str) -> str | None:
    """Inverse of :func:`tag_field` (``None`` for other fields)."""
    if field.startswith("tag:"):
        return field.split(":", 1)[1] or None
    return None


def known_fields() -> tuple[str, ...]:
    """The fields a claim may be recorded for (open set, for validation only)."""
    extra = [identifier_field(scheme) for scheme in identifiers.SCHEMES]
    tags = [tag_field(kind) for kind in ("ieee_terms", "author_terms", "dynamic_index_terms", "source_tag")]
    return tuple(SIMPLE_FIELDS) + (FIELD_VENUE, FIELD_AUTHORS) + tuple(extra) + tuple(tags)


def is_known_field(field: str) -> bool:
    if field in SIMPLE_FIELDS or field in (FIELD_VENUE, FIELD_AUTHORS):
        return True
    return scheme_of_identifier_field(field) is not None or kind_of_tag_field(field) is not None


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def current_claim(
    session: Session, paper_id: str, field: str
) -> PaperFieldProvenance | None:
    """The claim that currently holds ``field`` (``None`` when there is none)."""
    statement = select(PaperFieldProvenance).where(
        PaperFieldProvenance.paper_id == paper_id,
        PaperFieldProvenance.field == field,
        PaperFieldProvenance.is_current.is_(True),
    )
    return session.execute(statement).scalars().first()


def field_history(
    session: Session, paper_id: str, field: str | None = None
) -> list[PaperFieldProvenance]:
    """Every claim of one field (or of the whole paper), newest first."""
    statement = select(PaperFieldProvenance).where(
        PaperFieldProvenance.paper_id == paper_id
    )
    if field is not None:
        statement = statement.where(PaperFieldProvenance.field == field)
    statement = statement.order_by(PaperFieldProvenance.decided_at.desc())
    return list(session.execute(statement).scalars().all())


def provenance_for_paper(session: Session, paper_id: str) -> dict[str, list[PaperFieldProvenance]]:
    """Claims of a paper grouped by field (current first, then newest history)."""
    grouped: dict[str, list[PaperFieldProvenance]] = {}
    for row in field_history(session, paper_id):
        grouped.setdefault(row.field, []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda item: (not item.is_current, item.decided_at is None))
    return grouped


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #
def _same_value(left: Any, right: Any) -> bool:
    """Loose equality that survives JSON round-trips (dates, numbers, lists)."""
    if left == right:
        return True
    if isinstance(left, (datetime, date)) or isinstance(right, (datetime, date)):
        return str(left)[:10] == str(right)[:10]
    return False


def record_claim(
    session: Session,
    *,
    paper_id: str,
    field: str,
    value: Any,
    source_id: str | None = None,
    confidence: float | None = None,
    decided_by: str = DECIDED_INITIAL,
    make_current: bool = False,
    identifier_id: str | None = None,
) -> PaperFieldProvenance | None:
    """Append a claim; optionally make it the current one.

    Restating the currently-holding value is a no-op (it returns the existing
    row), which is what keeps a re-import from filling the ledger with identical
    rows.
    """
    if field is None or not str(field).strip():
        return None
    existing = current_claim(session, paper_id, field)
    if existing is not None and _same_value(existing.value, value):
        if make_current and not existing.is_current:  # pragma: no cover - defensive
            existing.is_current = True
        return existing

    row = PaperFieldProvenance(
        id=new_uuid(),
        paper_id=paper_id,
        source_id=source_id,
        field=field,
        value=_jsonable(value),
        confidence=confidence,
        is_current=False,
        decided_by=decided_by,
        identifier_id=identifier_id,
    )
    session.add(row)
    session.flush()
    if make_current:
        promote(session, row)
    return row


def promote(session: Session, claim: PaperFieldProvenance) -> PaperFieldProvenance:
    """Make ``claim`` the current value of its field, demoting the old one."""
    current = current_claim(session, claim.paper_id, claim.field)
    if current is not None and current.id != claim.id:
        current.is_current = False
        session.flush()
    claim.is_current = True
    session.flush()
    return claim


def set_field(
    session: Session,
    paper: Paper,
    field: str,
    value: Any,
    *,
    source_id: str | None = None,
    confidence: float | None = None,
    decided_by: str = DECIDED_INITIAL,
    override: bool = False,
) -> PaperFieldProvenance | None:
    """Record a claim *and* write it onto the paper (the usual entry point).

    Use :func:`record_claim` alone when the value must not become the current one
    (that is what the merge engine does with the loser of a conflict).
    ``override=True`` publishes the value even where the merge rules would only
    fill a blank -- the manual-update and rollback paths.
    """
    claim = record_claim(
        session,
        paper_id=paper.id,
        field=field,
        value=value,
        source_id=source_id,
        confidence=confidence,
        decided_by=decided_by,
        make_current=True,
    )
    if claim is not None:
        write_field(session, paper, field, value, override=override)
    return claim


def rollback_field(
    session: Session, paper: Paper, field: str, provenance_id: str
) -> PaperFieldProvenance:
    """Restore a historical claim as the current one and re-write the column.

    Raises ``LookupError`` when the claim does not exist or belongs to another
    paper/field -- rolling back "some other paper's title" must not be possible.
    """
    row = session.get(PaperFieldProvenance, provenance_id)
    if row is None or row.paper_id != paper.id or row.field != field:
        raise LookupError(
            f"provenance {provenance_id} does not belong to paper {paper.id} field {field}"
        )
    promote(session, row)
    if scheme := scheme_of_identifier_field(field):
        _restore_identifier(session, paper, scheme, row.value)
    else:
        write_field(session, paper, field, row.value, override=True)
    session.flush()
    logger.info(
        "metadata rolled back",
        extra={
            "extra_fields": {
                "paper_id": paper.id,
                "field": field,
                "provenance_id": provenance_id,
                "value": row.value,
            }
        },
    )
    return row


# --------------------------------------------------------------------------- #
# claim value -> papers column
# --------------------------------------------------------------------------- #
def _jsonable(value: Any) -> Any:
    """Convert a value into something PostgreSQL JSONB accepts verbatim."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def write_field(
    session: Session, paper: Paper, field: str, value: Any, *, override: bool = False
) -> bool:
    """Apply a claim value to the paper row; returns whether anything changed.

    ``override=True`` is for the two paths that are allowed to *replace* a value
    rather than fill a hole: a rollback (the caller picked the value explicitly)
    and a manual edit. Everything else merges by filling blanks.
    """
    if value is None:
        return False

    attribute = SIMPLE_FIELDS.get(field)
    if attribute is not None:
        return _write_simple(paper, attribute, value)

    if field == FIELD_VENUE:
        return _write_venue(session, paper, value, override=override)
    if field == FIELD_AUTHORS:
        return _write_authors(session, paper, value, override=override)

    scheme = scheme_of_identifier_field(field)
    if scheme is not None:
        return _write_identifier(session, paper, scheme, value)

    if kind_of_tag_field(field) is not None:
        return _write_tags(session, paper, field, value)

    logger.debug("no writer for provenance field %s", field)
    return False


def _write_simple(paper: Paper, attribute: str, value: Any) -> bool:
    current = getattr(paper, attribute, None)
    if attribute == "publication_date" and isinstance(value, str):
        value = _parse_date(value)
        if value is None:
            return False
    if attribute == "year" and isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if current == value:
        return False
    if attribute == "title" and not str(value).strip():
        return False
    setattr(paper, attribute, value)
    return True


def _parse_date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        for fmt in ("%B %Y", "%b %Y", "%Y-%m", "%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed.date().replace(day=1) if fmt in ("%B %Y", "%b %Y", "%Y-%m") else date(parsed.year, 1, 1)
            except ValueError:
                continue
    return None


def _write_venue(
    session: Session, paper: Paper, value: Any, *, override: bool = False
) -> bool:
    """``{"name": ..., "year": ..., "location": ...}`` -> venue + edition ids."""
    if isinstance(value, str):
        claim: Mapping[str, Any] = {"name": value}
    elif isinstance(value, Mapping):
        claim = value
    else:
        return False
    name = claim.get("name")
    if not name:
        return False
    venue, edition = venues.resolve_venue(
        session,
        name=str(name),
        year=claim.get("year"),
        content_type=claim.get("content_type"),
        issn=claim.get("issn"),
        publisher=claim.get("publisher"),
        location=claim.get("location"),
        dates=claim.get("dates"),
        publication_number=claim.get("publication_number"),
        is_number=claim.get("is_number"),
    )
    if venue is None:
        return False
    if override:
        paper.venue_id = venue.id
        paper.venue_edition_id = edition.id if edition is not None else None
        parsed = venues._parse_year(claim.get("year"))
        paper.venue_year = parsed if parsed is not None else paper.year
        session.flush()
        return True
    changed = venues.attach_venue(session, paper, venue, edition)
    if paper.venue_year is None and claim.get("year") is not None:
        paper.venue_year = venues._parse_year(claim.get("year"))
        changed = True
    return changed


def _write_authors(
    session: Session, paper: Paper, value: Any, *, override: bool = False
) -> bool:
    if isinstance(value, str):
        names = [value]
    elif isinstance(value, Sequence):
        names = [str(item) for item in value if str(item).strip()]
    else:
        return False
    if not names:
        return False
    if not override and paper.paper_authors and _names_equal(paper, names):
        return False
    from app.services import paper_service

    paper_service.set_paper_authors(session, paper, names)
    return True


def _names_equal(paper: Paper, names: Sequence[str]) -> bool:
    from app.services import paper_service

    return paper_service.paper_author_names(paper) == list(names)


def _write_identifier(session: Session, paper: Paper, scheme: str, value: Any) -> bool:
    """Identifier claims keep the table and the mirrored columns in step."""
    row = identifiers.upsert_identifier(
        session, paper_id=paper.id, scheme=scheme, value=str(value)
    )
    if row is None:
        return False
    identifiers.refresh_primary(session, paper.id)
    identifiers.mirror_legacy_columns(session, paper)
    return True


def _restore_identifier(session: Session, paper: Paper, scheme: str, value: Any) -> None:
    """Rollback path for ``identifier:<scheme>``: re-point the mirror column."""
    attribute = "doi" if scheme == identifiers.SCHEME_DOI else "arxiv_id" if scheme == identifiers.SCHEME_ARXIV else None
    if attribute is not None and value:
        setattr(paper, attribute, identifiers.normalize_identifier(scheme, str(value)))
    _write_identifier(session, paper, scheme, value)


def _write_tags(session: Session, paper: Paper, field: str, value: Any) -> bool:
    """``tag:<kind>`` claims link paper_tags rows with that kind."""
    kind = kind_of_tag_field(field)
    if kind is None or value is None:
        return False
    if isinstance(value, str):
        names = [value]
    elif isinstance(value, Sequence):
        names = [str(item) for item in value if str(item).strip()]
    else:
        return False
    if not names:
        return False
    from app.services import metadata_tags as tags

    return tags.link_tags(session, paper, names, kind=kind)


def read_field(paper: Paper, field: str) -> Any:
    """Current value of ``field`` as the claim value shape (for comparisons)."""
    attribute = SIMPLE_FIELDS.get(field)
    if attribute is not None:
        value = getattr(paper, attribute, None)
        return value.isoformat() if isinstance(value, (date, datetime)) else value
    if field == FIELD_VENUE:
        name = paper.venue.name if paper.venue is not None else None
        return {"name": name, "year": paper.venue_year} if name else None
    if field == FIELD_AUTHORS:
        from app.services import paper_service

        return paper_service.paper_author_names(paper)
    scheme = scheme_of_identifier_field(field)
    if scheme == identifiers.SCHEME_DOI:
        return paper.doi
    if scheme == identifiers.SCHEME_ARXIV:
        return paper.arxiv_id
    return None


def provenance_summary(
    session: Session, paper_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Readable history per field, for ``GET /api/papers/{id}/metadata``."""
    summary: dict[str, list[dict[str, Any]]] = {}
    for field, rows in provenance_for_paper(session, paper_id).items():
        summary[field] = [
            {
                "provenance_id": row.id,
                "value": row.value,
                "source_id": row.source_id,
                "confidence": row.confidence,
                "is_current": bool(row.is_current),
                "decided_by": row.decided_by,
                "decided_at": row.decided_at,
            }
            for row in rows
        ]
    return summary


def utcnow() -> datetime:  # pragma: no cover - trivial helper used by callers
    return datetime.now(timezone.utc)


#: Signature every field writer obeys (documentation/typing aid).
FieldWriter = Callable[[Session, Paper, Any], bool]

__all__ = [
    "DECIDED_BY_VALUES",
    "DECIDED_INITIAL",
    "DECIDED_MANUAL",
    "DECIDED_STRUCTURED_OVERRIDE",
    "FIELD_ABSTRACT",
    "FIELD_AUTHORS",
    "FIELD_ISSUE",
    "FIELD_LANGUAGE",
    "FIELD_PAGES",
    "FIELD_PAPER_TYPE",
    "FIELD_PUBLICATION_DATE",
    "FIELD_TITLE",
    "FIELD_URL",
    "FIELD_VENUE",
    "FIELD_VOLUME",
    "FIELD_YEAR",
    "SIMPLE_FIELDS",
    "current_claim",
    "field_history",
    "identifier_field",
    "is_known_field",
    "kind_of_tag_field",
    "known_fields",
    "promote",
    "provenance_for_paper",
    "provenance_summary",
    "read_field",
    "record_claim",
    "rollback_field",
    "scheme_of_identifier_field",
    "set_field",
    "tag_field",
    "write_field",
]