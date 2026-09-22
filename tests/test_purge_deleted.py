"""``scripts/purge_deleted.py``: soft purge by default, ``--hard`` on request.

The default purge only reconciles OpenSearch and MinIO and deliberately keeps the
PostgreSQL rows (soft delete). ``--hard`` is the explicit "remove this paper's
data" path: every child table plus the ``papers`` row. Both paths run here against
in-memory SQLite with stubbed OpenSearch/MinIO calls.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

from app.db.models import (
    Author,
    IngestionJob,
    Paper,
    PaperAuthor,
    PaperChunk,
    PaperFieldProvenance,
    PaperFile,
    PaperIdentifier,
    PaperSource,
    PaperTag,
    PapersTag,
    new_uuid,
)
from scripts import purge_deleted
from tests.test_job_progress import factory  # noqa: F401 - fixture

#: Every table ``--hard`` has to clean, keyed by model.
HARD_TABLES = purge_deleted.HARD_PURGE_MODELS


def make_deleted_paper(session_factory) -> str:
    """One soft-deleted paper with a row in every table ``--hard`` touches."""
    session = session_factory()
    try:
        paper = Paper(
            id=new_uuid(),
            title="Deleted Paper",
            fingerprint=f"sha256:{new_uuid()}",
            status="DELETED",
            deleted_at=datetime.now(timezone.utc),
        )
        author = Author(
            id=new_uuid(), name="Alice", normalized_name=f"alice {new_uuid()[:8]}"
        )
        tag = PaperTag(id=new_uuid(), name="sram", normalized_name=f"sram {new_uuid()[:8]}")
        session.add_all([paper, author, tag])
        session.flush()
        session.add_all(
            [
                PaperChunk(
                    id=new_uuid(),
                    paper_id=paper.id,
                    chunk_index=0,
                    page_start=1,
                    page_end=1,
                    section="body",
                    text="text",
                    token_count=1,
                    char_count=4,
                ),
                PaperFile(
                    id=new_uuid(),
                    paper_id=paper.id,
                    kind="original",
                    object_key=f"papers/{paper.id}/original.pdf",
                    bucket="paperbox",
                    filename="original.pdf",
                    content_type="application/pdf",
                    size_bytes=10,
                ),
                PaperIdentifier(
                    id=new_uuid(),
                    paper_id=paper.id,
                    scheme="doi",
                    value="10.1/x",
                    normalized_value="10.1/x",
                ),
                PaperFieldProvenance(
                    id=new_uuid(),
                    paper_id=paper.id,
                    field="title",
                    value="Deleted Paper",
                    is_current=True,
                    decided_by="initial",
                    decided_at=datetime.now(timezone.utc),
                ),
                PaperSource(
                    id=new_uuid(),
                    paper_id=paper.id,
                    source_type="import_file",
                    source_ref="rec-1",
                    raw={},
                    match_status="matched",
                ),
                PaperAuthor(
                    id=new_uuid(), paper_id=paper.id, author_id=author.id, author_order=0
                ),
                PapersTag(
                    id=new_uuid(), paper_id=paper.id, tag_id=tag.id, kind="source_tag"
                ),
                IngestionJob(
                    id=new_uuid(),
                    paper_id=paper.id,
                    kind="ingest",
                    stage="DONE",
                    progress=1.0,
                    payload={"source_type": "file"},
                    started_at=datetime.now(timezone.utc),
                ),
            ]
        )
        session.commit()
        return paper.id
    finally:
        session.close()


def count_rows(session_factory, paper_id: str) -> dict[str, int]:
    session = session_factory()
    try:
        counts = {
            model.__tablename__: session.query(model)
            .filter(model.paper_id == paper_id)
            .count()
            for model in HARD_TABLES
        }
        counts["papers"] = (
            session.query(Paper).filter(Paper.id == paper_id).count()
        )
        return counts
    finally:
        session.close()


@pytest.fixture
def stubbed_stores(monkeypatch, factory):  # noqa: F811
    """Point the script at SQLite and stub the OpenSearch/MinIO calls."""
    monkeypatch.setattr(purge_deleted, "SessionLocal", factory)
    monkeypatch.setattr(purge_deleted, "count_index_docs", lambda paper_id: 3)
    monkeypatch.setattr(purge_deleted.opensearch, "delete_by_paper_id", lambda *a, **k: 3)
    monkeypatch.setattr(purge_deleted.object_storage, "delete_prefix", lambda *a, **k: 1)
    monkeypatch.setattr(purge_deleted.object_storage, "list_objects", lambda *a, **k: [])


def run_script(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["purge_deleted.py", *args])
    return purge_deleted.main()


# --------------------------------------------------------------------------- #
# hard_delete_paper
# --------------------------------------------------------------------------- #


def test_hard_delete_removes_every_row_of_the_paper(factory) -> None:  # noqa: F811
    paper_id = make_deleted_paper(factory)
    session = factory()
    try:
        counts = purge_deleted.hard_delete_paper(session, paper_id)
        session.commit()
    finally:
        session.close()

    assert counts["papers"] == 1
    assert counts["paper_chunks"] == 1
    assert counts["paper_files"] == 1
    assert counts["paper_identifiers"] == 1
    assert counts["paper_field_provenance"] == 1
    assert counts["paper_sources"] == 1
    assert counts["paper_authors"] == 1
    assert counts["papers_tags"] == 1
    assert counts["ingestion_jobs"] == 1
    assert set(count_rows(factory, paper_id).values()) == {0}


def test_hard_delete_is_idempotent(factory) -> None:  # noqa: F811
    paper_id = make_deleted_paper(factory)
    session = factory()
    try:
        purge_deleted.hard_delete_paper(session, paper_id)
        session.commit()
        second = purge_deleted.hard_delete_paper(session, paper_id)
        session.commit()
    finally:
        session.close()
    assert set(second.values()) == {0}


def test_hard_delete_leaves_other_papers_alone(factory) -> None:  # noqa: F811
    doomed = make_deleted_paper(factory)
    keeper = make_deleted_paper(factory)
    session = factory()
    try:
        purge_deleted.hard_delete_paper(session, doomed)
        session.commit()
    finally:
        session.close()
    assert count_rows(factory, keeper)["papers"] == 1


def test_format_hard_counts_skips_empty_tables() -> None:
    assert purge_deleted.format_hard_counts({"paper_chunks": 2, "papers": 1}) == (
        "paper_chunks=2 papers=1"
    )
    assert purge_deleted.format_hard_counts({"paper_chunks": 0}) == "no postgres rows"


# --------------------------------------------------------------------------- #
# the script
# --------------------------------------------------------------------------- #


def test_the_default_run_keeps_the_postgres_rows(monkeypatch, factory, stubbed_stores) -> None:  # noqa: F811
    paper_id = make_deleted_paper(factory)
    assert run_script(monkeypatch) == 0
    counts = count_rows(factory, paper_id)
    assert counts["papers"] == 1
    assert counts["paper_chunks"] == 1


def test_the_hard_run_removes_the_postgres_rows(monkeypatch, factory, stubbed_stores) -> None:  # noqa: F811
    paper_id = make_deleted_paper(factory)
    assert run_script(monkeypatch, "--hard") == 0
    assert set(count_rows(factory, paper_id).values()) == {0}


def test_a_dry_run_changes_nothing(monkeypatch, factory, stubbed_stores) -> None:  # noqa: F811
    paper_id = make_deleted_paper(factory)
    assert run_script(monkeypatch, "--hard", "--dry-run") == 0
    assert count_rows(factory, paper_id)["papers"] == 1
    assert count_rows(factory, paper_id)["paper_chunks"] == 1


def test_a_failing_store_does_not_delete_the_postgres_rows(
    monkeypatch, factory, stubbed_stores
) -> None:  # noqa: F811
    """The rows are only removed once both stores answered."""

    def boom(*args, **kwargs):
        raise RuntimeError("opensearch is down")

    monkeypatch.setattr(purge_deleted.opensearch, "delete_by_paper_id", boom)
    paper_id = make_deleted_paper(factory)
    assert run_script(monkeypatch, "--hard") == 1
    assert count_rows(factory, paper_id)["papers"] == 1