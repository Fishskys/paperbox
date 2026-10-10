"""The metadata endpoints: import, review, attach, apply (section 9 of the design).

The HTTP contract matters here as much as the behaviour: ``dry_run`` is the
default, an unsupported media type is a 415 (not a silent empty import), and a
human attribution is a 404 when either side of it does not exist.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.security import require_api_key, require_write
from app.db.models import Paper, PaperSource
from app.db.session import get_db
from app.main import app
from app.services import metadata_identifiers as ids
from app.services import metadata_sources as sources
from app.services import paper_service, provenance_service

DOI = "10.1109/JSSC.2015.2441234"

IEEE_SAMPLE = {
    "total_records": 1,
    "articles": [
        {
            "title": "A 0.6 V Low Power SRAM with Leakage Reduction",
            "abstract": "This paper presents a leakage reduction technique.",
            "doi": DOI,
            "article_number": "7065247",
            "issn": "0018-9219",
            "publication_title": "IEEE Journal of Solid-State Circuits",
            "publication_year": 2015,
            "publication_date": "July 2015",
            "content_type": "Journals",
            "volume": "62",
            "issue": "7",
            "start_page": "631",
            "end_page": "635",
            "html_url": "https://ieeexplore.ieee.org/document/7065247",
            "authors": [
                {"full_name": "Alice Smith", "author_order": 1},
                {"full_name": "Bob Jones", "author_order": 2},
            ],
            "index_terms": {
                "ieee_terms": {"terms": ["SRAM"]},
                "author_terms": {"terms": ["leakage reduction"]},
            },
        }
    ],
}


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


def seed_paper_with_doi(session_factory, **overrides) -> str:
    session = session_factory()
    try:
        values = {
            "id": paper_service.new_uuid(),
            "title": "Placeholder",
            "fingerprint": f"sha256:{paper_service.new_uuid()}",
            "status": "INDEXED",
        }
        values.update(overrides)
        paper = Paper(**values)
        session.add(paper)
        session.flush()
        ids.upsert_identifier(
            session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value=DOI
        )
        ids.refresh_primary(session, paper.id)
        session.commit()
        return paper.id
    finally:
        session.close()


def count(session_factory, model) -> int:
    session = session_factory()
    try:
        return session.query(model).count()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# POST /api/metadata/import
# --------------------------------------------------------------------------- #
def test_import_defaults_to_a_dry_run(client, session_factory) -> None:
    paper_id = seed_paper_with_doi(session_factory)

    response = client.post("/api/metadata/import", json=IEEE_SAMPLE)

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["matched"] == 1
    assert body["sources"][0]["paper_id"] == paper_id
    assert count(session_factory, PaperSource) == 0


def test_import_applies_when_asked(client, session_factory) -> None:
    paper_id = seed_paper_with_doi(session_factory)

    response = client.post("/api/metadata/import?apply=true", json=IEEE_SAMPLE)

    assert response.status_code == 200
    assert response.json()["dry_run"] is False
    session = session_factory()
    try:
        paper = session.get(Paper, paper_id)
        assert paper.volume == "62"
        assert paper.pages == "631-635"
    finally:
        session.close()


def test_import_accepts_dry_run_false_as_the_same_thing(client, session_factory) -> None:
    seed_paper_with_doi(session_factory)

    response = client.post("/api/metadata/import?dry_run=false", json=IEEE_SAMPLE)

    assert response.json()["dry_run"] is False
    assert count(session_factory, PaperSource) == 1


def test_import_accepts_a_multipart_file(client, session_factory) -> None:
    seed_paper_with_doi(session_factory)
    payload = json.dumps(IEEE_SAMPLE).encode("utf-8")

    response = client.post(
        "/api/metadata/import?apply=true",
        files={"file": ("ieee.json", payload, "application/json")},
    )

    assert response.status_code == 200
    assert response.json()["matched"] == 1
    assert count(session_factory, PaperSource) == 1


def test_import_reports_a_shell_for_an_unknown_record(client, session_factory) -> None:
    response = client.post("/api/metadata/import?apply=true", json=IEEE_SAMPLE)

    body = response.json()
    assert body["created_shell"] == 1
    session = session_factory()
    try:
        paper = session.query(Paper).one()
        assert paper.status == paper_service.STATUS_AWAITING_FILE
    finally:
        session.close()


def test_import_is_idempotent_over_http(client, session_factory) -> None:
    seed_paper_with_doi(session_factory)

    client.post("/api/metadata/import?apply=true", json=IEEE_SAMPLE)
    second = client.post("/api/metadata/import?apply=true", json=IEEE_SAMPLE)

    assert second.json()["unchanged"] == 1
    assert count(session_factory, PaperSource) == 1


def test_import_rejects_an_unsupported_media_type(client) -> None:
    response = client.post(
        "/api/metadata/import",
        content=b"title,x\n",
        headers={"content-type": "text/csv"},
    )

    assert response.status_code == 415


def test_import_rejects_a_broken_body(client) -> None:
    response = client.post(
        "/api/metadata/import",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422


def test_import_rejects_an_unknown_source_type(client) -> None:
    response = client.post(
        "/api/metadata/import?source_type=magic", json=IEEE_SAMPLE
    )

    assert response.status_code == 422
    assert "source_type" in response.json()["detail"]


def test_import_honours_the_limit(client) -> None:
    record = IEEE_SAMPLE["articles"][0]
    payload = {"articles": [record, {**record, "doi": "10.1/second"}]}

    body = client.post("/api/metadata/import?apply=true&limit=1", json=payload).json()

    assert body["total"] == 1


# --------------------------------------------------------------------------- #
# GET /api/metadata/review
# --------------------------------------------------------------------------- #
def test_review_lists_ambiguous_sources_and_conflicts(client, session_factory) -> None:
    session = session_factory()
    try:
        paper = Paper(
            id=paper_service.new_uuid(),
            title="A 0.6 V Low Power SRAM with Leakage Reduction",
            fingerprint=f"sha256:{paper_service.new_uuid()}",
            status="INDEXED",
        )
        session.add(paper)
        session.flush()
        provenance_service.set_field(session, paper, "title", "Kept title", override=True)
        provenance_service.record_claim(
            session, paper_id=paper.id, field="title", value="Rejected title"
        )
        sources.upsert_source(
            session,
            source_type=sources.SOURCE_TYPE_IMPORT_FILE,
            source_ref="doi:10.1/ambiguous",
            raw={"title": "A 0.6 V Low Power SRAM with Leakage Reduction"},
            match_status=sources.MATCH_STATUS_AMBIGUOUS,
            match_method="title",
            match_confidence=0.5,
        )
        session.commit()
    finally:
        session.close()

    response = client.get("/api/metadata/review")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["match_status"] == "ambiguous"
    assert body["items"][0]["match_method"] == "title"
    assert body["conflicts"][0]["kept"] == "Kept title"
    assert body["conflicts"][0]["rejected"] == "Rejected title"


def test_review_can_be_narrowed_by_status(client, session_factory) -> None:
    session = session_factory()
    try:
        sources.upsert_source(
            session,
            source_type=sources.SOURCE_TYPE_IMPORT_FILE,
            source_ref="doi:10.1/pending",
            raw={},
            match_status=sources.MATCH_STATUS_PENDING,
        )
        session.commit()
    finally:
        session.close()

    assert client.get("/api/metadata/review?status=pending").json()["total"] == 1
    assert client.get("/api/metadata/review?status=ambiguous").json()["total"] == 0


# --------------------------------------------------------------------------- #
# POST /api/metadata/sources/{source_id}/attach
# --------------------------------------------------------------------------- #
def test_attach_replays_the_stored_record_onto_the_paper(client, session_factory) -> None:
    session = session_factory()
    try:
        paper = Paper(
            id=paper_service.new_uuid(),
            title="Manually chosen paper",
            fingerprint=f"sha256:{paper_service.new_uuid()}",
            status="INDEXED",
        )
        session.add(paper)
        source = sources.upsert_source(
            session,
            source_type=sources.SOURCE_TYPE_IMPORT_FILE,
            source_ref=f"doi:{DOI.casefold()}",
            raw=IEEE_SAMPLE["articles"][0],
            match_status=sources.MATCH_STATUS_AMBIGUOUS,
        )
        session.commit()
        paper_id, source_id = paper.id, source.id
    finally:
        session.close()

    response = client.post(
        f"/api/metadata/sources/{source_id}/attach", json={"paper_id": paper_id}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["match_status"] == "matched"
    assert body["match_method"] == "manual"
    assert "volume" in body["merged_fields"]
    session = session_factory()
    try:
        paper = session.get(Paper, paper_id)
        assert paper.volume == "62"
        assert paper.doi == DOI.casefold()
    finally:
        session.close()


def test_attach_404s_for_an_unknown_source(client, session_factory) -> None:
    paper_id = seed_paper_with_doi(session_factory)

    response = client.post(
        f"/api/metadata/sources/{paper_service.new_uuid()}/attach",
        json={"paper_id": paper_id},
    )

    assert response.status_code == 404


def test_attach_404s_for_an_unknown_paper(client, session_factory) -> None:
    session = session_factory()
    try:
        source = sources.upsert_source(
            session,
            source_type=sources.SOURCE_TYPE_IMPORT_FILE,
            source_ref="doi:10.1/x",
            raw={},
            match_status=sources.MATCH_STATUS_PENDING,
        )
        session.commit()
        source_id = source.id
    finally:
        session.close()

    response = client.post(
        f"/api/metadata/sources/{source_id}/attach",
        json={"paper_id": paper_service.new_uuid()},
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# POST /api/metadata/apply
# --------------------------------------------------------------------------- #
def test_apply_attaches_the_decisions_from_a_report(client, session_factory) -> None:
    session = session_factory()
    try:
        paper = Paper(
            id=paper_service.new_uuid(),
            title="Manually chosen paper",
            fingerprint=f"sha256:{paper_service.new_uuid()}",
            status="INDEXED",
        )
        session.add(paper)
        source = sources.upsert_source(
            session,
            source_type=sources.SOURCE_TYPE_IMPORT_FILE,
            source_ref=f"doi:{DOI.casefold()}",
            raw=IEEE_SAMPLE["articles"][0],
            match_status=sources.MATCH_STATUS_AMBIGUOUS,
        )
        session.commit()
        paper_id, source_id, source_ref = paper.id, source.id, source.source_ref
    finally:
        session.close()

    response = client.post(
        "/api/metadata/apply",
        json={
            "entries": [
                {
                    "source_ref": source_ref,
                    "paper_id": paper_id,
                    "source_type": sources.SOURCE_TYPE_IMPORT_FILE,
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json() == {"applied": 1, "skipped": 0, "errors": []}
    session = session_factory()
    try:
        assert session.get(PaperSource, source_id).paper_id == paper_id
        assert session.get(Paper, paper_id).volume == "62"
    finally:
        session.close()


def test_apply_overwrite_mode_writes_manual_claims(client, session_factory) -> None:
    session = session_factory()
    try:
        paper = Paper(
            id=paper_service.new_uuid(),
            title="Existing title",
            fingerprint=f"sha256:{paper_service.new_uuid()}",
            status="INDEXED",
        )
        session.add(paper)
        source = sources.upsert_source(
            session,
            source_type=sources.SOURCE_TYPE_IMPORT_FILE,
            source_ref=f"doi:{DOI.casefold()}",
            raw=IEEE_SAMPLE["articles"][0],
            match_status=sources.MATCH_STATUS_AMBIGUOUS,
        )
        session.commit()
        paper_id, source_ref = paper.id, source.source_ref
    finally:
        session.close()

    response = client.post(
        "/api/metadata/apply",
        json={
            "entries": [{"source_ref": source_ref, "paper_id": paper_id}],
            "mode": "overwrite",
            "fields": ["title"],
        },
    )

    assert response.json()["applied"] == 1
    session = session_factory()
    try:
        paper = session.get(Paper, paper_id)
        assert paper.title == "A 0.6 V Low Power SRAM with Leakage Reduction"
        claim = provenance_service.current_claim(session, paper_id, "title")
        assert claim.decided_by == provenance_service.DECIDED_MANUAL
    finally:
        session.close()


def test_apply_reports_an_unknown_source(client, session_factory) -> None:
    paper_id = seed_paper_with_doi(session_factory)

    body = client.post(
        "/api/metadata/apply",
        json={"entries": [{"source_ref": "doi:10.1/missing", "paper_id": paper_id}]},
    ).json()

    assert body["applied"] == 0
    assert body["skipped"] == 1
    assert body["errors"][0]["error"] == "source not found"


def test_apply_reports_an_unknown_paper(client, session_factory) -> None:
    session = session_factory()
    try:
        source = sources.upsert_source(
            session,
            source_type=sources.SOURCE_TYPE_IMPORT_FILE,
            source_ref="doi:10.1/x",
            raw={},
            match_status=sources.MATCH_STATUS_PENDING,
        )
        session.commit()
        source_ref = source.source_ref
    finally:
        session.close()

    body = client.post(
        "/api/metadata/apply",
        json={"entries": [{"source_ref": source_ref, "paper_id": paper_service.new_uuid()}]},
    ).json()

    assert body["skipped"] == 1
    assert body["errors"][0]["error"] == "paper not found"

def test_the_import_report_splits_detected_from_imported_and_failed(client) -> None:
    """回执给三层数字：检测到 / 导入成功 / 失败，并点名失败条目（2026-10-10）。

    一条读不出来的记录不该让整批 422；它被跳过、写进 ``failures``，其余照常。
    解析器对畸形输入很稳，所以这里用注入式失败来确定性地造出"那条坏记录"。
    """
    from unittest import mock

    from app.services import metadata_import as importer

    payload = {
        "total_records": 3,
        "articles": [
            dict(IEEE_SAMPLE["articles"][0], doi=f"10.1109/TEST.{index}") for index in range(3)
        ],
    }
    payload["articles"][1] = dict(payload["articles"][1], doi="10.1109/TEST.BROKEN")
    real_parse = importer.parse_record

    def explode(record, fmt):
        if record.get("doi") == "10.1109/TEST.BROKEN":
            raise ValueError("year 18202014 is out of range")
        return real_parse(record, fmt)

    with mock.patch.object(importer, "parse_record", side_effect=explode):
        response = client.post("/api/metadata/import", json=payload)

    assert response.status_code == 200, "一条坏记录不再把整批变成 422"
    body = response.json()
    assert body["detected"] == 3
    assert body["total"] == 2
    assert body["failed"] == 1
    assert body["skipped"] == 0
    assert body["total"] + body["failed"] + body["skipped"] == body["detected"]
    failure = body["failures"][0]
    assert failure["index"] == 1
    assert failure["identifier"] == "10.1109/TEST.BROKEN"
    assert "year 18202014 is out of range" in failure["reason"]
