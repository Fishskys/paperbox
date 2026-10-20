"""Clearing one paper's metadata: claims/sources/identifiers/links/columns go,
the paper row and its content stay.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.security import require_write
from app.db.models import (
    Paper,
    PaperAuthor,
    PaperFieldProvenance,
    PaperIdentifier,
    PaperSource,
    PapersTag,
    Author,
    PaperTag,
)
from app.db.session import get_db
from app.main import app
from app.services import metadata_clear, metadata_sources as sources, paper_service
from app.services import provenance_service


def make_paper(session, **overrides) -> Paper:
    values = {
        "id": paper_service.new_uuid(),
        "title": "Original title",
        "fingerprint": f"sha256:{paper_service.new_uuid()}",
        "status": "INDEXED",
    }
    values.update(overrides)
    paper = Paper(**values)
    session.add(paper)
    session.flush()
    return paper


def seed_full_metadata(session, paper: Paper) -> None:
    """Give the paper a source, a claim, an identifier, an author and a tag."""
    source = sources.upsert_source(
        session,
        source_type=sources.SOURCE_TYPE_IEEE_API,
        source_ref=f"doi:10.1109/test.{paper.id[:8]}",
        raw={"title": "From IEEE"},
        paper_id=paper.id,
        match_status=sources.MATCH_STATUS_MATCHED,
    )
    provenance_service.set_field(
        session, paper, "title", "From IEEE", source_id=source.id, override=True
    )
    provenance_service.set_field(
        session, paper, "year", 2026, source_id=source.id, override=True
    )
    doi_value = f"10.1109/test.{paper.id[:8]}"
    session.add(
        PaperIdentifier(
            id=paper_service.new_uuid(),
            paper_id=paper.id,
            scheme="doi",
            value=doi_value,
            normalized_value=doi_value,
            first_source_id=source.id,
            is_primary=True,
        )
    )
    author = Author(
        id=paper_service.new_uuid(), name="Alice", normalized_name=f"alice {paper.id[:6]}"
    )
    session.add(author)
    session.flush()
    session.add(
        PaperAuthor(
            id=paper_service.new_uuid(),
            paper_id=paper.id,
            author_id=author.id,
            author_order=0,
        )
    )
    tag = PaperTag(
        id=paper_service.new_uuid(), name="sram", normalized_name=f"sram {paper.id[:6]}"
    )
    session.add(tag)
    session.flush()
    session.add(
        PapersTag(
            id=paper_service.new_uuid(),
            paper_id=paper.id,
            tag_id=tag.id,
            kind="source_tag",
        )
    )
    paper.doi = doi_value
    session.flush()


@pytest.fixture()
def client(session_factory):
    def _db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_write] = lambda: "test-key"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #
def test_clear_removes_the_metadata_layer(db_session) -> None:
    paper = make_paper(db_session, year=2020, abstract="abs")
    seed_full_metadata(db_session, paper)
    paper_id = paper.id

    outcome = metadata_clear.clear_metadata(db_session, paper)

    assert outcome.provenance_rows >= 2
    assert outcome.source_rows == 1
    assert outcome.identifier_rows == 1
    assert outcome.author_links == 1
    assert outcome.tag_links == 1

    session = db_session
    assert (
        session.query(PaperFieldProvenance)
        .filter(PaperFieldProvenance.paper_id == paper_id)
        .count()
        == 0
    )
    assert session.query(PaperSource).filter(PaperSource.paper_id == paper_id).count() == 0
    assert (
        session.query(PaperIdentifier)
        .filter(PaperIdentifier.paper_id == paper_id)
        .count()
        == 0
    )
    assert session.query(PaperAuthor).filter(PaperAuthor.paper_id == paper_id).count() == 0
    assert session.query(PapersTag).filter(PapersTag.paper_id == paper_id).count() == 0


def test_clear_blanks_the_columns_but_keeps_the_row(db_session) -> None:
    paper = make_paper(db_session, year=2020, abstract="abs", venue_year=2020)
    seed_full_metadata(db_session, paper)
    paper_id = paper.id

    metadata_clear.clear_metadata(db_session, paper)

    kept = paper_service.get_paper(db_session, paper_id)
    assert kept is not None, "the paper row itself must survive"
    assert kept.title == "", "title is NOT NULL -> empty"
    assert kept.abstract is None
    assert kept.year is None
    assert kept.doi is None
    assert kept.venue_id is None
    assert kept.venue_year is None
    assert kept.status == "INDEXED", "content-level status is not metadata"


def test_clear_releases_the_doi_and_re_derives_the_fingerprint(db_session) -> None:
    paper = make_paper(db_session)
    seed_full_metadata(db_session, paper)

    outcome = metadata_clear.clear_metadata(db_session, paper)

    assert outcome.fingerprint, "a fingerprint is still set"
    assert not outcome.fingerprint.startswith("doi:"), (
        "no identifier left -> fingerprint falls back to title/content"
    )
    assert paper.fingerprint == outcome.fingerprint


def test_clear_is_scoped_to_one_paper(db_session) -> None:
    target = make_paper(db_session)
    other = make_paper(db_session, title="Other paper")
    seed_full_metadata(db_session, target)
    seed_full_metadata(db_session, other)

    metadata_clear.clear_metadata(db_session, target)

    assert (
        db_session.query(PaperSource).filter(PaperSource.paper_id == other.id).count() == 1
    )
    kept_other = paper_service.get_paper(db_session, other.id)
    assert kept_other.title == "From IEEE"


def test_preview_counts_without_deleting(db_session) -> None:
    paper = make_paper(db_session)
    seed_full_metadata(db_session, paper)

    preview = metadata_clear.preview_clear(db_session, paper)

    assert preview.source_rows == 1
    assert preview.identifier_rows == 1
    assert (
        db_session.query(PaperSource).filter(PaperSource.paper_id == paper.id).count() == 1
    )


# --------------------------------------------------------------------------- #
# the endpoint
# --------------------------------------------------------------------------- #
def test_delete_metadata_endpoint_clears_and_reports(client, session_factory) -> None:
    session = session_factory()
    try:
        paper = make_paper(session)
        seed_full_metadata(session, paper)
        session.commit()
        paper_id = paper.id
    finally:
        session.close()

    response = client.delete(f"/api/papers/{paper_id}/metadata")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["paper_id"] == paper_id
    assert body["source_rows"] == 1
    assert body["identifier_rows"] == 1
    assert "title" in body["cleared_fields"]


def test_delete_metadata_dry_run_changes_nothing(client, session_factory) -> None:
    session = session_factory()
    try:
        paper = make_paper(session)
        seed_full_metadata(session, paper)
        session.commit()
        paper_id = paper.id
    finally:
        session.close()

    response = client.delete(f"/api/papers/{paper_id}/metadata", params={"dry_run": "true"})

    assert response.status_code == 200, response.text
    assert response.json()["source_rows"] == 1
    check = session_factory()
    try:
        assert (
            check.query(PaperSource).filter(PaperSource.paper_id == paper_id).count() == 1
        )
    finally:
        check.close()


def test_delete_metadata_404_for_unknown_paper(client) -> None:
    response = client.delete(f"/api/papers/{paper_service.new_uuid()}/metadata")
    assert response.status_code == 404
