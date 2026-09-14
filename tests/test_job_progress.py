"""Stage progress must be visible to other sessions (SPEC-P1 section A1).

The worker used to set ``PARSING``/``CHUNKING``/``EMBEDDING``/``INDEXING`` and
only ``flush()``, so a concurrent ``GET /api/jobs/{job_id}`` -- a *different*
session, hence a different transaction -- could only ever observe ``RECEIVED``
and the final ``COMPLETED``/``FAILED``.

These tests run the real pipeline against a private in-memory SQLite database
(no PostgreSQL, no OpenSearch, no MinIO, no embedding server): every external
collaborator is monkeypatched, while the job bookkeeping itself goes through the
production code path.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest
import sqlalchemy
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import (
    Author,
    Base,
    IngestionJob,
    Paper,
    PaperAuthor,
    PaperChunk,
    PaperFile,
    new_uuid,
)
from app.parsing.chunking import Chunk
from app.workers import tasks


# --------------------------------------------------------------------------- #
# fixtures: a private SQLite database + monkeypatched collaborators
# --------------------------------------------------------------------------- #
@pytest.fixture()
def factory():
    """A session factory bound to a private in-memory SQLite database."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    # PostgreSQL-only DDL (JSONB / UUID / partial indexes) is out of scope here:
    # only the tables the pipeline touches are created, with the JSONB and UUID
    # column types mapped onto their portable generic counterparts.
    from sqlalchemy.dialects.postgresql import JSONB, UUID
    from sqlalchemy.types import JSON

    dialect_types = {JSONB: JSON(), UUID: sqlalchemy.String(36)}
    tables = (
        Author.__table__,
        Paper.__table__,
        PaperAuthor.__table__,
        PaperChunk.__table__,
        PaperFile.__table__,
        IngestionJob.__table__,
    )
    for table in tables:
        columns = [c._copy() for c in table.columns]
        for column in columns:
            for source, replacement in dialect_types.items():
                if isinstance(column.type, source):
                    column.type = replacement
        meta = sqlalchemy.MetaData()
        sqlalchemy.Table(table.name, meta, *columns)
        meta.create_all(engine)

    session_factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    try:
        yield session_factory
    finally:
        engine.dispose()


def make_paper(session_factory, *, paper_id: str | None = None):
    """Insert one PENDING paper plus its stored original file record."""
    session = session_factory()
    try:
        paper = Paper(
            id=paper_id or new_uuid(),
            title="A Paper",
            fingerprint=f"sha256:{new_uuid()}",
            status="PENDING",
        )
        session.add(paper)
        session.add(
            PaperFile(
                id=new_uuid(),
                paper_id=paper.id,
                kind="original",
                object_key=f"papers/{paper.id}/original.pdf",
                bucket="paperbox",
                filename="original.pdf",
                content_type="application/pdf",
                size_bytes=10,
            )
        )
        session.commit()
        return paper.id
    finally:
        session.close()


def make_job(session_factory, paper_id: str | None = None) -> str:
    """Insert one RECEIVED job row directly, as ``create_job`` would."""
    session = session_factory()
    try:
        job = IngestionJob(
            id=new_uuid(),
            paper_id=paper_id,
            kind="ingest",
            stage="RECEIVED",
            progress=0.0,
            payload={"source_type": "file", "object_key": "papers/x/original.pdf"},
            started_at=datetime.now(timezone.utc),
        )
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


@pytest.fixture()
def stubbed_pipeline(monkeypatch):
    """Patch every external collaborator of ``_run_pipeline``.

    ``state["blocking_call"]`` lets a test park the pipeline at a chosen point
    so a second session can read the row while the worker is still running.
    """
    state = {"blocking_call": None}

    monkeypatch.setattr(
        tasks.object_storage,
        "download_bytes",
        lambda key: b"%PDF-1.4 fake",
    )
    monkeypatch.setattr(tasks, "extract_pages", lambda data: ["page one text"])
    monkeypatch.setattr(tasks, "detect_sections", lambda pages: ["body"])
    monkeypatch.setattr(tasks, "merge_short_sections", lambda sections: sections)
    monkeypatch.setattr(
        tasks, "_backfill_metadata", lambda session, paper, pages, data=None: None
    )
    monkeypatch.setattr(
        tasks,
        "chunk_document",
        lambda pages, sections: [
            Chunk(
                chunk_index=0,
                text="some chunk text",
                page_start=1,
                page_end=1,
                section="body",
                section_title="body",
                token_count=3,
                char_count=15,
            )
        ],
    )

    def _replace_chunks(session, paper, chunks):
        row = PaperChunk(
            id=new_uuid(),
            paper_id=paper.id,
            chunk_index=0,
            page_start=1,
            page_end=1,
            section="body",
            text="some chunk text",
            token_count=3,
            char_count=15,
        )
        session.add(row)
        session.flush()
        return [row]

    monkeypatch.setattr(tasks, "_replace_chunks", _replace_chunks)
    monkeypatch.setattr(
        tasks, "_write_embeddings", lambda session, paper, rows, vectors, now: None
    )
    monkeypatch.setattr(
        tasks.opensearch, "ensure_index", lambda *a, **kw: {"acknowledged": True}
    )
    monkeypatch.setattr(tasks.opensearch, "delete_by_paper_id", lambda *a, **kw: None)
    monkeypatch.setattr(
        tasks.opensearch,
        "bulk_index_chunks",
        lambda *a, **kw: {"indexed": 1, "failed": 0},
    )
    monkeypatch.setattr(tasks, "_mark_indexed", lambda session, paper, rows, now: None)
    # ``_index_rows`` walks paper->authors/venue/tags through lazy relationship
    # loads; the SQLite fixture only creates the tables the pipeline itself
    # writes, so the document builder is stubbed as well.
    monkeypatch.setattr(tasks, "_index_rows", lambda paper, rows, vectors: [])

    def _embed_texts(texts, *args, **kwargs):
        call = state["blocking_call"]
        if call is not None:
            call(texts)
        return [[0.1] * tasks.settings.embedding_dimension for _ in texts]

    monkeypatch.setattr(tasks.embedding_service, "embed_texts", _embed_texts)
    return state


def read_stage(session_factory, job_id: str) -> tuple[str, float]:
    """Read ``(stage, progress)`` through a fresh session, as the API would."""
    session = session_factory()
    try:
        row = session.execute(
            select(IngestionJob.stage, IngestionJob.progress).where(
                IngestionJob.id == job_id
            )
        ).one()
        return str(row[0]), float(row[1])
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_running_pipeline_exposes_intermediate_stages(factory, stubbed_pipeline):
    """While the worker is mid-pipeline, another session sees real progress.

    Every observation is made from a *different* SQLAlchemy session (and a
    different connection) than the worker thread, exactly like the API: the
    ``stage_commit_hook`` fires right after each ``commit()``, and the assertion
    re-reads the row through that independent session. Before the fix (flush
    only) these reads returned ``RECEIVED`` until the very end.
    """
    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)

    release = threading.Event()
    observed: list[tuple[str, float]] = []
    reads: list[tuple[str, float]] = []

    def observe(_job_id, stage, progress):
        # Independent session: only committed data is visible here.
        reads.append(read_stage(factory, job_id))
        observed.append((stage, progress))

    def blocking_index_rows(paper_, rows_, vectors_):
        assert release.wait(timeout=10), "test never released the pipeline"
        return []

    def blocking_embeddings(_texts):
        return [[0.1] * tasks.settings.embedding_dimension]

    stubbed_pipeline["blocking_call"] = blocking_embeddings

    def worker() -> None:
        worker_session = factory()
        try:
            job = worker_session.get(IngestionJob, job_id)
            paper = worker_session.get(Paper, paper_id)
            tasks._run_pipeline(worker_session, job, paper, "papers/x/original.pdf")
        finally:
            worker_session.close()

    tasks.stage_commit_hook = observe
    try:
        with pytest.MonkeyPatch.context() as patched:
            # Park the worker after INDEXING is committed, so polling happens
            # while the pipeline is genuinely still running.
            patched.setattr(tasks, "_index_rows", blocking_index_rows)
            patched.setattr(
                tasks.opensearch,
                "ensure_index",
                lambda *a, **kw: {"acknowledged": True},
            )
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if len(observed) >= 4:
                    break
                time.sleep(0.01)

            assert observed == [
                (tasks.STAGE_PARSING, tasks.PROGRESS_PARSING),
                (tasks.STAGE_CHUNKING, tasks.PROGRESS_CHUNKING),
                (tasks.STAGE_EMBEDDING, tasks.PROGRESS_EMBEDDING),
                (tasks.STAGE_INDEXING, tasks.PROGRESS_INDEXING),
            ], observed
            # The independent session read the same intermediate values, while
            # the worker was still parked before the index call.
            assert reads == observed, f"other session saw {reads}"

            release.set()
            thread.join(timeout=10)
            assert not thread.is_alive()
    finally:
        tasks.stage_commit_hook = None

    assert read_stage(factory, job_id) == ("COMPLETED", 100.0)


def test_stage_transitions_reach_the_log_as_commits(factory, stubbed_pipeline):
    """Regression guard: every stage boundary is a real ``COMMIT``.

    This inspects the engine's own log: the pipeline must emit a ``COMMIT``
    after each of the four stage transitions. With the old ``flush``-only code
    the same run produced a single trailing commit, which is why a polling
    session stayed on ``RECEIVED`` for the whole pipeline.
    """
    import logging

    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)

    statements: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            statements.append(str(record.getMessage()))

    engine_logger = logging.getLogger("sqlalchemy.engine.Engine")
    handler = _Collector()
    engine_logger.addHandler(handler)
    previous_level = engine_logger.level
    engine_logger.setLevel(logging.INFO)

    observed: list[tuple[str, float]] = []
    tasks.stage_commit_hook = lambda job_id_, stage, progress: observed.append(
        (stage, progress)
    )
    try:
        worker_session = factory()
        job = worker_session.get(IngestionJob, job_id)
        paper = worker_session.get(Paper, paper_id)
        tasks._run_pipeline(worker_session, job, paper, "papers/x/original.pdf")
        worker_session.close()
    finally:
        tasks.stage_commit_hook = None
        engine_logger.removeHandler(handler)
        engine_logger.setLevel(previous_level)

    assert [stage for stage, _ in observed] == [
        "PARSING",
        "CHUNKING",
        "EMBEDDING",
        "INDEXING",
    ]

    updates = [
        statement
        for statement in statements
        if statement.startswith("UPDATE ingestion_jobs")
    ]
    commits = [
        statement for statement in statements if statement.strip() == "COMMIT"
    ]
    assert len(updates) >= 4, f"expected one job UPDATE per stage, got {updates}"
    assert len(commits) >= 4, f"expected a COMMIT per stage boundary, got {commits}"


def test_every_stage_transition_is_committed(factory, stubbed_pipeline):
    """Each stage the pipeline passes through is committed, not just flushed."""
    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)

    stages: list[tuple[str, float]] = []

    def spy(_job_id, stage, progress):
        # Read through an independent session *at the moment of transition*.
        stages.append(read_stage(factory, job_id))
        assert stages[-1] == (stage, progress), "transition is not visible yet"

    session = factory()
    job = session.get(IngestionJob, job_id)
    paper = session.get(Paper, paper_id)

    tasks.stage_commit_hook = spy
    try:
        tasks._run_pipeline(session, job, paper, "papers/x/original.pdf")
    finally:
        tasks.stage_commit_hook = None
        session.close()

    assert [stage for stage, _ in stages] == [
        "PARSING",
        "CHUNKING",
        "EMBEDDING",
        "INDEXING",
    ]
    assert read_stage(factory, job_id) == ("COMPLETED", 100.0)


def test_failure_keeps_the_stage_it_failed_at(factory, stubbed_pipeline):
    """A mid-pipeline failure leaves the failing stage/progress readable."""
    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)

    def explode(_texts):
        raise RuntimeError("embedding server down")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(tasks.embedding_service, "embed_texts", explode)
        session = factory()
        job = session.get(IngestionJob, job_id)
        paper = session.get(Paper, paper_id)
        with pytest.raises(RuntimeError):
            tasks._run_pipeline(session, job, paper, "papers/x/original.pdf")
        session.rollback()
        tasks._record_failure(session, job_id, RuntimeError("embedding server down"))
        session.close()

    stage, progress = read_stage(factory, job_id)
    assert stage == "FAILED"
    assert progress == tasks.PROGRESS_EMBEDDING

    session = factory()
    try:
        row = session.get(IngestionJob, job_id)
        assert row.error_message and "embedding server down" in row.error_message
        assert row.finished_at is not None
        assert session.get(Paper, paper_id).status == "FAILED"
    finally:
        session.close()


def test_serialize_job_exposes_the_progress_fields(factory):
    """``JobOut`` carries everything the polling client needs (SPEC-P1 A1)."""
    from app.schemas.job import JobOut
    from app.services.ingestion_service import serialize_job

    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)

    session = factory()
    try:
        payload = serialize_job(session.get(IngestionJob, job_id))
    finally:
        session.close()

    for field in (
        "stage",
        "progress",
        "paper_id",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
        "finished_at",
    ):
        assert field in payload, f"serialize_job misses {field}"
    assert payload["error_code"] is None

    out = JobOut.model_validate(payload)
    assert out.stage == "RECEIVED"
    assert out.progress == 0.0
    assert out.paper_id == paper_id
    assert out.error_code is None
    assert out.finished_at is None

    assert {
        "stage",
        "progress",
        "paper_id",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
        "finished_at",
    } <= set(JobOut.model_fields)
