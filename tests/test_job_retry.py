"""Manual retry of FAILED ingestion jobs (plan section 22).

The pipeline itself is the one from ``test_job_progress`` (private in-memory
SQLite, every external collaborator monkeypatched); these tests cover the
retry-specific bookkeeping: the atomic FAILED -> RECEIVED claim, the routing
between "resume as reindex" (STORED checkpoint survived) and "re-run the whole
ingestion" (nothing was persisted), and that a successful retry clears the
failure fields and flips the paper back to INDEXED.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.models import IngestionJob, Paper, new_uuid
from app.search import opensearch
from app.services import ingestion_service as ingest
from app.services import paper_service
from app.workers import tasks
from tests.test_job_progress import (  # noqa: F401  (pytest fixtures)
    factory,
    make_job,
    make_paper,
    read_stage,
    stubbed_pipeline,
)


def _fail_job(session_factory, job_id, *, code="EMBEDDING_FAILED", progress=80.0):
    """Mark a job FAILED through the production path (``mark_failed``)."""
    session = session_factory()
    try:
        job = session.get(IngestionJob, job_id)
        if progress is not None:
            job.progress = progress
        ingest.mark_failed(session, job, "embedding server down", code=code)
        if job.paper_id:
            paper = session.get(Paper, job.paper_id)
            paper.status = paper_service.STATUS_FAILED
        session.commit()
        return job.id
    finally:
        session.close()


def _get_job_row(session_factory, job_id) -> IngestionJob:
    session = session_factory()
    try:
        return session.get(IngestionJob, job_id)
    finally:
        session.close()


def _get_paper_status(session_factory, paper_id) -> str:
    session = session_factory()
    try:
        return str(session.get(Paper, paper_id).status)
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# prepare_retry: the atomic FAILED -> RECEIVED claim
# --------------------------------------------------------------------------- #
def test_prepare_retry_resets_the_failure_fields(factory):
    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)
    _fail_job(factory, job_id)

    job = ingest.prepare_retry(factory(), job_id)

    assert job is not None
    assert (job.stage, job.progress) == ("RECEIVED", 0.0)
    assert job.error_code is None
    assert job.error_message is None
    assert job.finished_at is None
    assert job.started_at is not None


def test_prepare_retry_claims_a_job_only_once(factory):
    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)
    _fail_job(factory, job_id)

    assert ingest.prepare_retry(factory(), job_id) is not None
    # The guard is the stage itself: the job is no longer FAILED, so a second
    # trigger (double click, concurrent caller) must not start a second worker.
    assert ingest.prepare_retry(factory(), job_id) is None


@pytest.mark.parametrize("stage", ["RECEIVED", "DOWNLOADING", "COMPLETED"])
def test_prepare_retry_rejects_jobs_that_are_not_failed(factory, stage):
    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)
    session = factory()
    try:
        session.get(IngestionJob, job_id).stage = stage
        session.commit()
    finally:
        session.close()

    assert ingest.prepare_retry(factory(), job_id) is None


def test_prepare_retry_rejects_unknown_jobs(factory):
    assert ingest.prepare_retry(factory(), "no-such-job") is None


# --------------------------------------------------------------------------- #
# run_retry_job: routing + end-to-end resume
# --------------------------------------------------------------------------- #
def test_retry_resumes_pipeline_for_a_stored_paper(factory, stubbed_pipeline, monkeypatch):
    """STORED checkpoint survived -> retry re-runs PARSING -> INDEXING."""
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)
    _fail_job(factory, job_id)

    assert ingest.prepare_retry(factory(), job_id) is not None
    tasks.run_retry_job(job_id)

    stage, progress = read_stage(factory, job_id)
    assert (stage, progress) == ("COMPLETED", 100.0)
    row = _get_job_row(factory, job_id)
    assert row.error_code is None
    assert row.error_message is None
    assert row.finished_at is not None
    assert _get_paper_status(factory, paper_id) == paper_service.STATUS_INDEXED


def test_retry_of_a_failing_pipeline_fails_again(factory, stubbed_pipeline, monkeypatch):
    """A retry that dies keeps the FAILED bookkeeping current (fresh code)."""
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)
    _fail_job(factory, job_id, code="INTERNAL")

    def explode(_session, _paper, chunks):
        raise opensearch.SearchIndexError("index still down")

    monkeypatch.setattr(tasks, "_replace_chunks", explode)
    assert ingest.prepare_retry(factory(), job_id) is not None
    tasks.run_retry_job(job_id)

    stage, progress = read_stage(factory, job_id)
    assert stage == "FAILED"
    row = _get_job_row(factory, job_id)
    assert row.error_code == "INDEX_FAILED"
    assert "index still down" in row.error_message
    assert _get_paper_status(factory, paper_id) == paper_service.STATUS_FAILED


def test_retry_reingests_when_nothing_was_stored(factory, stubbed_pipeline, monkeypatch):
    """Failed before STORED -> the download/store transaction is redone."""
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    job_id = make_job(factory)  # no paper: payload has object_key, no paper_id
    _fail_job(factory, job_id, progress=10.0)
    monkeypatch.setattr(
        tasks.object_storage,
        "upload_bytes",
        lambda key, data, **kwargs: SimpleNamespace(
            object_key=key, bucket="paperbox", size_bytes=len(data)
        ),
    )

    assert ingest.prepare_retry(factory(), job_id) is not None
    tasks.run_retry_job(job_id)

    stage, progress = read_stage(factory, job_id)
    assert (stage, progress) == ("COMPLETED", 100.0)
    row = _get_job_row(factory, job_id)
    assert row.paper_id is not None
    assert _get_paper_status(factory, row.paper_id) == paper_service.STATUS_INDEXED


def test_retry_of_a_vanished_job_is_a_no_op(factory, stubbed_pipeline, monkeypatch):
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    tasks.run_retry_job("no-such-job")  # must neither raise nor touch anything
