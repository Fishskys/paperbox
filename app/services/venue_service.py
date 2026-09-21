"""Venues and their yearly editions (decision 7).

A venue is an *entity* ("IEEE International Solid-State Circuits Conference"),
an edition is one *year* of it (2015, with its location, dates and publication
number). They are stored apart on purpose: searching "the conference" must match
every year, searching "the conference + 2015" must match one, and a venue name is
never glued together with a year into a single string.

``papers.venue_year`` mirrors ``venue_editions.year`` so the second query needs no
join. Venue rows are matched by ``normalize_text``-level normalization only --
alias tables and disambiguation are explicitly out of scope for this iteration.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper, Venue, VenueEdition, new_uuid
from app.services.paper_service import normalize_text

logger = get_logger(__name__)

KIND_JOURNAL = "journal"
KIND_MAGAZINE = "magazine"
KIND_CONFERENCE = "conference"
KIND_WORKSHOP = "workshop"
KIND_STANDARD = "standard"
KIND_BOOK = "book"
KIND_PREPRINT = "preprint"
KIND_UNKNOWN = "unknown"

VENUE_KINDS: tuple[str, ...] = (
    KIND_JOURNAL,
    KIND_MAGAZINE,
    KIND_CONFERENCE,
    KIND_WORKSHOP,
    KIND_STANDARD,
    KIND_BOOK,
    KIND_PREPRINT,
    KIND_UNKNOWN,
)

PAPER_TYPE_JOURNAL = "journal"
PAPER_TYPE_CONFERENCE = "conference"
PAPER_TYPE_PREPRINT = "preprint"
PAPER_TYPE_EARLY_ACCESS = "early_access"
PAPER_TYPE_STANDARD = "standard"
PAPER_TYPE_UNKNOWN = "unknown"

PAPER_TYPES: tuple[str, ...] = (
    PAPER_TYPE_JOURNAL,
    PAPER_TYPE_CONFERENCE,
    PAPER_TYPE_PREPRINT,
    PAPER_TYPE_EARLY_ACCESS,
    PAPER_TYPE_STANDARD,
    PAPER_TYPE_UNKNOWN,
)

#: IEEE ``content_type`` strings (case/plural insensitive) -> venue kind.
_VENUE_KIND_BY_CONTENT_TYPE: dict[str, str] = {
    "journal": KIND_JOURNAL,
    "journals": KIND_JOURNAL,
    "magazine": KIND_MAGAZINE,
    "magazines": KIND_MAGAZINE,
    "conference": KIND_CONFERENCE,
    "conferences": KIND_CONFERENCE,
    "workshop": KIND_WORKSHOP,
    "workshops": KIND_WORKSHOP,
    "standard": KIND_STANDARD,
    "standards": KIND_STANDARD,
    "book": KIND_BOOK,
    "books": KIND_BOOK,
    "early access": KIND_UNKNOWN,
    "courses": KIND_UNKNOWN,
}

#: IEEE ``content_type`` -> ``papers.paper_type``.
_PAPER_TYPE_BY_CONTENT_TYPE: dict[str, str] = {
    "journal": PAPER_TYPE_JOURNAL,
    "journals": PAPER_TYPE_JOURNAL,
    "magazine": PAPER_TYPE_JOURNAL,
    "magazines": PAPER_TYPE_JOURNAL,
    "conference": PAPER_TYPE_CONFERENCE,
    "conferences": PAPER_TYPE_CONFERENCE,
    "workshop": PAPER_TYPE_CONFERENCE,
    "workshops": PAPER_TYPE_CONFERENCE,
    "standard": PAPER_TYPE_STANDARD,
    "standards": PAPER_TYPE_STANDARD,
    "early access": PAPER_TYPE_EARLY_ACCESS,
    "preprint": PAPER_TYPE_PREPRINT,
}


def _key(value: str | None) -> str:
    return (value or "").strip().casefold()


def venue_kind_for_content_type(content_type: str | None) -> str | None:
    """Map a source-reported ``content_type`` to a venue kind."""
    return _VENUE_KIND_BY_CONTENT_TYPE.get(_key(content_type))


def paper_type_for_content_type(content_type: str | None) -> str | None:
    """Map a source-reported ``content_type`` to ``papers.paper_type``."""
    return _PAPER_TYPE_BY_CONTENT_TYPE.get(_key(content_type))


def normalize_venue_name(name: str | None) -> str:
    """Stable key for a venue name (reuses ``paper_service.normalize_text``)."""
    if not name:
        return ""
    return normalize_text(name) or (name.strip().casefold())


def _parse_year(year: int | str | None) -> int | None:
    """Accept ``2015``, ``"2015"`` and ``"July 2015"`` style inputs."""
    if year is None or isinstance(year, bool):
        return None
    if isinstance(year, int):
        return year if 1000 <= year <= 2999 else None
    text = str(year).strip()
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        return value if 1000 <= value <= 2999 else None
    for token in text.replace("-", " ").split():
        if len(token) == 4 and token.isdigit():
            value = int(token)
            if 1000 <= value <= 2999:
                return value
    return None


def find_venue(session: Session, name: str | None) -> Venue | None:
    """Look up a venue by its normalized name."""
    key = normalize_venue_name(name)
    if not key:
        return None
    statement = select(Venue).where(Venue.normalized_name == key)
    return session.execute(statement).scalars().first()


def get_or_create_venue(
    session: Session,
    name: str,
    *,
    kind: str | None = None,
    issn: str | None = None,
    publisher: str | None = None,
) -> Venue | None:
    """Return the venue called ``name``, creating it when needed.

    An existing row is only ever *filled in* (missing kind/issn/publisher), never
    overwritten: the same venue reached from two platforms may report one field
    each, and neither report is more authoritative than the other (rule R2).
    """
    if not name or not str(name).strip():
        return None
    key = normalize_venue_name(name)
    venue = find_venue(session, name)
    if venue is None:
        venue = Venue(
            id=new_uuid(),
            name=str(name).strip(),
            normalized_name=key,
            kind=kind,
            publisher=publisher,
            issn=issn,
        )
        session.add(venue)
        session.flush()
        return venue

    changed = False
    if kind and not venue.kind:
        venue.kind = kind
        changed = True
    if issn and not venue.issn:
        venue.issn = issn
        changed = True
    if publisher and not venue.publisher:
        venue.publisher = publisher
        changed = True
    if changed:
        session.flush()
    return venue


def find_edition(session: Session, venue_id: str, year: int) -> VenueEdition | None:
    """The edition of ``venue_id`` published in ``year``."""
    if not venue_id or year is None:
        return None
    statement = select(VenueEdition).where(
        VenueEdition.venue_id == venue_id, VenueEdition.year == year
    )
    return session.execute(statement).scalars().first()


def get_or_create_edition(
    session: Session,
    venue: Venue,
    year: int | str | None,
    *,
    location: str | None = None,
    dates: str | None = None,
    publication_number: str | None = None,
    is_number: str | None = None,
) -> VenueEdition | None:
    """Return ``(venue, year)``, creating the edition when needed.

    Fields already present on the row win (fill-blanks only), so re-importing the
    same record from another platform cannot blank the location IEEE reported.
    """
    parsed = _parse_year(year)
    if venue is None or parsed is None:
        return None
    edition = find_edition(session, venue.id, parsed)
    if edition is None:
        edition = VenueEdition(
            id=new_uuid(),
            venue_id=venue.id,
            year=parsed,
            location=location,
            dates=dates,
            publication_number=publication_number,
            is_number=is_number,
        )
        session.add(edition)
        session.flush()
        return edition

    changed = False
    for attribute, value in (
        ("location", location),
        ("dates", dates),
        ("publication_number", publication_number),
        ("is_number", is_number),
    ):
        if value and not getattr(edition, attribute):
            setattr(edition, attribute, value)
            changed = True
    if changed:
        session.flush()
    return edition


def resolve_venue(
    session: Session,
    *,
    name: str | None,
    year: int | str | None = None,
    content_type: str | None = None,
    issn: str | None = None,
    publisher: str | None = None,
    location: str | None = None,
    dates: str | None = None,
    publication_number: str | None = None,
    is_number: str | None = None,
) -> tuple[Venue | None, VenueEdition | None]:
    """``(venue, edition)`` for one source record, creating what is missing."""
    venue = get_or_create_venue(
        session,
        name or "",
        kind=venue_kind_for_content_type(content_type),
        issn=issn,
        publisher=publisher,
    )
    edition = get_or_create_edition(
        session,
        venue,
        year,
        location=location,
        dates=dates,
        publication_number=publication_number,
        is_number=is_number,
    )
    return venue, edition


def attach_venue(
    session: Session, paper: Paper, venue: Venue | None, edition: VenueEdition | None
) -> bool:
    """Point a paper at a venue/edition; returns whether the paper changed.

    Only fills in: a paper that already names its venue is not re-pointed, which
    keeps the fill-blanks contract of the merge engine in one place.
    """
    changed = False
    if venue is not None and paper.venue_id is None:
        paper.venue_id = venue.id
        changed = True
    if edition is not None and paper.venue_edition_id is None:
        paper.venue_edition_id = edition.id
        paper.venue_year = edition.year
        changed = True
    elif edition is None and paper.venue_year is None and paper.year is not None:
        paper.venue_year = paper.year
        changed = True
    if changed:
        session.flush()
    return changed


def find_papers_by_venue(
    session: Session,
    *,
    name: str | None = None,
    year: int | str | None = None,
    limit: int = 50,
) -> list[Paper]:
    """Papers at a venue, optionally narrowed to one year.

    ``name`` alone matches every year of the venue (join on ``venues``); adding
    ``year`` narrows on the redundant ``papers.venue_year`` column, which is the
    "conference + 2015" query of decision 7.
    """
    statement = select(Paper).where(Paper.deleted_at.is_(None))
    if name:
        key = normalize_venue_name(name)
        statement = statement.join(Venue, Venue.id == Paper.venue_id).where(
            Venue.normalized_name == key
        )
    parsed = _parse_year(year)
    if parsed is not None:
        statement = statement.where(Paper.venue_year == parsed)
    statement = statement.order_by(Paper.created_at.desc()).limit(max(1, min(limit, 200)))
    return list(session.execute(statement).scalars().all())


def venue_editions_for_venue(session: Session, venue_id: str) -> list[VenueEdition]:
    """Every edition of a venue, oldest first."""
    statement = (
        select(VenueEdition)
        .where(VenueEdition.venue_id == venue_id)
        .order_by(VenueEdition.year.asc())
    )
    return list(session.execute(statement).scalars().all())


def count_venue_editions(session: Session, venue_id: str) -> int:
    """How many years of this venue are known (reporting helper)."""
    statement = select(func.count(VenueEdition.id)).where(
        VenueEdition.venue_id == venue_id
    )
    return int(session.execute(statement).scalar_one())


def venue_names_for_papers(papers: Sequence[Paper]) -> list[str | None]:
    """Venue names of a paper list (download/export helper)."""
    return [paper.venue.name if paper.venue is not None else None for paper in papers]


__all__ = [
    "KIND_BOOK",
    "KIND_CONFERENCE",
    "KIND_JOURNAL",
    "KIND_MAGAZINE",
    "KIND_PREPRINT",
    "KIND_STANDARD",
    "KIND_UNKNOWN",
    "KIND_WORKSHOP",
    "PAPER_TYPES",
    "PAPER_TYPE_CONFERENCE",
    "PAPER_TYPE_EARLY_ACCESS",
    "PAPER_TYPE_JOURNAL",
    "PAPER_TYPE_PREPRINT",
    "PAPER_TYPE_STANDARD",
    "PAPER_TYPE_UNKNOWN",
    "VENUE_KINDS",
    "attach_venue",
    "count_venue_editions",
    "find_edition",
    "find_papers_by_venue",
    "find_venue",
    "get_or_create_edition",
    "get_or_create_venue",
    "normalize_venue_name",
    "paper_type_for_content_type",
    "resolve_venue",
    "venue_editions_for_venue",
    "venue_kind_for_content_type",
    "venue_names_for_papers",
]