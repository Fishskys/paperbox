"""The in-process ingestion queue (2026-09-19).

Uploads used to start one pipeline each through FastAPI ``BackgroundTasks``:
ten quick uploads meant ten concurrent parse/embed/index runs against the same
embedding server and OpenSearch. The queue caps that at ``INGEST_CONCURRENCY``
(default 2) and parks the rest in stage ``QUEUED``.

These tests cover the queue mechanics with fake runners (no database, no HTTP)
plus the two database-side helpers through the production code path on a private
in-memory SQLite database (the ``factory`` fixture from ``test_job_progress``).
No pytest-asyncio dependency: the async parts are driven with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest

from app.db.models import IngestionJob, new_uuid
from app.services import ingestion_service as ingest
from app.workers import queue as job_queue
from tests.test_job_progress import factory, make_job, make_paper  # noqa: F401


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class RecordingRunner:
    """Fake pipeline: records order and the observed concurrency."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.started: list[str] = []
        self.finished: list[str] = []
        self.parallel = 0
        self.max_parallel = 0

    def __call__(self, job_id: str) -> None:
        with self.lock:
            self.started.append(job_id)
            self.parallel += 1
            self.max_parallel = max(self.max_parallel, self.parallel)
        time.sleep(self.delay)
        with self.lock:
            self.parallel -= 1
            self.finished.append(job_id)


def run_queue(concurrency: int, runners: dict, job_ids: list[str], kind: str = "ingest"):
    """Start a queue, enqueue ``job_ids``, drain it, stop it."""

    async def _main() -> dict:
        queue = job_queue.IngestQueue(concurrency=concurrency, runners=runners)
        queue.start()
        try:
            for job_id in job_ids:
                queue.enqueue(job_id, kind)
            await queue.join()
            return queue.stats()
        finally:
            await queue.stop()

    return asyncio.run(_main())


# --------------------------------------------------------------------------- #
# concurrency ceiling
# --------------------------------------------------------------------------- #
def test_concurrency_ceiling_is_respected():
    runner = RecordingRunner()
    stats = run_queue(2, {"ingest": runner}, [f"job-{i}" for i in range(6)])

    assert runner.max_parallel == 2
    assert len(runner.finished) == 6
    assert stats["queued"] == 0
    assert stats["running"] == 0


def test_single_slot_serialises_everything():
    runner = RecordingRunner()
    run_queue(1, {"ingest": runner}, ["a", "b", "c"])

    assert runner.max_parallel == 1


def test_jobs_run_in_fifo_order():
    runner = RecordingRunner(delay=0.01)
    run_queue(1, {"ingest": runner}, ["first", "second", "third"])

    assert runner.started == ["first", "second", "third"]


def test_stats_report_waiting_and_running_jobs():
    """While two jobs run, the remaining ones are visible as queued."""

    async def _main() -> dict:
        release = threading.Event()
        runner = RecordingRunner(delay=0.0)

        def blocking(job_id: str) -> None:
            runner(job_id)
            release.wait(2.0)

        queue = job_queue.IngestQueue(concurrency=2, runners={"ingest": blocking})
        queue.start()
        try:
            for job_id in ["j1", "j2", "j3", "j4"]:
                queue.enqueue(job_id)
            # Let the two workers pick up j1/j2 and park on the event.
            await asyncio.sleep(0.3)
            snapshot = queue.stats()
            release.set()
            await queue.join()
            return snapshot
        finally:
            await queue.stop()

    snapshot = asyncio.run(_main())

    assert snapshot["concurrency"] == 2
    assert snapshot["running"] == 2
    assert snapshot["queued"] == 2
    assert sorted(snapshot["running_job_ids"]) == ["j1", "j2"]
    assert sorted(snapshot["queued_job_ids"]) == ["j3", "j4"]


def test_worker_survives_a_failing_job():
    """One exploding pipeline must not kill the worker or the queue."""
    seen: list[str] = []

    def flaky(job_id: str) -> None:
        seen.append(job_id)
        if job_id == "boom":
            raise RuntimeError("pipeline exploded")

    run_queue(1, {"ingest": flaky}, ["boom", "next"])

    assert seen == ["boom", "next"]


# --------------------------------------------------------------------------- #
# admission
# --------------------------------------------------------------------------- #
def test_enqueue_before_start_runs_inline():
    """Without a started queue the pipeline runs on the calling thread."""
    seen: list[str] = []
    queue = job_queue.IngestQueue(concurrency=2, runners={"ingest": seen.append})

    queued = queue.enqueue("solo")

    assert queued is False
    assert seen == ["solo"]


def test_enqueue_rejects_unknown_kind():
    queue = job_queue.IngestQueue(concurrency=1, runners={"ingest": lambda _: None})

    with pytest.raises(ValueError):
        queue.enqueue("job-1", "nonsense")


def test_enqueue_is_idempotent_for_a_queued_job():
    async def _main() -> tuple[bool, bool, dict]:
        release = threading.Event()

        def blocking(job_id: str) -> None:
            release.wait(2.0)

        queue = job_queue.IngestQueue(concurrency=1, runners={"ingest": blocking})
        queue.start()
        try:
            first = queue.enqueue("dup")
            second = queue.enqueue("dup")
            await asyncio.sleep(0.2)
            release.set()
            await queue.join()
            return first, second, queue.stats()
        finally:
            await queue.stop()

    first, second, stats = asyncio.run(_main())

    assert (first, second) == (True, False)
    assert stats["queued"] == 0 and stats["running"] == 0


def test_kind_for_payload_routes_reindex_jobs():
    assert job_queue.kind_for_payload({"source_type": "reindex"}) == job_queue.KIND_REINDEX
    assert job_queue.kind_for_payload({"source_type": "file"}) == job_queue.KIND_INGEST
    assert job_queue.kind_for_payload(None) == job_queue.KIND_INGEST


# --------------------------------------------------------------------------- #
# database-side helpers (mark_queued / recover_jobs)
# --------------------------------------------------------------------------- #
def test_mark_queued_parks_a_received_job(factory):  # noqa: F811
    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)

    session = factory()
    try:
        job = ingest.mark_queued(session, job_id)
        assert job is not None
        assert (job.stage, job.progress) == ("QUEUED", 0.0)
    finally:
        session.close()


def test_mark_queued_refuses_a_job_that_moved_on(factory):  # noqa: F811
    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)
    session = factory()
    try:
        running = session.get(IngestionJob, job_id)
        running.stage = "EMBEDDING"
        running.progress = 80.0
        session.commit()

        assert ingest.mark_queued(session, job_id) is None
        assert session.get(IngestionJob, job_id).stage == "EMBEDDING"
    finally:
        session.close()


def test_recover_requeues_waiting_jobs_and_fails_interrupted_ones(factory):  # noqa: F811
    paper_id = make_paper(factory)
    waiting = make_job(factory, paper_id)
    queued = make_job(factory, paper_id)
    interrupted = make_job(factory, paper_id)
    done = make_job(factory, paper_id)

    session = factory()
    try:
        ingest.mark_queued(session, queued)
        stuck = session.get(IngestionJob, interrupted)
        stuck.stage = "CHUNKING"
        stuck.progress = 60.0
        finished = session.get(IngestionJob, done)
        finished.stage = "COMPLETED"
        finished.progress = 100.0
        finished.finished_at = datetime.now(timezone.utc)
        session.commit()

        requeue, failed = ingest.recover_jobs(session)
    finally:
        session.close()

    assert sorted(job_id for job_id, _ in requeue) == sorted([waiting, queued])
    assert failed == [interrupted]

    session = factory()
    try:
        row = session.get(IngestionJob, interrupted)
        assert row.stage == "FAILED"
        assert row.error_code == "INTERRUPTED"
        assert row.finished_at is not None
        assert session.get(IngestionJob, done).stage == "COMPLETED"
    finally:
        session.close()


def test_recover_passes_the_payload_so_reindex_routes_correctly(factory):  # noqa: F811
    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)
    session = factory()
    try:
        job = session.get(IngestionJob, job_id)
        job.payload = {"source_type": "reindex"}
        session.commit()

        requeue, _ = ingest.recover_jobs(session)
    finally:
        session.close()

    assert requeue == [(job_id, {"source_type": "reindex"})]
    assert job_queue.kind_for_payload(requeue[0][1]) == job_queue.KIND_REINDEX


def test_queue_recover_enqueues_the_waiting_jobs(factory, monkeypatch):  # noqa: F811
    """``IngestQueue.recover`` reads the database and feeds the workers."""
    paper_id = make_paper(factory)
    waiting = make_job(factory, paper_id)
    monkeypatch.setattr(job_queue, "SessionLocal", factory)

    async def _main() -> dict:
        queue = job_queue.IngestQueue(concurrency=1, runners={"ingest": lambda _: None})
        queue.start()
        try:
            summary = queue.recover()
            assert summary == {"requeued": 1, "interrupted": 0}
            return queue.stats()
        finally:
            await queue.stop()

    stats = asyncio.run(_main())

    assert stats["queued_job_ids"] == [waiting]


def test_interrupted_is_a_known_failure_code():
    from app.core.errors import FAILURE_CODES

    assert "INTERRUPTED" in FAILURE_CODES


def test_new_job_ids_are_unique():
    """Guard the helper the other tests rely on."""
    assert new_uuid() != new_uuid()
