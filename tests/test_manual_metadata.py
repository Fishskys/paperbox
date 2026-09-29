"""Manual metadata edits and rollback (decision 12, section 9 of the design).

``manual`` is the one source rule R2 does not constrain, but it is still a claim:
these tests pin both halves -- the edit wins immediately, and it can be rolled back
to whatever was there before.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.security import require_api_key
from app.db.models import Paper
from app.db.session import get_db
from app.main import app
from app.services import metadata_identifiers as ids
from app.services import metadata_manual
from app.services import metadata_merge as merge
from app.services import metadata_sources as sources
from app.services import metadata_tags as tags
from app.services import paper_service, provenance_service


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


def seed_structured_title(session, paper, title, source_type="ieee_api") -> None:
    source = sources.upsert_source(
        session,
        source_type=source_type,
        source_ref=f"{source_type}:{paper.id}",
        raw={},
        paper_id=paper.id,
        match_status=sources.MATCH_STATUS_MATCHED,
    )
    provenance_service.set_field(
        session, paper, "title", title, source_id=source.id, override=True
    )


@pytest.fixture()
def client(session_factory):
    def _db():
        session = session_factory()
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


def seed_paper(session_factory, **overrides) -> str:
    session = session_factory()
    try:
        paper = make_paper(session, **overrides)
        session.commit()
        return paper.id
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #
def test_a_manual_edit_overrides_a_structured_value(db_session) -> None:
    paper = make_paper(db_session)
    seed_structured_title(db_session, paper, "From IEEE")

    result = metadata_manual.patch_metadata(db_session, paper, {"title": "Hand fixed"})

    assert result.fields == ["title"]
    assert paper.title == "Hand fixed"
    claim = provenance_service.current_claim(db_session, paper.id, "title")
    assert claim.decided_by == provenance_service.DECIDED_MANUAL
    assert claim.source_id == metadata_manual.manual_source(db_session, paper).id
    history = provenance_service.field_history(db_session, paper.id, "title")
    assert "From IEEE" in [row.value for row in history if not row.is_current]


def test_one_manual_source_is_reused_for_every_edit(db_session) -> None:
    paper = make_paper(db_session)

    metadata_manual.patch_metadata(db_session, paper, {"title": "A"})
    metadata_manual.patch_metadata(db_session, paper, {"title": "B", "volume": "62"})

    rows = [
        row
        for row in sources.sources_for_paper(db_session, paper.id)
        if row.source_type == sources.SOURCE_TYPE_MANUAL
    ]
    assert len(rows) == 1
    assert rows[0].source_ref == sources.manual_ref(paper.id)


def test_simple_fields_land_on_their_columns(db_session) -> None:
    paper = make_paper(db_session)

    metadata_manual.patch_metadata(
        db_session,
        paper,
        {
            "abstract": "An abstract",
            "year": 2015,
            "volume": "62",
            "issue": "7",
            "pages": "631-635",
            "paper_type": "journal",
            "publication_date": "2015-07-01",
            "language": "en",
            "url": "https://example.org/x",
        },
    )

    assert paper.abstract == "An abstract"
    assert paper.year == 2015
    assert (paper.volume, paper.issue, paper.pages) == ("62", "7", "631-635")
    assert paper.paper_type == "journal"
    assert paper.publication_date.isoformat() == "2015-07-01"
    assert paper.language == "en"
    assert paper.url == "https://example.org/x"


def test_a_venue_edit_creates_the_edition(db_session) -> None:
    paper = make_paper(db_session)

    metadata_manual.patch_metadata(
        db_session, paper, {"venue": "ISSCC", "venue_year": 2015}
    )

    assert paper.venue.name == "ISSCC"
    assert paper.venue_year == 2015
    assert paper.venue_edition.year == 2015


def test_a_venue_edit_accepts_a_mapping(db_session) -> None:
    paper = make_paper(db_session)

    metadata_manual.patch_metadata(
        db_session, paper, {"venue": {"name": "ISSCC", "year": 2016}}
    )

    assert paper.venue_year == 2016


def test_authors_and_tags_can_be_edited(db_session) -> None:
    paper = make_paper(db_session)

    metadata_manual.patch_metadata(
        db_session, paper, {"authors": ["Alice Smith", "Bob Jones"], "tags": ["sram", "leakage"]}
    )

    assert paper_service.paper_author_names(paper) == ["Alice Smith", "Bob Jones"]
    assert tags.tags_for_paper(db_session, paper, kind=tags.KIND_SOURCE_TAG) == [
        "leakage",
        "sram",
    ]


def test_editing_tags_replaces_the_previous_ones(db_session) -> None:
    paper = make_paper(db_session)

    metadata_manual.patch_metadata(db_session, paper, {"tags": ["old"]})
    metadata_manual.patch_metadata(db_session, paper, {"tags": ["new"]})

    assert tags.tags_for_paper(db_session, paper, kind=tags.KIND_SOURCE_TAG) == ["new"]


def test_a_doi_edit_replaces_the_identifier_and_upgrades_the_fingerprint(db_session) -> None:
    paper = make_paper(db_session)
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1/old"
    )
    ids.refresh_primary(db_session, paper.id)
    ids.upgrade_fingerprint(db_session, paper)

    result = metadata_manual.patch_metadata(db_session, paper, {"doi": "10.1/new"})

    rows = ids.identifiers_for_paper(db_session, paper.id)
    assert [row.normalized_value for row in rows] == ["10.1/new"]
    assert rows[0].is_primary is True
    assert paper.doi == "10.1/new"
    assert paper.fingerprint == "doi:10.1/new"
    assert result.fingerprint == "doi:10.1/new"


def test_a_second_identifier_edit_moves_the_mirror_column(db_session) -> None:
    """The mirror columns follow a human correction, not just fill a blank.

    2026-09-30: ``mirror_legacy_columns`` is fill-only (the pipeline must not
    blank a value a source provided), so a paper that already had a DOI kept the
    **superseded** one in ``papers.doi`` while ``paper_identifiers`` and
    ``papers.fingerprint`` already said the new one. That drift made
    ``test_a_doi_edit_replaces_the_identifier_and_upgrades_the_fingerprint`` fail
    intermittently in full-suite runs. ``patch_metadata`` now passes
    ``force_scheme``, which is what this test pins.
    """
    paper = make_paper(db_session)
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1/old"
    )
    ids.refresh_primary(db_session, paper.id)
    ids.mirror_legacy_columns(db_session, paper)
    assert paper.doi == "10.1/old"

    metadata_manual.patch_metadata(db_session, paper, {"doi": "10.1/new"})

    assert paper.doi == "10.1/new"
    assert [row.normalized_value for row in ids.identifiers_for_paper(db_session, paper.id)] == [
        "10.1/new"
    ]


def test_an_arxiv_edit_sets_the_mirror_and_the_fingerprint(db_session) -> None:
    paper = make_paper(db_session)

    metadata_manual.patch_metadata(db_session, paper, {"arxiv_id": "1706.03762v5"})

    assert paper.arxiv_id == "1706.03762"
    assert paper.fingerprint == "arxiv:1706.03762"


def test_unknown_keys_are_reported_not_ignored(db_session) -> None:
    paper = make_paper(db_session)

    result = metadata_manual.patch_metadata(
        db_session, paper, {"title": "Kept", "titel": "typo"}
    )

    assert result.fields == ["title"]
    assert result.rejected == ["titel"]
    assert paper.title == "Kept"


def test_empty_values_are_skipped(db_session) -> None:
    paper = make_paper(db_session, title="Original title")

    result = metadata_manual.patch_metadata(
        db_session, paper, {"title": None, "doi": "   "}
    )

    assert result.fields == []
    assert paper.title == "Original title"


# --------------------------------------------------------------------------- #
# the view
# --------------------------------------------------------------------------- #
def test_the_view_reports_values_history_and_sources(db_session) -> None:
    paper = make_paper(db_session)
    seed_structured_title(db_session, paper, "From IEEE")
    metadata_manual.patch_metadata(db_session, paper, {"title": "Hand fixed"})

    view = metadata_manual.metadata_view(db_session, paper)

    assert view["paper_id"] == paper.id
    assert view["values"]["title"] == "Hand fixed"
    entries = view["provenance"]["title"]
    assert [entry["is_current"] for entry in entries].count(True) == 1
    assert {entry["decided_by"] for entry in entries} >= {"manual"}
    assert {row["source_type"] for row in view["sources"]} == {"ieee_api", "manual"}
    assert view["identifiers"] == []
    assert view["tags"] == {}


def test_the_view_exposes_the_identifiers(db_session) -> None:
    paper = make_paper(db_session)
    ids.upsert_identifier(
        db_session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value="10.1/x"
    )
    ids.refresh_primary(db_session, paper.id)

    view = metadata_manual.metadata_view(db_session, paper)

    assert view["identifiers"][0]["scheme"] == "doi"
    assert view["identifiers"][0]["is_primary"] is True


# --------------------------------------------------------------------------- #
# rollback
# --------------------------------------------------------------------------- #
def test_rollback_restores_the_previous_value(db_session) -> None:
    paper = make_paper(db_session)
    seed_structured_title(db_session, paper, "From IEEE")
    before = provenance_service.current_claim(db_session, paper.id, "title")
    metadata_manual.patch_metadata(db_session, paper, {"title": "Hand fixed"})

    row = metadata_manual.rollback_metadata(db_session, paper, "title", before.id)

    assert row.id == before.id
    assert paper.title == "From IEEE"
    assert provenance_service.current_claim(db_session, paper.id, "title").id == before.id


def test_rollback_of_a_doi_restores_the_identifier_and_fingerprint(db_session) -> None:
    paper = make_paper(db_session)
    provenance_service.set_field(db_session, paper, "identifier:doi", "10.1/old")
    ids.refresh_primary(db_session, paper.id)
    ids.upgrade_fingerprint(db_session, paper)
    before = provenance_service.current_claim(db_session, paper.id, "identifier:doi")
    metadata_manual.patch_metadata(db_session, paper, {"doi": "10.1/new"})

    metadata_manual.rollback_metadata(db_session, paper, "identifier:doi", before.id)

    assert paper.doi == "10.1/old"
    assert paper.fingerprint == "doi:10.1/old"


def test_rollback_refuses_a_foreign_claim(db_session) -> None:
    paper = make_paper(db_session)
    other = make_paper(db_session, title="Other")
    claim = provenance_service.set_field(db_session, other, "title", "Other title")

    with pytest.raises(LookupError):
        metadata_manual.rollback_metadata(db_session, paper, "title", claim.id)


def test_a_manual_value_still_loses_to_a_later_manual_value(db_session) -> None:
    """Two hand edits are both ``manual``; the newest wins (no ranking involved)."""
    paper = make_paper(db_session)

    metadata_manual.patch_metadata(db_session, paper, {"title": "First"})
    metadata_manual.patch_metadata(db_session, paper, {"title": "Second"})

    assert paper.title == "Second"
    assert provenance_service.current_claim(db_session, paper.id, "title").value == "Second"
    assert merge.is_structured(sources.SOURCE_TYPE_MANUAL) is True


# --------------------------------------------------------------------------- #
# the HTTP contract
# --------------------------------------------------------------------------- #
def test_get_metadata_returns_the_view(client, session_factory) -> None:
    paper_id = seed_paper(session_factory)

    response = client.get(f"/api/papers/{paper_id}/metadata")

    assert response.status_code == 200
    body = response.json()
    assert body["paper_id"] == paper_id
    assert body["values"]["title"] == "Original title"
    assert body["fingerprint"]


def test_get_metadata_404s_for_an_unknown_paper(client) -> None:
    assert client.get(f"/api/papers/{paper_service.new_uuid()}/metadata").status_code == 404


def test_patch_metadata_over_http(client, session_factory) -> None:
    paper_id = seed_paper(session_factory)

    response = client.patch(
        f"/api/papers/{paper_id}/metadata",
        json={"title": "Edited over HTTP", "year": 2015, "doi": "10.1/http"},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body["fields"]) == {"title", "year", "doi"}
    assert body["fingerprint"] == "doi:10.1/http"
    session = session_factory()
    try:
        paper = session.get(Paper, paper_id)
        assert paper.title == "Edited over HTTP"
        assert paper.year == 2015
    finally:
        session.close()


def test_patch_metadata_reports_unknown_keys(client, session_factory) -> None:
    paper_id = seed_paper(session_factory)

    body = client.patch(
        f"/api/papers/{paper_id}/metadata", json={"titel": "typo"}
    ).json()

    assert body["rejected"] == ["titel"]
    assert body["fields"] == []


def test_rollback_over_http(client, session_factory) -> None:
    session = session_factory()
    try:
        paper = make_paper(session)
        claim = provenance_service.set_field(session, paper, "title", "Original title")
        provenance_id = claim.id
        session.commit()
        paper_id = paper.id
    finally:
        session.close()
    client.patch(f"/api/papers/{paper_id}/metadata", json={"title": "Edited"})

    response = client.post(
        f"/api/papers/{paper_id}/metadata/rollback",
        json={"field": "title", "provenance_id": provenance_id},
    )

    assert response.status_code == 200
    assert response.json()["value"] == "Original title"
    session = session_factory()
    try:
        assert session.get(Paper, paper_id).title == "Original title"
    finally:
        session.close()


def test_rollback_404s_for_an_unknown_claim(client, session_factory) -> None:
    paper_id = seed_paper(session_factory)

    response = client.post(
        f"/api/papers/{paper_id}/metadata/rollback",
        json={"field": "title", "provenance_id": paper_service.new_uuid()},
    )

    assert response.status_code == 404