"""Quality reconciliation preserves evidence, respects edits and is retryable."""

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.db.models import Paper, PaperFieldProvenance, PaperSource, new_uuid
from app.services import metadata_quality_review as quality
from app.services import metadata_sources as sources
from app.services import paper_service, provenance_service as prov


def seed(session, *, field="authors"):
    paper = Paper(id=new_uuid(), title="ADC", fingerprint=f"test:{new_uuid()}", status="INDEXED")
    session.add(paper)
    session.flush()
    source_ids = []
    for kind in ("pdf_heuristic", "pdf_embedded"):
        source = sources.upsert_source(session, source_type=kind, source_ref=new_uuid(), paper_id=paper.id,
                                       raw={}, match_status="matched")
        source_ids.append(source.id)
    kept = ["Alice Smith", "Student Member"] if field == "authors" else "This paper presents a time-domain converter."
    rejected = ["Alice Smith"] if field == "authors" else "IEEE Journal;2020;10;10.1109/example"
    current = prov.set_field(session, paper, field, kept, source_id=source_ids[0], override=True)
    losing = prov.record_claim(session, paper_id=paper.id, field=field, value=rejected, source_id=source_ids[1])
    session.commit()
    decision = {"paper_id": paper.id, "field": field, "current_id": current.id, "provenance_id": losing.id,
                "kept": kept, "rejected": rejected, "value": rejected if field == "authors" else kept,
                "reason": "PDF header confirms author; membership is not an author", "evidence": "Alice Smith, Student Member, IEEE"}
    return paper, current, losing, decision


def test_preview_does_not_mutate_claims_or_create_audit(db_session):
    paper, current, losing, decision = seed(db_session)
    before = db_session.scalar(select(func.count()).select_from(PaperSource))
    report = quality.apply_review(db_session, [decision])
    assert report["reviewed"] == report["changed"] == 1
    assert paper_service.paper_author_names(paper) == decision["kept"]
    assert current.is_current and losing.decided_by == "initial"
    assert db_session.scalar(select(func.count()).select_from(PaperSource)) == before


@pytest.mark.parametrize("field", ["authors", "abstract"])
def test_apply_keeps_history_and_records_machine_audit(db_session, field):
    paper, current, losing, decision = seed(db_session, field=field)
    assert prov.conflict_page(db_session)[1] == 0
    result = quality.apply_review(db_session, [decision], apply=True)
    db_session.commit()
    assert result["changed"] == int(field == "authors")
    assert prov.read_field(paper, field) == decision["value"]
    assert prov.conflict_page(db_session)[1] == 0
    assert losing.value == decision["rejected"]
    assert losing.decided_by == prov.DECIDED_QUALITY_REJECTED
    assert losing.decision_at is None and current.decision_at is None
    audit = db_session.scalar(select(PaperSource).where(PaperSource.importer == quality.IMPORTER))
    assert audit.raw["kept"] == decision["kept"]
    assert audit.raw["value"] == decision["value"]
    assert audit.raw["original_rejected_decider"] == "initial"
    assert audit.raw["reason"] == decision["reason"]
    assert audit.raw["reviewed_at"]
    count = db_session.scalar(select(func.count()).select_from(PaperFieldProvenance))
    retried = quality.apply_review(db_session, [decision], apply=True)
    assert retried["already_applied"] == 1 and retried["reviewed"] == 0
    assert retried["paper_ids"] == result["paper_ids"]
    assert db_session.scalar(select(func.count()).select_from(PaperFieldProvenance)) == count
    if field == "authors":
        prov.rollback_field(db_session, paper, field, current.id)
        assert paper_service.paper_author_names(paper) == decision["kept"]


@pytest.mark.parametrize("change", ["current_id", "kept", "rejected", "human", "source", "materialized"])
def test_stale_or_protected_preview_is_rejected(db_session, change):
    paper, current, losing, decision = seed(db_session)
    if change == "current_id":
        decision[change] = new_uuid()
    elif change in ("kept", "rejected"):
        decision[change] = ["Different Person"]
    elif change == "human":
        prov.record_human_decision(current)
    elif change == "source":
        db_session.get(PaperSource, losing.source_id).source_type = "import_file"
    elif change == "materialized":
        paper_service.set_paper_authors(db_session, paper, ["Different Person"])
    with pytest.raises(ValueError):
        quality.apply_review(db_session, [decision], apply=True)


@pytest.mark.parametrize("patch", [{"value": []}, {"evidence": ""}, {"reason": ""}, {"field": "year"}])
def test_invalid_decisions_fail_before_writing(db_session, patch):
    _, _, _, decision = seed(db_session)
    decision.update(patch)
    with pytest.raises(ValueError):
        quality.apply_review(db_session, [decision], apply=True)


def test_batch_failure_can_rollback_every_change(db_session):
    paper, current, losing, decision = seed(db_session)
    invalid = deepcopy(decision)
    invalid["paper_id"] = new_uuid()
    with pytest.raises(ValueError):
        quality.apply_review(db_session, [decision, invalid], apply=True)
    db_session.rollback()
    assert prov.current_claim(db_session, paper.id, "authors").id == current.id
    assert losing.decided_by == "initial"


def test_explicit_materialized_snapshot_is_validated_and_audited(db_session):
    paper, _, _, decision = seed(db_session)
    paper.paper_authors[1].display_name = "(Student Member"
    decision["materialized_kept"] = ["Alice Smith", "(Student Member"]
    quality.apply_review(db_session, [decision], apply=True)
    audit = db_session.scalar(select(PaperSource).where(PaperSource.importer == quality.IMPORTER))
    assert audit.raw["materialized_before"] == decision["materialized_kept"]
    assert audit.raw["materialized_after"] == ["Alice Smith"]


@pytest.mark.parametrize("decider", ["initial", "structured_override"])
def test_identical_historical_duplicates_are_archived_together(db_session, decider):
    paper, _, losing, decision = seed(db_session, field="abstract")
    duplicate = PaperFieldProvenance(id=new_uuid(), paper_id=paper.id, source_id=losing.source_id,
                                    field="abstract", value=losing.value, is_current=False, decided_by=decider)
    db_session.add(duplicate)
    db_session.flush()
    decision["duplicate_provenance_ids"] = [duplicate.id]
    quality.apply_review(db_session, [decision], apply=True)
    assert duplicate.decided_by == prov.DECIDED_QUALITY_REJECTED
    assert prov.conflict_page(db_session)[1] == 0
    assert duplicate.value == decision["rejected"]


def test_index_refresh_reports_failures_without_reembedding(db_session, monkeypatch):
    from app.db.models import PaperChunk
    from app.search import opensearch

    paper, _, _, decision = seed(db_session)
    quality.apply_review(db_session, [decision], apply=True)
    chunk = PaperChunk(id=new_uuid(), paper_id=paper.id, chunk_index=0, text="Existing chunk", page_start=1, page_end=1)
    db_session.add(chunk)
    db_session.flush()
    calls = []
    def update(entries):
        calls.extend(entries)
        return {"updated": 0, "failed": 1}
    monkeypatch.setattr(opensearch, "bulk_update_documents", update)
    assert quality.refresh_authors(db_session, [paper.id]) == {"updated": 0, "failed": 1}
    assert calls == [{"chunk_id": chunk.id, "doc": {"authors": ["Alice Smith"]}}]
    assert chunk.text == "Existing chunk"


def test_missing_pdf_evidence_file_aborts_the_review(db_session):
    _, _, _, decision = seed(db_session)
    decision["evidence"] = {"file_id": new_uuid(), "pdf_sha256": "old"}
    with pytest.raises(ValueError, match="PDF evidence"):
        quality.apply_review(db_session, [decision], apply=True)
