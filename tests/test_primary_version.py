"""Primary version rule (decision 14, section 7.1): one PDF per paper is indexed.

``published_pdf > original > arxiv_pdf``; a lower-priority arrival is stored in
MinIO and registered, but never parsed or indexed. Only a *promotion* changes what
the index holds, and that is what the caller turns into a reindex.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.models import Paper, PaperFile, new_uuid
from app.services import paper_service


def make_paper(session, **overrides) -> Paper:
    values = {
        "id": new_uuid(),
        "title": "Low Power SRAM",
        "fingerprint": f"sha256:{new_uuid()}",
        "status": "INDEXED",
    }
    values.update(overrides)
    paper = Paper(**values)
    session.add(paper)
    session.flush()
    return paper


def add_file(session, paper, kind="original", *, minutes=0, sha256=None) -> PaperFile:
    record = PaperFile(
        id=new_uuid(),
        paper_id=paper.id,
        kind=kind,
        object_key=f"papers/{paper.id}/{kind}.pdf",
        bucket="paperbox",
        filename=f"{kind}.pdf",
        content_type="application/pdf",
        size_bytes=10,
        sha256=sha256,
        created_at=datetime.now(timezone.utc) + timedelta(minutes=minutes),
    )
    session.add(record)
    session.flush()
    return record


# --------------------------------------------------------------------------- #
# priority table
# --------------------------------------------------------------------------- #
def test_priority_order_is_published_then_original_then_preprint() -> None:
    assert paper_service.primary_priority("published_pdf") > paper_service.primary_priority(
        "original"
    )
    assert paper_service.primary_priority("original") > paper_service.primary_priority(
        "arxiv_pdf"
    )
    assert paper_service.primary_priority("arxiv_pdf") > paper_service.primary_priority(
        "supplement"
    )
    assert paper_service.primary_priority(None) == 0
    assert paper_service.primary_priority("something_new") == 0


def test_selection_prefers_the_highest_priority_kind(db_session) -> None:
    paper = make_paper(db_session)
    arxiv = add_file(db_session, paper, "arxiv_pdf")
    published = add_file(db_session, paper, "published_pdf", minutes=5)

    assert paper_service.select_primary_file(paper_service.live_files(paper)).id == published.id
    assert arxiv.is_primary is False


def test_selection_keeps_the_first_file_on_a_tie(db_session) -> None:
    paper = make_paper(db_session)
    first = add_file(db_session, paper, "original", minutes=0)
    second = add_file(db_session, paper, "original", minutes=5)

    assert paper_service.select_primary_file(paper_service.live_files(paper)).id == first.id
    assert second.id != first.id


def test_deleted_files_are_not_candidates(db_session) -> None:
    paper = make_paper(db_session)
    published = add_file(db_session, paper, "published_pdf")
    published.deleted_at = datetime.now(timezone.utc)
    arxiv = add_file(db_session, paper, "arxiv_pdf")

    assert paper_service.select_primary_file(paper_service.live_files(paper)).id == arxiv.id


# --------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------- #
def test_the_first_arriving_version_becomes_primary(db_session) -> None:
    paper = make_paper(db_session, status="PENDING")
    record = add_file(db_session, paper, "original")

    outcome = paper_service.apply_primary_selection(db_session, paper, incoming=record)

    assert outcome.action == paper_service.PRIMARY_ACTION_PRIMARY
    assert outcome.indexed is True
    assert outcome.needs_reindex is False
    assert record.is_primary is True


def test_a_better_version_later_promotes_and_asks_for_a_reindex(db_session) -> None:
    paper = make_paper(db_session)
    arxiv = add_file(db_session, paper, "arxiv_pdf")
    paper_service.apply_primary_selection(db_session, paper, incoming=arxiv)
    assert arxiv.is_primary is True

    published = add_file(db_session, paper, "published_pdf", minutes=1)
    outcome = paper_service.apply_primary_selection(db_session, paper, incoming=published)

    assert outcome.action == paper_service.PRIMARY_ACTION_PROMOTED
    assert outcome.needs_reindex is True
    assert published.is_primary is True
    assert arxiv.is_primary is False
    assert outcome.previous.id == arxiv.id


def test_a_worse_version_later_is_only_stored(db_session) -> None:
    paper = make_paper(db_session)
    published = add_file(db_session, paper, "published_pdf")
    paper_service.apply_primary_selection(db_session, paper, incoming=published)

    arxiv = add_file(db_session, paper, "arxiv_pdf", minutes=1)
    outcome = paper_service.apply_primary_selection(db_session, paper, incoming=arxiv)

    assert outcome.action == paper_service.PRIMARY_ACTION_NON_PRIMARY
    assert outcome.indexed is False
    assert outcome.needs_reindex is False
    assert arxiv.is_primary is False
    assert published.is_primary is True


def test_an_equal_priority_newcomer_does_not_displace_the_primary(db_session) -> None:
    paper = make_paper(db_session)
    first = add_file(db_session, paper, "original")
    paper_service.apply_primary_selection(db_session, paper, incoming=first)

    second = add_file(db_session, paper, "original", minutes=1)
    outcome = paper_service.apply_primary_selection(db_session, paper, incoming=second)

    assert outcome.action == paper_service.PRIMARY_ACTION_NON_PRIMARY
    assert first.is_primary is True


def test_selection_without_files_is_a_no_op(db_session) -> None:
    paper = make_paper(db_session)

    outcome = paper_service.apply_primary_selection(db_session, paper)

    assert outcome.action == paper_service.PRIMARY_ACTION_NONE
    assert outcome.primary is None


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def test_original_file_returns_the_primary_version(db_session) -> None:
    paper = make_paper(db_session)
    arxiv = add_file(db_session, paper, "arxiv_pdf")
    published = add_file(db_session, paper, "published_pdf", minutes=1)
    paper_service.apply_primary_selection(db_session, paper, incoming=published)

    assert paper_service.original_file(paper).id == published.id
    assert paper_service.primary_file(paper).id == published.id
    assert arxiv.id != published.id


def test_original_file_falls_back_for_legacy_rows(db_session) -> None:
    """Rows written before ``is_primary`` existed still resolve to their PDF."""
    paper = make_paper(db_session)
    legacy = add_file(db_session, paper, "original")

    assert paper_service.original_file(paper).id == legacy.id
    assert paper_service.primary_file(paper) is None


def test_original_file_is_none_without_files(db_session) -> None:
    paper = make_paper(db_session)

    assert paper_service.original_file(paper) is None
    assert paper_service.primary_file(paper) is None


# --------------------------------------------------------------------------- #
# deletion
# --------------------------------------------------------------------------- #
def test_removing_a_non_primary_file_changes_nothing_else(db_session) -> None:
    paper = make_paper(db_session)
    published = add_file(db_session, paper, "published_pdf")
    arxiv = add_file(db_session, paper, "arxiv_pdf", minutes=1)
    paper_service.apply_primary_selection(db_session, paper, incoming=published)

    outcome = paper_service.remove_file(db_session, paper, arxiv)

    assert outcome.action == paper_service.PRIMARY_ACTION_NONE
    assert outcome.needs_reindex is False
    assert published.is_primary is True
    assert paper.status == "INDEXED"


def test_removing_the_primary_file_promotes_the_next_one(db_session) -> None:
    paper = make_paper(db_session)
    published = add_file(db_session, paper, "published_pdf")
    arxiv = add_file(db_session, paper, "arxiv_pdf", minutes=1)
    paper_service.apply_primary_selection(db_session, paper, incoming=published)

    outcome = paper_service.remove_file(db_session, paper, published)

    assert outcome.action == paper_service.PRIMARY_ACTION_PROMOTED
    assert outcome.needs_reindex is True
    assert outcome.primary.id == arxiv.id
    assert arxiv.is_primary is True
    assert published.deleted_at is not None


def test_removing_the_last_file_fails_the_paper_and_keeps_the_index(db_session) -> None:
    paper = make_paper(db_session)
    only = add_file(db_session, paper, "original")
    paper_service.apply_primary_selection(db_session, paper, incoming=only)

    outcome = paper_service.remove_file(db_session, paper, only)

    assert outcome.action == paper_service.PRIMARY_ACTION_NONE
    assert paper.status == paper_service.STATUS_FAILED
    assert paper_service.primary_file(paper) is None