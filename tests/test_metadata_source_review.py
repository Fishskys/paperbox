"""Source review preserves input, identity guards, transactionality and history."""

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.db.models import MetadataSourceReviewEvent, Paper, PaperSource
from app.services import metadata_import as importer, metadata_source_review as review
from app.services import metadata_sources as sources, paper_service
from tests.test_metadata_merge import make_paper, seed
from tests.test_metadata_api import client  # noqa: F401


def pending(session, *, raw=None):
    raw = raw or {"title": "A converter", "authors": ["Alice Smith"], "year": 2020, "doi": "10.1234/new"}
    parsed = importer.parse_records([raw])[0]
    return sources.upsert_source(session, source_type="import_file", source_ref=parsed.source_ref,
                                 raw=deepcopy(raw), match_status="ambiguous", match_method="title")


def target(session):
    paper = make_paper(session, title="A converter", year=2020)
    seed(session, paper, "authors", ["Alice Smith"], "pdf_embedded")
    return paper


def events(session):
    return session.scalar(select(func.count()).select_from(MetadataSourceReviewEvent))


def test_preview_is_read_only_and_apply_is_guarded_idempotent(db_session):
    paper, source = target(db_session), pending(db_session)
    raw = deepcopy(source.raw)
    preview = review.rematch(db_session, source.id, actor="tester")
    assert preview["match"]["paper_id"] == paper.id and not preview["applied"]
    assert source.paper_id is None and events(db_session) == 0
    result = review.rematch(db_session, source.id, actor="tester", apply=True,
                            revision=preview["revision"], expected_paper_id=paper.id)
    assert result["applied"] and source.match_status == "matched" and source.paper_id == paper.id
    assert paper.doi == "10.1234/new" and source.raw == raw and events(db_session) == 1
    event = db_session.scalar(select(MetadataSourceReviewEvent))
    assert event.actor == "tester" and event.payload["before"]["paper_id"] is None
    assert event.payload["after"]["paper_id"] == paper.id
    with pytest.raises(review.ReviewError):
        review.rematch(db_session, source.id, actor="tester", apply=True,
                       revision=preview["revision"], expected_paper_id=paper.id)
    assert events(db_session) == 1


def test_changed_matching_target_requires_fresh_preview(db_session):
    paper, source = target(db_session), pending(db_session)
    preview = review.detail(db_session, source.id)
    paper.title = "Completely unrelated detector"
    db_session.flush()
    with pytest.raises(review.ReviewError, match="匹配结果已变化"):
        review.rematch(db_session, source.id, actor="tester", apply=True,
                       revision=preview["revision"], expected_paper_id=paper.id)
    assert source.paper_id is None and events(db_session) == 0


def test_independent_publication_keeps_other_papers_and_raw(db_session):
    other, source = target(db_session), pending(db_session)
    seed(db_session, other, "identifier:doi", "10.1234/conference", "import_file")
    before = deepcopy(source.raw)
    result = review.create_paper(db_session, source.id, actor="writer", revision=review.detail(db_session, source.id)["revision"])
    created = db_session.get(Paper, result["created_paper_id"])
    assert created.id != other.id and created.status == "AWAITING_FILE"
    assert created.doi == "10.1234/new" and created.files == []
    assert other.doi == "10.1234/conference" and source.raw == before
    assert source.paper_id == created.id and source.match_method == "shell"


def test_create_and_attach_cannot_steal_an_identifier(db_session):
    owner, source = target(db_session), pending(db_session)
    seed(db_session, owner, "identifier:doi", "10.1234/new", "import_file")
    other = make_paper(db_session, title="Other paper")
    revision = review.detail(db_session, source.id)["revision"]
    with pytest.raises(review.ReviewError, match="已属于另一篇论文"):
        review.create_paper(db_session, source.id, actor="writer", revision=revision)
    with pytest.raises(review.ReviewError, match="已属于另一篇论文"):
        review.attach(db_session, source.id, other.id, actor="writer", revision=revision)
    assert source.paper_id is None and events(db_session) == 0


def test_manual_attach_rejects_strong_doi_disagreement(db_session):
    paper, source = target(db_session), pending(db_session)
    seed(db_session, paper, "identifier:doi", "10.1234/other", "import_file")
    with pytest.raises(review.ReviewError, match="身份冲突"):
        review.attach(db_session, source.id, paper.id, actor="writer")
    assert paper.doi == "10.1234/other" and source.paper_id is None


def test_rejection_survives_reimport_and_restore_preserves_history(db_session):
    source = pending(db_session)
    original = deepcopy(source.raw)
    first = review.detail(db_session, source.id)
    result = review.reject(db_session, source.id, actor="writer", revision=first["revision"])
    for apply in (False, True):
        imported = importer.import_payload(db_session, [original], apply=apply)
        assert imported.unchanged == 1 and imported.created_shell == 0
    assert source.match_status == "rejected" and source.raw == original
    assert db_session.scalar(select(func.count()).select_from(Paper)) == 0
    review.restore(db_session, source.id, actor="writer", revision=result["revision"])
    assert source.match_status == "pending" and source.raw == original and events(db_session) == 2
    with pytest.raises(review.ReviewError):
        review.reject(db_session, source.id, actor="writer", revision=first["revision"])


def test_batch_isolates_stale_rows_and_never_creates_papers(db_session):
    paper, first = target(db_session), pending(db_session)
    second = pending(db_session, raw={"title": "A missing paper", "doi": "10.1234/missing"})
    preview = review.rematch_batch(db_session, [{"source_id": first.id}, {"source_id": second.id}], actor="writer", apply=False)
    assert preview["applied"] == 0 and events(db_session) == 0
    a, b = preview["items"]
    result = review.rematch_batch(db_session, [
        {"source_id": first.id, "revision": a["revision"], "expected_paper_id": paper.id},
        {"source_id": second.id, "revision": "stale", "expected_paper_id": None},
    ], actor="writer", apply=True)
    assert result["applied"] == 1 and result["items"][1]["status"] == 409
    assert second.paper_id is None and db_session.scalar(select(func.count()).select_from(Paper)) == 1


def test_unmatched_recheck_stays_pending_without_creating_a_shell(db_session):
    source = pending(db_session)
    result = review.rematch(db_session, source.id, actor="writer", apply=True,
                            revision=review.detail(db_session, source.id)["revision"])
    assert not result["applied"] and source.match_status == "pending"
    assert db_session.scalar(select(func.count()).select_from(Paper)) == 0


def test_duplicate_title_fingerprint_rolls_back_independent_admission(db_session):
    from app.services import metadata_shell

    raw = {"title": "A converter", "authors": ["Alice Smith"], "year": 2020}
    metadata_shell.create_shell(db_session, raw, source_type="import_file", source_ref="existing")
    source = pending(db_session, raw=raw)
    with pytest.raises(review.ReviewError, match="已存在"):
        review.create_paper(db_session, source.id, actor="writer", revision=review.detail(db_session, source.id)["revision"])
    assert source.paper_id is None and events(db_session) == 0
    assert db_session.scalar(select(func.count()).select_from(Paper)) == 1


def test_review_api_failure_rolls_back_attribution_and_merged_values(client, session_factory, monkeypatch):
    with session_factory() as session:
        paper, source = target(session), pending(session)
        paper_id, source_id = paper.id, source.id
        session.commit()
    base = f"/api/metadata/sources/{source_id}"
    revision = client.get(base).json()["revision"]
    merge = review._merge

    def fail(*args):
        merge(*args)
        raise review.ReviewError("simulated persistence failure")

    monkeypatch.setattr(review, "_merge", fail)
    assert client.post(f"{base}/attach", json={"paper_id": paper_id, "revision": revision}).status_code == 409
    with session_factory() as session:
        assert session.get(PaperSource, source_id).paper_id is None
        assert session.get(Paper, paper_id).doi is None and events(session) == 0


def test_parse_failure_can_be_rejected_without_losing_the_input(db_session):
    source = sources.upsert_source(db_session, source_type="import_file", source_ref="broken", raw={"unexpected": []})
    detail = review.detail(db_session, source.id)
    assert detail["parse_error"]
    review.reject(db_session, source.id, actor="writer", revision=detail["revision"])
    assert source.raw == {"unexpected": []} and source.match_status == "rejected"


def test_review_api_covers_detail_rematch_reject_restore_and_validation(client, session_factory):
    with session_factory() as session:
        source = pending(session)
        source_id = source.id
        session.commit()
    base = f"/api/metadata/sources/{source_id}"
    first = client.get(base).json()
    assert first["source"]["source_id"] == source_id
    assert client.post(f"{base}/reject", json={"revision": "old"}).status_code == 422
    rejected = client.post(f"{base}/reject", json={"revision": first["revision"]})
    assert rejected.status_code == 200
    assert client.get("/api/metadata/review").json()["total"] == 0
    assert client.get("/api/metadata/review?status=rejected").json()["total"] == 1
    assert client.post(f"{base}/restore", json={"revision": rejected.json()["revision"]}).status_code == 200
    assert client.post("/api/metadata/sources/rematch", json={"items": []}).status_code == 422
    assert client.post("/api/metadata/sources/rematch", json={"items": [{"source_id": source_id}]}).status_code == 200
    assert client.get("/api/metadata/sources/missing").status_code == 404


@pytest.mark.parametrize("operation,body", [
    ("rematch", {"apply": False}),
    ("create-paper", {"revision": "a" * 64}),
    ("reject", {"revision": "a" * 64}),
    ("restore", {"revision": "a" * 64}),
    ("attach", {"paper_id": "paper"}),
])
def test_review_actions_require_write_permission(client, operation, body):
    from fastapi import HTTPException
    from app.core.security import require_write
    from app.main import app

    def deny():
        raise HTTPException(status_code=403, detail="write required")

    app.dependency_overrides[require_write] = deny
    assert client.post(f"/api/metadata/sources/source/{operation}", json=body).status_code == 403
    assert client.post("/api/metadata/sources/rematch", json={"items": [{"source_id": "source"}]}).status_code == 403
