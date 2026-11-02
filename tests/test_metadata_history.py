"""Historical PDF repair preserves human decisions, snapshots and index retries."""

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.db.models import MetadataIndexUpdate, MetadataQualityDecision, PaperChunk, PaperFile, PaperSource, new_uuid
from app.services import metadata_history as history, metadata_policy as policy, provenance_service as prov
from tests.test_metadata_merge import make_paper, make_source, seed


def fixture(session, *, kept=None, rejected=None):
    paper = make_paper(session)
    kept = kept or ["Tomislav Matic"]
    rejected = rejected or ["Tomislav Matić"]
    current = seed(session, paper, "authors", kept, "pdf_embedded")
    candidate_source = make_source(session, paper.id, "pdf_heuristic")
    candidate = prov.record_claim(session, paper_id=paper.id, field="authors", value=rejected, source_id=candidate_source.id)
    file = PaperFile(id=new_uuid(), paper_id=paper.id, kind="original", object_key=f"papers/{paper.id}/original.pdf",
                     bucket="test", content_type="application/pdf", size_bytes=10, sha256="a" * 64, is_primary=True)
    session.add(file)
    session.flush()
    snapshot = history.snapshots(session, [{"paper_id": paper.id, "field": "authors"}])[0]
    extracted = {"body": {"authors": rejected}, "quality": {"rule_version": policy.QUALITY_VERSION,
        "text_state": "ok", "byline": rejected, "byline_complete": True, "primary_file_id": file.id, "pdf_sha256": file.sha256}}
    return paper, current, candidate, file, snapshot, extracted


def test_preview_is_field_scoped_and_does_not_write(db_session):
    paper, current, candidate, _, snapshot, extracted = fixture(db_session)
    before = db_session.scalar(select(func.count()).select_from(PaperSource))
    decision = history.plan(snapshot, extracted)
    assert decision["selected"] == ["Tomislav Matić"] and decision["changed"]
    assert prov.current_claim(db_session, paper.id, "authors").id == current.id
    assert candidate.decided_by == "initial"
    assert db_session.scalar(select(func.count()).select_from(PaperSource)) == before
    assert db_session.scalar(select(func.count()).select_from(MetadataQualityDecision)) == 0


def test_apply_preserves_sources_and_is_idempotent(db_session):
    paper, current, candidate, _, snapshot, extracted = fixture(db_session)
    source = db_session.get(PaperSource, current.source_id)
    original_raw = deepcopy(source.raw)
    decision = history.plan(snapshot, extracted)
    result = history.apply(db_session, snapshot, extracted, decision)
    assert result == {"status": "applied", "changed": True, "pending": False, "archived": 2}
    assert prov.read_field(paper, "authors") == ["Tomislav Matić"]
    assert current.value == ["Tomislav Matic"] and current.decision_at is None
    assert source.raw == original_raw
    assert candidate.decided_by == prov.DECIDED_QUALITY_REJECTED
    assert prov.conflict_page(db_session)[1] == 0
    count = db_session.scalar(select(func.count()).select_from(MetadataQualityDecision))
    assert history.apply(db_session, snapshot, extracted, decision)["status"] == "already_applied"
    assert db_session.scalar(select(func.count()).select_from(MetadataQualityDecision)) == count


@pytest.mark.parametrize("change", ["human_current", "human_candidate", "changed_pdf", "changed_value", "different_rules"])
def test_stale_or_human_protected_previews_cannot_apply(db_session, change):
    paper, current, candidate, file, snapshot, extracted = fixture(db_session)
    decision = history.plan(snapshot, extracted)
    if change == "human_current":
        prov.record_human_decision(current)
    elif change == "human_candidate":
        prov.record_human_decision(candidate, prov.DECIDED_DISMISSED)
    elif change == "changed_pdf":
        file.sha256 = "b" * 64
    elif change == "changed_value":
        current.value = ["Someone Else"]
    else:
        extracted["quality"]["rule_version"] = "other-version"
    db_session.flush()
    with pytest.raises(ValueError):
        history.apply(db_session, snapshot, extracted, decision)
    assert prov.read_field(paper, "authors") == ["Tomislav Matic"]


def test_unsafe_body_retains_ambiguous_claims_with_evidence(db_session):
    paper, _, candidate, _, snapshot, extracted = fixture(db_session)
    extracted["quality"]["byline_complete"] = False
    decision = history.plan(snapshot, extracted)
    assert not decision["changed"] and decision["pending"]
    history.apply(db_session, snapshot, extracted, decision)
    assert candidate.decided_by == "initial"
    assert prov.conflict_page(db_session)[1] == 1
    current = prov.current_claim(db_session, paper.id, "authors")
    assert db_session.get(PaperSource, current.source_id).raw["quality"] == extracted["quality"]


def test_unverified_evidence_does_not_overwrite_materialized_spelling_drift(db_session):
    from app.services import paper_service

    paper, _, _, _, _, extracted = fixture(db_session)
    paper_service.set_paper_authors(db_session, paper, ["Tomislav Matić"])
    snapshot = history.snapshots(db_session, [{"paper_id": paper.id, "field": "authors"}])[0]
    extracted["quality"]["byline_complete"] = False
    decision = history.plan(snapshot, extracted)
    assert decision["selected"] == ["Tomislav Matić"] and not decision["changed"]
    assert decision["pending"]
    history.apply(db_session, snapshot, extracted, decision)
    assert prov.read_field(paper, "authors") == ["Tomislav Matić"]


@pytest.mark.parametrize("before,after", [(["Y. Arai"], ["Y. Ami"]), (["Kwyro Lee"], ["Kwyro Lee'"]),
                                        (["Gabor C. Temes"], ["Gabor. C. Temes"])])
def test_history_does_not_introduce_changed_letters_or_suspect_markers(db_session, before, after):
    paper, _, _, _, snapshot, extracted = fixture(db_session, kept=before, rejected=after)
    decision = history.plan(snapshot, extracted)
    assert decision["selected"] == before and not decision["changed"] and decision["pending"]
    history.apply(db_session, snapshot, extracted, decision)
    assert prov.read_field(paper, "authors") == before


def test_author_edits_enqueue_transactional_metadata_sync(db_session):
    paper, _, _, _, snapshot, extracted = fixture(db_session)
    db_session.add(PaperChunk(id=new_uuid(), paper_id=paper.id, chunk_index=0, text="Keep this evidence"))
    db_session.commit()
    history.apply(db_session, snapshot, extracted, history.plan(snapshot, extracted))
    assert db_session.get(MetadataIndexUpdate, paper.id) is not None
    assert db_session.scalar(select(PaperChunk.text)) == "Keep this evidence"
    db_session.rollback()
    assert prov.read_field(paper, "authors") == ["Tomislav Matic"]
    assert db_session.get(MetadataIndexUpdate, paper.id) is None


def test_pdf_checksum_mismatch_is_rejected_before_parsing(db_session):
    *_, snapshot, _ = fixture(db_session)
    with pytest.raises(ValueError, match="checksum"):
        history.extract(snapshot, download=lambda *a, **k: b"different PDF")