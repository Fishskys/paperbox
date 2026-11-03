"""PDF quality policy is consistent across merge, review, history and index retry."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.db.models import MetadataIndexUpdate, MetadataQualityDecision, Paper, PaperChunk, PaperFieldProvenance, new_uuid
from app.services import metadata_index_sync as sync, metadata_merge as merge, metadata_policy as policy
from app.services import metadata_manual, provenance_service as prov
from tests.test_metadata_merge import make_paper, make_source, seed


def context(names):
    return {"byline": names, "byline_complete": True, "byline_origin": "body:geometry", "text_state": "ok"}


@pytest.mark.parametrize("value", ["IEEE Journal;2024;1;2;10.1234/example", "Instructions for Paper Preparation"])
def test_invalid_pdf_abstract_is_archived_even_when_current_is_blank(db_session, value):
    paper = make_paper(db_session)
    result = merge.merge_values(db_session, paper, {"abstract": value}, source_type="pdf_embedded")
    assert result.decisions[0].action == merge.ACTION_ARCHIVED
    assert paper.abstract is None
    history = prov.field_history(db_session, paper.id, "abstract")
    assert history[0].value == value and history[0].decided_by == prov.DECIDED_QUALITY_REJECTED
    assert db_session.scalar(select(func.count()).select_from(MetadataQualityDecision)) == 1


def test_repeated_rejection_is_idempotent_and_auditable(db_session):
    paper = make_paper(db_session)
    seed(db_session, paper, "abstract", "This paper presents a TDC.", "pdf_heuristic")
    source = make_source(db_session, paper.id, "pdf_embedded")
    for _ in range(3):
        merge.merge_values(db_session, paper, {"abstract": "IEEE Journal;2024;1;2;10.1234/example"},
                           source_type="pdf_embedded", source_id=source.id)
    assert len(prov.field_history(db_session, paper.id, "abstract")) == 2
    rows = metadata_manual.metadata_view(db_session, paper)["quality_decisions"]
    assert len(rows) == 1
    assert rows[0]["before"] == rows[0]["selected"] == paper.abstract
    assert rows[0]["rule_version"] == policy.QUALITY_VERSION and rows[0]["actor"] == "metadata_quality"
    assert prov.conflict_page(db_session)[1] == 0


def test_rejected_candidate_does_not_repeat_after_a_later_layer_fills_authors(db_session):
    paper = make_paper(db_session)
    source = make_source(db_session, paper.id, "pdf_embedded")
    for _ in range(2):
        merge.merge_values(db_session, paper, {"authors": ["user123"]}, source_type="pdf_embedded", source_id=source.id)
        merge.merge_values(db_session, paper, {"authors": ["Alice Smith"]}, source_type="pdf_heuristic")
    rows = db_session.scalars(select(MetadataQualityDecision).where(MetadataQualityDecision.field == "authors")).all()
    assert len(rows) == 2
    assert sum(row.payload["scope"] == "candidate" for row in rows) == 1


def test_corroborated_body_authors_correct_more_authoritative_pdf_values(db_session):
    paper = make_paper(db_session)
    old = seed(db_session, paper, "authors", ["Someone Else"], "pdf_embedded")
    source = make_source(db_session, paper.id, "pdf_heuristic")
    source.raw = {"quality": context(["Alice Smith", "Bob Jones"])}
    result = merge.merge_values(db_session, paper, {"authors": ["Alice Smith", "Bob Jones"]},
                               source_type="pdf_heuristic", source_id=source.id)
    assert result.decisions[0].action == merge.ACTION_OVERRIDDEN
    assert prov.read_field(paper, "authors") == ["Alice Smith", "Bob Jones"]
    assert old.value == ["Someone Else"] and old.decision_at is None
    assert prov.conflict_page(db_session)[1] == 0


def test_incomplete_embedded_author_list_cannot_replace_a_verified_byline(db_session):
    paper = make_paper(db_session)
    names = ["Alice Smith", "Bob Jones"]
    seed(db_session, paper, "authors", names, "pdf_heuristic")
    result = merge.merge_values(db_session, paper, {"authors": ["Alice Smith"]}, source_type="pdf_embedded", evidence=context(names))
    assert result.decisions[0].action == merge.ACTION_ARCHIVED
    assert prov.read_field(paper, "authors") == names


@pytest.mark.parametrize("state", ["ok", "missing_text", "damaged_text", "overlapping_text"])
def test_valid_but_unverified_author_differences_are_history_only(db_session, state):
    paper = make_paper(db_session)
    seed(db_session, paper, "authors", ["Alice Smith"], "pdf_embedded")
    source = make_source(db_session, paper.id, "pdf_heuristic")
    source.raw = {"quality": {"text_state": state}}
    result = merge.merge_values(db_session, paper, {"authors": ["Bob Jones"]}, source_type="pdf_heuristic", source_id=source.id)
    assert result.decisions[0].action == merge.ACTION_ARCHIVED
    assert prov.read_field(paper, "authors") == ["Alice Smith"]
    assert prov.conflict_page(db_session)[1] == 0


def test_manual_decision_and_dismissed_candidate_survive_rule_upgrades(db_session, monkeypatch):
    paper = make_paper(db_session)
    current = seed(db_session, paper, "authors", ["Alice Smith"], "pdf_embedded")
    prov.record_human_decision(current)
    verified = context(["Bob Jones"])
    result = merge.merge_values(db_session, paper, {"authors": ["Bob Jones"]}, source_type="pdf_heuristic", evidence=verified)
    assert result.decisions[0].action == merge.ACTION_ARCHIVED
    assert prov.read_field(paper, "authors") == ["Alice Smith"]
    rejected = prov.field_history(db_session, paper.id, "authors")[0]
    if rejected.is_current:
        rejected = prov.field_history(db_session, paper.id, "authors")[1]
    prov.record_human_decision(rejected, prov.DECIDED_DISMISSED)
    monkeypatch.setattr(policy, "QUALITY_VERSION", "pdf-fields-next")
    current.decision_at = None
    merge.merge_values(db_session, paper, {"authors": ["Bob Jones"]}, source_type="pdf_heuristic", evidence=verified)
    assert prov.read_field(paper, "authors") == ["Alice Smith"]
    assert rejected.decided_by == prov.DECIDED_DISMISSED


def test_unverified_body_authors_stay_in_history_with_no_current_authors(db_session):
    paper = make_paper(db_session)
    source = make_source(db_session, paper.id, "pdf_heuristic")
    source.raw = {"quality": {"text_state": "ok", "byline_complete": False}}
    result = merge.merge_values(db_session, paper, {"authors": ["Alice Smith"]}, source_type="pdf_heuristic", source_id=source.id)
    assert result.decisions[0].action == merge.ACTION_ARCHIVED
    assert not prov.read_field(paper, "authors")
    assert prov.conflict_page(db_session)[1] == 0


def test_changed_rules_do_not_reopen_uncertain_pdf_candidates(db_session, monkeypatch):
    paper = make_paper(db_session)
    seed(db_session, paper, "authors", ["Alice Smith"], "pdf_heuristic")
    source = make_source(db_session, paper.id, "pdf_embedded")
    source.raw = {"quality": context(["Alice Smith"])}
    merge.merge_values(db_session, paper, {"authors": ["Bob Jones"]}, source_type="pdf_embedded", source_id=source.id)
    assert prov.conflict_page(db_session)[1] == 0
    monkeypatch.setattr(policy, "QUALITY_VERSION", "pdf-fields-next")
    source.raw = {"quality": {}}
    result = merge.merge_values(db_session, paper, {"authors": ["Bob Jones"]}, source_type="pdf_embedded", source_id=source.id)
    assert result.decisions[0].action == merge.ACTION_ARCHIVED
    assert prov.conflict_page(db_session)[1] == 0


def test_review_keeps_quality_decisions_and_counts_across_candidate_batches(db_session):
    paper = make_paper(db_session)
    seed(db_session, paper, "authors", ["Alice Smith"], "pdf_heuristic")
    verified = make_source(db_session, paper.id, "pdf_embedded")
    verified.raw = {"quality": context(["Alice Smith"]), "unused_payload": "x" * 10000}
    db_session.add_all([PaperFieldProvenance(id=new_uuid(), paper_id=paper.id, field="authors",
        source_id=verified.id, value=["Bob Jones"], is_current=False, decided_by="initial",
        decided_at=datetime(2027, 1, 1, tzinfo=timezone.utc)) for _ in range(251)])
    unverified = make_source(db_session, paper.id, "import_file")
    candidate = prov.record_claim(db_session, paper_id=paper.id, field="authors",
                                  value=["Carol White"], source_id=unverified.id)
    db_session.flush()
    rows, total = prov.conflict_page(db_session, limit=1)
    assert total == 1 and rows[0]["provenance_id"] == candidate.id
    assert "current_source_id" not in rows[0] and "quality" not in rows[0]


@pytest.mark.parametrize("field,current,candidate", [
    ("title", "A converter", "Another converter"),
    ("authors", ["Alice Smith"], ["Bob Jones"]),
    ("abstract", "This paper presents a converter.", "This work evaluates a sensor."),
    ("year", 2023, 2024),
    ("identifier:doi", "10.1234/one", "10.1234/two"),
])
def test_historical_local_candidates_do_not_create_user_tasks(db_session, field, current, candidate):
    paper = make_paper(db_session)
    seed(db_session, paper, field, current, "pdf_embedded")
    source = make_source(db_session, paper.id, "pdf_embedded")
    row = prov.record_claim(db_session, paper_id=paper.id, field=field, value=candidate, source_id=source.id)
    assert prov.conflict_page(db_session)[1] == 0
    assert not row.is_current and row.value == candidate and row.decision_at is None
    assert prov.read_field(paper, field) == current


def test_external_disagreement_still_requires_review_and_overrides_pdf(db_session):
    paper = make_paper(db_session)
    seed(db_session, paper, "authors", ["A. Smith"], "pdf_embedded")
    external = make_source(db_session, paper.id, "import_file")
    report = merge.merge_values(db_session, paper, {"authors": ["Alice Smith"]},
                               source_type="import_file", source_id=external.id)
    assert report.decisions[0].action == merge.ACTION_OVERRIDDEN
    other = make_source(db_session, paper.id, "crossref")
    report = merge.merge_values(db_session, paper, {"authors": ["Bob Jones"]},
                               source_type="crossref", source_id=other.id)
    assert report.decisions[0].action == merge.ACTION_CONFLICT
    assert prov.conflict_page(db_session)[1] == 1


def test_equal_external_confirmation_protects_pdf_value_without_rewriting_it(db_session):
    paper = make_paper(db_session)
    old = seed(db_session, paper, "authors", ["Alice Smith"], "pdf_embedded")
    source = make_source(db_session, paper.id, "import_file")
    for _ in range(2):
        report = merge.merge_values(db_session, paper, {"authors": ["Alice Smith"]},
                                   source_type="import_file", source_id=source.id)
        assert report.decisions[0].action == merge.ACTION_UNCHANGED
    current = prov.current_claim(db_session, paper.id, "authors")
    assert current.source_id == source.id and current.decision_at is None and not old.is_current
    assert len(prov.field_history(db_session, paper.id, "authors")) == 2
    report = merge.merge_values(db_session, paper, {"authors": ["Bob Jones"]},
        source_type="pdf_heuristic", evidence=context(["Bob Jones"]))
    assert report.decisions[0].action == merge.ACTION_ARCHIVED
    assert prov.read_field(paper, "authors") == ["Alice Smith"]


def test_external_confirmation_also_protects_unclaimed_legacy_fields(db_session):
    paper = make_paper(db_session, title="A converter")
    source = make_source(db_session, paper.id, "import_file")
    merge.merge_values(db_session, paper, {"title": paper.title}, source_type="import_file", source_id=source.id)
    current = prov.current_claim(db_session, paper.id, "title")
    assert current.source_id == source.id and current.value == "A converter"


@pytest.mark.parametrize("existing_type", ["pdf_embedded", "import_file"])
@pytest.mark.parametrize("encoded,clean", [
    ("A 22.5&#x2013;27.7-GHz PLL", "A 22.5–27.7-GHz PLL"),
    ("A <i>PLL</i>", "A PLL"),
])
def test_clean_external_title_replaces_equivalent_encoded_text(db_session, existing_type, encoded, clean):
    paper = make_paper(db_session, title=encoded)
    old = seed(db_session, paper, "title", encoded, existing_type)
    source = make_source(db_session, paper.id, "import_file")
    report = merge.merge_values(db_session, paper, {"title": clean},
                               source_type="import_file", source_id=source.id, dry_run=True)
    assert report.decisions[0].action == merge.ACTION_OVERRIDDEN
    assert paper.title == encoded and old.is_current
    merge.merge_values(db_session, paper, {"title": clean}, source_type="import_file", source_id=source.id)
    assert paper.title == clean
    current = prov.current_claim(db_session, paper.id, "title")
    assert current.value == clean and current.source_id == source.id
    assert not old.is_current and old.value == encoded
    assert prov.conflict_page(db_session)[1] == 0
    count = len(prov.field_history(db_session, paper.id, "title"))
    again = merge.merge_values(db_session, paper, {"title": clean}, source_type="import_file", source_id=source.id)
    assert again.decisions[0].action == merge.ACTION_UNCHANGED
    assert len(prov.field_history(db_session, paper.id, "title")) == count


@pytest.mark.parametrize("incoming_type", ["pdf_embedded", "pdf_heuristic", "import_file"])
def test_title_presentation_cleanup_preserves_manual_decisions(db_session, incoming_type):
    encoded = "A 22.5&#x2013;27.7-GHz PLL"
    paper = make_paper(db_session, title=encoded)
    claim = seed(db_session, paper, "title", encoded, "manual")
    claim.decision_at = datetime.now(timezone.utc)
    merge.merge_values(db_session, paper, {"title": "A 22.5–27.7-GHz PLL"}, source_type=incoming_type)
    assert paper.title == encoded and claim.is_current


def test_encoded_external_title_does_not_replace_clean_title(db_session):
    paper = make_paper(db_session, title="A 22.5–27.7-GHz PLL")
    seed(db_session, paper, "title", paper.title, "pdf_embedded")
    merge.merge_values(db_session, paper, {"title": "A 22.5&#x2013;27.7-GHz PLL"}, source_type="import_file")
    assert paper.title == "A 22.5–27.7-GHz PLL"


def test_title_cleanup_preserves_a_previously_dismissed_candidate(db_session):
    paper = make_paper(db_session, title="A PLL")
    rejected = seed(db_session, paper, "title", "A PLL", "import_file")
    seed(db_session, paper, "title", "A <i>PLL</i>", "pdf_embedded")
    rejected.decided_by = prov.DECIDED_DISMISSED
    rejected.decision_at = datetime.now(timezone.utc)
    merge.merge_values(db_session, paper, {"title": "A PLL"}, source_type="import_file")
    assert paper.title == "A <i>PLL</i>"


def test_presentation_cleanup_enqueues_title_snapshot_update(db_session):
    paper = indexed(db_session)
    seed(db_session, paper, "title", "A <i>PLL</i>", "import_file")
    source = make_source(db_session, paper.id, "import_file")
    result = merge.merge_values(db_session, paper, {"title": "A PLL"}, source_type="import_file", source_id=source.id)
    assert [row.field for row in result.applied] == ["title"]
    assert db_session.get(MetadataIndexUpdate, paper.id) is not None


def indexed(session):
    paper = make_paper(session)
    session.add(PaperChunk(id=new_uuid(), paper_id=paper.id, chunk_index=0, text="Original evidence", page_start=1, page_end=1))
    session.flush()
    return paper


def test_outbox_is_transactional_and_retries_index_failure(db_session, monkeypatch):
    from app.search import opensearch

    paper = indexed(db_session)
    merge.merge_values(db_session, paper, {"authors": ["Alice Smith"]}, source_type="import_file")
    db_session.commit()
    original = deepcopy(db_session.scalar(select(PaperChunk)).text)
    monkeypatch.setattr(opensearch, "bulk_update_documents", lambda entries: {"updated": 0, "failed": len(entries)})
    failed = sync.flush_pending(db_session)
    assert failed["pending"] == 1
    row = db_session.get(MetadataIndexUpdate, paper.id)
    assert row.attempts == 1 and row.last_error == "bulk_update_failed"
    calls = []
    def success(entries):
        calls.extend(entries)
        return {"updated": len(entries), "failed": 0}
    monkeypatch.setattr(opensearch, "bulk_update_documents", success)
    assert sync.flush_pending(db_session)["updated"] == 1
    assert db_session.get(MetadataIndexUpdate, paper.id) is None
    assert calls[0]["doc"]["authors"] == ["Alice Smith"]
    assert "embedding" not in calls[0]["doc"] and "text" not in calls[0]["doc"]
    assert db_session.scalar(select(PaperChunk)).text == original


def test_newer_metadata_revision_is_not_lost_after_index_refresh(db_session, monkeypatch):
    from app.search import opensearch

    paper = indexed(db_session)
    prov.set_field(db_session, paper, "title", "First title", override=True)
    db_session.commit()
    def concurrent_edit(entries):
        prov.set_field(db_session, paper, "title", "New title", override=True)
        return {"updated": 1, "failed": 0}
    monkeypatch.setattr(opensearch, "bulk_update_documents", concurrent_edit)
    assert sync.flush_pending(db_session)["pending"] == 1
    assert db_session.get(MetadataIndexUpdate, paper.id) is not None


def test_metadata_rollback_also_rolls_back_the_outbox(db_session):
    paper = indexed(db_session)
    db_session.commit()
    prov.set_field(db_session, paper, "title", "Changed", override=True)
    assert db_session.get(MetadataIndexUpdate, paper.id) is not None
    db_session.rollback()
    assert db_session.get(MetadataIndexUpdate, paper.id) is None
    assert paper.title == "Heuristic title"


def test_byline_preserves_repeated_names_and_per_paper_spelling(db_session):
    from app.services import paper_service

    first, second = make_paper(db_session), make_paper(db_session)
    paper_service.set_paper_authors(db_session, first, ["Alice Smith#"])
    paper_service.set_paper_authors(db_session, second, ["Alice Smith", "Alice Smith", "José Chen", "Jose Chen"])
    assert paper_service.paper_author_names(second) == ["Alice Smith", "Alice Smith", "José Chen", "Jose Chen"]
    assert paper_service.paper_author_names(first) == ["Alice Smith#"]
