"""``GET /api/papers`` metadata filters and the citation fields in ``PaperOut``.

The list endpoint reads PostgreSQL, so these filters are *not* the index-time
snapshot ``POST /api/search`` uses: a metadata change is visible immediately.
These tests run against in-memory SQLite (no live service is touched).
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.core.security import require_api_key
from app.db.models import Paper, PaperTag, PapersTag, Venue, new_uuid
from app.db.session import get_db
from app.main import app
from app.services import paper_service
from tests.test_job_progress import factory  # noqa: F401 - fixture


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def add_paper(
    session_factory,
    *,
    title: str = "A Paper",
    year: int | None = 2021,
    venue: str | None = None,
    paper_type: str | None = None,
    volume: str | None = None,
    issue: str | None = None,
    pages: str | None = None,
    publication_date: date | None = None,
    tags: tuple[tuple[str, str], ...] = (),
    status: str = "INDEXED",
) -> str:
    """Insert one paper, optionally with a venue and ``(tag, kind)`` links."""
    session = session_factory()
    try:
        venue_row = None
        if venue:
            key = paper_service.normalize_text(venue) or venue.casefold()
            venue_row = (
                session.query(Venue).filter(Venue.normalized_name == key).one_or_none()
            )
            if venue_row is None:
                venue_row = Venue(
                    id=new_uuid(), name=venue, normalized_name=key, kind="conference"
                )
                session.add(venue_row)
        paper = Paper(
            id=new_uuid(),
            title=title,
            fingerprint=f"sha256:{new_uuid()}",
            status=status,
            year=year,
            venue=venue_row,
            venue_year=year,
            paper_type=paper_type,
            volume=volume,
            issue=issue,
            pages=pages,
            publication_date=publication_date,
        )
        session.add(paper)
        session.flush()
        for name, kind in tags:
            key = paper_service.normalize_text(name) or name.casefold()
            tag = session.query(PaperTag).filter(PaperTag.normalized_name == key).one_or_none()
            if tag is None:
                tag = PaperTag(id=new_uuid(), name=name, normalized_name=key)
                session.add(tag)
                session.flush()
            session.add(
                PapersTag(id=new_uuid(), paper_id=paper.id, tag_id=tag.id, kind=kind)
            )
        session.commit()
        return paper.id
    finally:
        session.close()


def listed_ids(session_factory, **kwargs) -> list[str]:
    session = session_factory()
    try:
        rows, total = paper_service.list_papers(session, **kwargs)
        assert total == len(rows)
        return [row.id for row in rows]
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# the service filters
# --------------------------------------------------------------------------- #


def test_venue_filter_matches_the_normalized_name(factory) -> None:  # noqa: F811
    isscc = add_paper(factory, venue="ISSCC")
    add_paper(factory, venue="VLSI")
    assert listed_ids(factory, venue=["isscc"]) == [isscc]
    # Punctuation/case variants normalize to the same key.
    assert listed_ids(factory, venue=["ISSCC"]) == [isscc]


def test_year_bounds_are_inclusive(factory) -> None:  # noqa: F811
    old = add_paper(factory, year=2015)
    mid = add_paper(factory, year=2020)
    new = add_paper(factory, year=2025)
    assert sorted(listed_ids(factory, year_from=2015, year_to=2020)) == sorted([old, mid])
    assert sorted(listed_ids(factory, year_from=2016)) == sorted([mid, new])
    assert listed_ids(factory, year_to=2015) == [old]


def test_paper_type_filter(factory) -> None:  # noqa: F811
    conference = add_paper(factory, paper_type="conference")
    add_paper(factory, paper_type="journal")
    assert listed_ids(factory, paper_type=["conference"]) == [conference]
    # Values are matched case-insensitively.
    assert listed_ids(factory, paper_type=["CONFERENCE"]) == [conference]


def test_tag_filter_matches_any_kind_and_lists_a_paper_once(factory) -> None:  # noqa: F811
    both = add_paper(
        factory,
        tags=(("Low Power SRAM", "ieee_terms"), ("sram", "source_tag")),
    )
    add_paper(factory, tags=(("nlp", "source_tag"),))
    assert listed_ids(factory, tag=["Low Power SRAM"]) == [both]
    # Two matching tags on one paper must not duplicate the row.
    assert listed_ids(factory, tag=["Low Power SRAM", "sram"]) == [both]


def test_filters_combine(factory) -> None:  # noqa: F811
    match = add_paper(
        factory, venue="ISSCC", year=2021, paper_type="conference", tags=(("sram", "source_tag"),)
    )
    add_paper(factory, venue="ISSCC", year=2010, paper_type="conference")
    add_paper(factory, venue="VLSI", year=2021, paper_type="conference")
    assert listed_ids(
        factory, venue=["ISSCC"], year_from=2020, paper_type=["conference"], tag=["sram"]
    ) == [match]


def test_filters_are_ignored_when_blank(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, venue="ISSCC")
    assert listed_ids(factory, venue=["  "], paper_type=[], tag=[""]) == [paper_id]


# --------------------------------------------------------------------------- #
# the endpoint
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(factory):  # noqa: F811 - fixture comes from the import above
    """A TestClient wired to the in-memory database and a stub API key."""

    def _db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_api_key] = lambda: "test-key"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_the_list_endpoint_accepts_the_metadata_filters(client, factory) -> None:  # noqa: F811
    match = add_paper(
        factory, title="Matching", venue="ISSCC", year=2021, paper_type="conference"
    )
    add_paper(factory, title="Other", venue="VLSI", year=2021, paper_type="conference")

    response = client.get("/api/papers?venue=ISSCC&year_from=2020&paper_type=conference")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["paper_id"] for item in body["papers"]] == [match]


def test_the_list_endpoint_rejects_a_non_numeric_year(client) -> None:
    assert client.get("/api/papers?year_from=recent").status_code == 422


def test_paper_out_exposes_the_citation_fields(client, factory) -> None:  # noqa: F811
    paper_id = add_paper(
        factory,
        venue="ISSCC",
        year=2021,
        paper_type="conference",
        volume="64",
        issue="3",
        pages="412-419",
        publication_date=date(2021, 3, 1),
    )
    response = client.get(f"/api/papers/{paper_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["venue"] == "ISSCC"
    assert body["venue_year"] == 2021
    assert body["paper_type"] == "conference"
    assert body["volume"] == "64"
    assert body["issue"] == "3"
    assert body["pages"] == "412-419"
    assert body["publication_date"] == "2021-03-01"


def test_serialize_paper_carries_the_citation_fields(factory) -> None:  # noqa: F811
    paper_id = add_paper(
        factory,
        venue="ISSCC",
        year=2021,
        paper_type="conference",
        volume="64",
        pages="412-419",
    )
    session = factory()
    try:
        payload = paper_service.serialize_paper(session.get(Paper, paper_id))
    finally:
        session.close()
    assert payload["venue_year"] == 2021
    assert payload["paper_type"] == "conference"
    assert payload["volume"] == "64"
    assert payload["issue"] is None
    assert payload["pages"] == "412-419"
    assert payload["publication_date"] is None