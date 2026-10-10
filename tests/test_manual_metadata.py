"""Manual metadata edits and rollback (decision 12, section 9 of the design).

``manual`` is the one source rule R2 does not constrain, but it is still a claim:
these tests pin both halves -- the edit wins immediately, and it can be rolled back
to whatever was there before.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.security import require_api_key, require_write
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
    app.dependency_overrides[require_write] = lambda: "test-key"
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

# --------------------------------------------------------------------------- #
# 驳回（保留现值）：人工裁决的另一半（2026-10-10）
# 这些用例不需要真实来源行：source_id 为 None 时 claim_source_type 也是 None，
# 既不是启发式也不是 manual，照样算"待裁决分歧"。
# --------------------------------------------------------------------------- #
def test_dismiss_keeps_the_current_value_and_closes_the_dispute(db_session) -> None:
    """「保留现值」= 记一笔人工裁决，值不动，但这行不再进复核清单。"""
    paper = make_paper(db_session)
    first = provenance_service.set_field(db_session, paper, "authors", ["Zhuocheng Zhang"])
    losing = provenance_service.record_claim(
        db_session,
        paper_id=paper.id,
        field="authors",
        value=["msi"],
        make_current=False,
    )
    assert len(provenance_service.recorded_conflicts(db_session)) == 1, "先确认它本来是一处冲突"

    row = metadata_manual.dismiss_conflict(db_session, paper, "authors", losing.id)

    assert row.id == losing.id
    assert row.decided_by == provenance_service.DECIDED_DISMISSED
    assert row.value == ["msi"], "值不能被动过 —— 只是裁决过"
    assert provenance_service.current_claim(db_session, paper.id, "authors").id == first.id
    assert provenance_service.recorded_conflicts(db_session) == []


def test_a_dismissed_claim_can_still_be_rolled_back(db_session) -> None:
    """驳回不删历史：真觉得被拒值对，照样能回滚（设计 §8 规则 5）。"""
    paper = make_paper(db_session)
    provenance_service.set_field(db_session, paper, "authors", ["Zhuocheng Zhang"])
    losing = provenance_service.record_claim(
        db_session,
        paper_id=paper.id,
        field="authors",
        value=["msi"],
        make_current=False,
    )
    metadata_manual.dismiss_conflict(db_session, paper, "authors", losing.id)

    metadata_manual.rollback_metadata(db_session, paper, "authors", losing.id)

    assert provenance_service.current_claim(db_session, paper.id, "authors").id == losing.id
    assert paper_service.paper_author_names(paper) == ["msi"]


def test_rollback_is_the_other_half_of_the_same_verdict(db_session) -> None:
    """「采纳被拒值」走 rollback：分歧同样消失（落败方变成了当前值）。"""
    paper = make_paper(db_session)
    current = provenance_service.set_field(db_session, paper, "title", "Wrong title")
    better = provenance_service.record_claim(
        db_session, paper_id=paper.id, field="title", value="Better title", make_current=False
    )
    assert len(provenance_service.recorded_conflicts(db_session)) == 1

    metadata_manual.rollback_metadata(db_session, paper, "title", better.id)

    assert paper.title == "Better title"
    assert provenance_service.recorded_conflicts(db_session) == []
    assert current.id != better.id


def test_dismiss_refuses_the_current_claim(db_session) -> None:
    """现值不是"分歧"，驳回它没有意义 —— 要改值请走 PATCH/rollback。"""
    paper = make_paper(db_session)
    current = provenance_service.set_field(db_session, paper, "title", "Current title")

    with pytest.raises(LookupError):
        metadata_manual.dismiss_conflict(db_session, paper, "title", current.id)


def test_dismiss_refuses_a_claim_from_another_paper(db_session) -> None:
    paper = make_paper(db_session)
    other = make_paper(db_session, title="Other")
    provenance_service.set_field(db_session, other, "title", "Other title")
    foreign = provenance_service.record_claim(
        db_session, paper_id=other.id, field="title", value="Other old", make_current=False
    )

    with pytest.raises(LookupError):
        metadata_manual.dismiss_conflict(db_session, paper, "title", foreign.id)


def test_dismiss_refuses_a_mismatched_field(db_session) -> None:
    paper = make_paper(db_session)
    provenance_service.set_field(db_session, paper, "title", "Title")
    claim = provenance_service.record_claim(
        db_session, paper_id=paper.id, field="title", value="Old title", make_current=False
    )

    with pytest.raises(LookupError):
        metadata_manual.dismiss_conflict(db_session, paper, "abstract", claim.id)


def test_the_dismiss_endpoint_reports_404_for_an_unknown_claim(client, session_factory) -> None:
    """API 层把 LookupError 映射成 404（与 rollback 一致）。"""
    paper_id = seed_paper(session_factory)

    response = client.post(
        f"/api/papers/{paper_id}/metadata/conflicts/dismiss",
        json={"field": "title", "provenance_id": paper_service.new_uuid()},
    )

    assert response.status_code == 404


def test_the_dismiss_endpoint_reports_404_for_the_current_claim(client, session_factory) -> None:
    """现值不是分歧：驳回它没有意义，接口要说清而不是默默成功。"""
    session = session_factory()
    try:
        paper = make_paper(session)
        claim = provenance_service.set_field(session, paper, "title", "Current title")
        session.commit()
        paper_id, claim_id = paper.id, claim.id
    finally:
        session.close()

    response = client.post(
        f"/api/papers/{paper_id}/metadata/conflicts/dismiss",
        json={"field": "title", "provenance_id": claim_id},
    )

    assert response.status_code == 404
    assert "current value" in response.json()["detail"]


def test_the_dismiss_endpoint_closes_a_conflict(client, session_factory) -> None:
    """走一遍 HTTP：驳回后这条不再出现在复核清单里，值仍是原来那个。"""
    session = session_factory()
    try:
        paper = make_paper(session)
        provenance_service.set_field(session, paper, "authors", ["Zhuocheng Zhang"])
        losing = provenance_service.record_claim(
            session, paper_id=paper.id, field="authors", value=["msi"], make_current=False
        )
        session.commit()
        paper_id, claim_id = paper.id, losing.id
    finally:
        session.close()

    before = client.get("/api/metadata/review?limit=50").json()["conflicts"]
    assert [c["field"] for c in before if c["paper_id"] == paper_id] == ["authors"]
    assert before[0]["provenance_id"], "清单必须带上落败声明的 id，界面才能一键裁决"

    response = client.post(
        f"/api/papers/{paper_id}/metadata/conflicts/dismiss",
        json={"field": "authors", "provenance_id": claim_id},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dismissed"] is True and body["provenance_id"] == claim_id

    after = client.get("/api/metadata/review?limit=50").json()["conflicts"]
    assert [c for c in after if c["paper_id"] == paper_id] == []
    detail = client.get(f"/api/papers/{paper_id}/metadata").json()
    assert detail["values"]["authors"] == ["Zhuocheng Zhang"], "驳回不改值"
