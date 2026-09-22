"""Housekeeping: the collector that removes upload debris (2026-09-19).

Two things leak if nobody watches: staging objects under ``uploads/`` (a killed
process leaves half-written ones, a crashed request leaves whole ones) and
extraction directories under the temp dir. The collector runs at startup and then
every ``INGEST_GC_INTERVAL_S``.

What matters in these tests:

* a **live** job's staging object is never touched -- that job is still going to
  read those bytes;
* a **terminal** job's staging residue and an **unowned** object are both removed;
* an extraction directory goes only when no live job references it *and* it is
  older than ``INGEST_ARCHIVE_TTL_HOURS``;
* the pass is idempotent, never raises (a broken collector must not take the app
  down) and never touches job rows;
* the periodic loop fires once immediately and stops cleanly on cancellation.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db.models import IngestionJob, new_uuid
from app.workers import housekeeping
from tests.test_local_source import factory  # noqa: F401 - fixture


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeStorage:
    """Stands in for ``app.services.object_storage``."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = list(keys)
        self.deleted: list[str] = []
        self.fail_delete: set[str] = set()
        self.list_error: Exception | None = None

    def list_objects(self, prefix: str = "", bucket: str | None = None):
        if self.list_error is not None:
            raise self.list_error
        return [SimpleNamespace(object_name=key) for key in self.keys if key.startswith(prefix)]

    def delete_object(self, key: str, bucket: str | None = None) -> None:
        if key in self.fail_delete:
            raise RuntimeError("minio said no")
        self.deleted.append(key)
        if key in self.keys:
            self.keys.remove(key)


def make_job_row(
    session_factory,
    *,
    payload: dict,
    stage: str = "QUEUED",
    finished: bool = False,
    finished_hours_ago: float = 0.0,
) -> str:
    session = session_factory()
    try:
        job = IngestionJob(
            id=new_uuid(),
            kind="ingest",
            stage=stage,
            progress=0.0,
            payload=payload,
            started_at=datetime.now(timezone.utc),
            finished_at=(
                datetime.now(timezone.utc) - timedelta(hours=finished_hours_ago)
                if finished
                else None
            ),
        )
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


def age(path: Path, hours: float) -> None:
    """Backdate a file/directory so the TTL check sees it as old."""
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def run(session_factory, storage, tmp_dir, **kwargs):
    return housekeeping.run_gc(
        session_factory=session_factory,
        storage=storage,
        tmp_dir=str(tmp_dir),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# staging objects
# --------------------------------------------------------------------------- #
def test_orphan_staging_object_is_removed(factory, tmp_path):  # noqa: F811
    storage = FakeStorage(["uploads/req1/1-a.pdf", "papers/x/original.pdf"])

    report = run(factory, storage, tmp_path)

    assert report.orphan_staging == ["uploads/req1/1-a.pdf"]
    assert storage.deleted == ["uploads/req1/1-a.pdf"]
    assert report.stale_staging == []


def test_staging_object_of_a_finished_job_is_removed(factory, tmp_path):  # noqa: F811
    make_job_row(
        factory,
        payload={"source_type": "file", "object_key": "uploads/req2/1-b.pdf"},
        stage="COMPLETED",
        finished=True,
    )
    storage = FakeStorage(["uploads/req2/1-b.pdf"])

    report = run(factory, storage, tmp_path)

    assert report.stale_staging == ["uploads/req2/1-b.pdf"]
    assert report.orphan_staging == []


def test_staging_object_of_a_live_job_is_kept(factory, tmp_path):  # noqa: F811
    make_job_row(
        factory,
        payload={"source_type": "file", "object_key": "uploads/req3/1-c.pdf"},
        stage="QUEUED",
    )
    make_job_row(
        factory,
        payload={"source_type": "file", "object_key": "uploads/req4/1-d.pdf"},
        stage="EMBEDDING",
    )
    storage = FakeStorage(["uploads/req3/1-c.pdf", "uploads/req4/1-d.pdf"])

    report = run(factory, storage, tmp_path)

    assert report.removed == 0
    assert storage.deleted == []


def test_staging_bytes_of_a_pre_stored_failure_are_kept_for_the_retry(
    factory, tmp_path
):  # noqa: F811
    """A FAILED job without a paper row can still be retried from its bytes.

    ``POST /api/jobs/{id}/retry`` re-reads ``payload["object_key"]`` for a job that
    died before the ``STORED`` checkpoint, so the collector must not treat that
    terminal row as "nobody needs this" (it did until 2026-09-22).
    """
    make_job_row(
        factory,
        payload={"source_type": "file", "object_key": "uploads/req9/1-i.pdf"},
        stage="FAILED",
        finished=True,
    )
    storage = FakeStorage(["uploads/req9/1-i.pdf"])

    report = run(factory, storage, tmp_path)

    assert storage.deleted == []
    assert report.stale_staging == []
    assert report.orphan_staging == []


def test_pre_stored_staging_is_collected_once_the_retry_window_closes(
    factory, tmp_path
):  # noqa: F811
    """The grace is finite: a job nobody retries must not pin storage forever."""
    over = housekeeping.STAGING_RETRY_GRACE_HOURS + 1
    make_job_row(
        factory,
        payload={"source_type": "file", "object_key": "uploads/req10/1-j.pdf"},
        stage="FAILED",
        finished=True,
        finished_hours_ago=over,
    )
    storage = FakeStorage(["uploads/req10/1-j.pdf"])

    report = run(factory, storage, tmp_path)

    assert report.stale_staging == ["uploads/req10/1-j.pdf"]
    assert storage.deleted == ["uploads/req10/1-j.pdf"]


def test_only_failed_jobs_get_the_retry_grace(factory, tmp_path):  # noqa: F811
    """A terminal row that is not a pre-STORED failure is debris as before."""
    make_job_row(
        factory,
        payload={"source_type": "file", "object_key": "uploads/req11/1-k.pdf"},
        stage="INTERRUPTED",
        finished=True,
    )
    storage = FakeStorage(["uploads/req11/1-k.pdf"])

    report = run(factory, storage, tmp_path)

    assert report.stale_staging == ["uploads/req11/1-k.pdf"]
    assert storage.deleted == ["uploads/req11/1-k.pdf"]


def test_a_half_written_object_without_a_job_is_removed(factory, tmp_path):  # noqa: F811
    """The classic crash case: the upload started, the job row never happened."""
    storage = FakeStorage(["uploads/req5/1-half.pdf"])

    report = run(factory, storage, tmp_path)

    assert report.orphan_staging == ["uploads/req5/1-half.pdf"]


# --------------------------------------------------------------------------- #
# extraction directories
# --------------------------------------------------------------------------- #
def make_extraction_dir(tmp_path: Path, request_id: str, *, age_hours: float = 0.0) -> Path:
    directory = tmp_path / f"paperbox-{request_id}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a.pdf").write_bytes(b"%PDF-1.7\n")
    if age_hours:
        age(directory, age_hours)
    return directory


def test_expired_extraction_dir_of_finished_jobs_is_removed(factory, tmp_path):  # noqa: F811
    directory = make_extraction_dir(tmp_path, "old1", age_hours=48)
    make_job_row(
        factory,
        payload={
            "source_type": "local_path",
            "local_path": str(directory / "a.pdf"),
            "cleanup_after": True,
        },
        stage="COMPLETED",
        finished=True,
    )

    report = run(factory, FakeStorage([]), tmp_path, ttl_hours=24)

    assert report.expired_dirs == [str(directory)]
    assert not directory.exists()


def test_extraction_dir_with_a_live_job_is_kept(factory, tmp_path):  # noqa: F811
    directory = make_extraction_dir(tmp_path, "live1", age_hours=48)
    make_job_row(
        factory,
        payload={
            "source_type": "local_path",
            "local_path": str(directory / "a.pdf"),
            "cleanup_after": True,
        },
        stage="QUEUED",
    )

    report = run(factory, FakeStorage([]), tmp_path, ttl_hours=24)

    assert report.expired_dirs == []
    assert directory.exists()


def test_recent_extraction_dir_is_kept_even_without_jobs(factory, tmp_path):  # noqa: F811
    directory = make_extraction_dir(tmp_path, "fresh1")

    report = run(factory, FakeStorage([]), tmp_path, ttl_hours=24)

    assert report.expired_dirs == []
    assert directory.exists()


def test_orphan_extraction_dir_older_than_the_ttl_is_removed(factory, tmp_path):  # noqa: F811
    directory = make_extraction_dir(tmp_path, "orphan1", age_hours=30)

    report = run(factory, FakeStorage([]), tmp_path, ttl_hours=24)

    assert report.expired_dirs == [str(directory)]
    assert not directory.exists()


def test_leftover_archive_files_are_removed_once_they_age_out(factory, tmp_path):  # noqa: F811
    old = tmp_path / "paperbox-dead.zip"
    old.write_bytes(b"PK\x03\x04")
    age(old, 48)
    fresh = tmp_path / "paperbox-fresh.zip"
    fresh.write_bytes(b"PK\x03\x04")

    report = run(factory, FakeStorage([]), tmp_path, ttl_hours=24)

    assert report.expired_archives == [str(old)]
    assert not old.exists()
    assert fresh.exists()


# --------------------------------------------------------------------------- #
# behaviour
# --------------------------------------------------------------------------- #
def test_a_second_pass_finds_nothing(factory, tmp_path):  # noqa: F811
    storage = FakeStorage(["uploads/req6/1-e.pdf"])

    first = run(factory, storage, tmp_path)
    second = run(factory, storage, tmp_path)

    assert first.removed == 1
    assert second.removed == 0


def test_a_storage_failure_is_reported_not_raised(factory, tmp_path):  # noqa: F811
    storage = FakeStorage(["uploads/req7/1-f.pdf"])
    storage.fail_delete = {"uploads/req7/1-f.pdf"}

    report = run(factory, storage, tmp_path)

    assert report.removed == 0
    assert report.errors and "req7" in report.errors[0]


def test_a_listing_failure_is_reported_not_raised(factory, tmp_path):  # noqa: F811
    storage = FakeStorage([])
    storage.list_error = RuntimeError("minio is down")

    report = run(factory, storage, tmp_path)

    assert report.errors


def test_job_rows_are_never_touched(factory, tmp_path):  # noqa: F811
    job_id = make_job_row(
        factory,
        payload={"source_type": "file", "object_key": "uploads/req8/1-g.pdf"},
        stage="COMPLETED",
        finished=True,
    )
    storage = FakeStorage(["uploads/req8/1-g.pdf"])

    run(factory, storage, tmp_path)

    session = factory()
    try:
        job = session.get(IngestionJob, job_id)
        assert job.stage == "COMPLETED"
        assert job.error_code is None
    finally:
        session.close()


def test_report_serializes_for_logging(factory, tmp_path):  # noqa: F811
    storage = FakeStorage(["uploads/req9/1-h.pdf"])

    snapshot = run(factory, storage, tmp_path).as_dict()

    assert snapshot == {
        "removed": 1,
        "orphan_staging": 1,
        "stale_staging": 0,
        "expired_dirs": 0,
        "expired_archives": 0,
        "errors": 0,
    }


# --------------------------------------------------------------------------- #
# the periodic loop
# --------------------------------------------------------------------------- #
def test_the_loop_runs_once_immediately_and_stops_cleanly():
    calls: list[int] = []

    def runner():
        calls.append(1)
        return housekeeping.GcReport()

    async def _main():
        collector = housekeeping.Housekeeping(interval=30, runner=runner)
        collector.start()
        await asyncio.sleep(0.3)
        stats = collector.stats()
        await collector.stop()
        return stats, collector.started

    stats, started = asyncio.run(_main())

    assert calls == [1]
    assert stats["started"] is True
    assert stats["interval_s"] == 30
    assert stats["passes"] == 1
    assert started is False


def test_a_failing_runner_does_not_kill_the_loop():
    calls: list[int] = []

    def runner():
        calls.append(1)
        raise RuntimeError("gc exploded")

    async def _main():
        collector = housekeeping.Housekeeping(interval=1, runner=runner)
        collector.start()
        await asyncio.sleep(0.2)
        await collector.stop()

    asyncio.run(_main())

    assert calls == [1]


def test_start_is_idempotent():
    async def _main():
        collector = housekeeping.Housekeeping(interval=60, runner=lambda: housekeeping.GcReport())
        collector.start()
        collector.start()
        try:
            return len([task for task in asyncio.all_tasks() if "housekeeping" in repr(task)])
        finally:
            await collector.stop()

    assert asyncio.run(_main()) == 1


def test_module_helpers_use_a_singleton():
    assert housekeeping.get_housekeeping() is housekeeping.get_housekeeping()


def test_collect_once_delegates_to_run_gc(factory, tmp_path):  # noqa: F811
    report = housekeeping.collect_once(
        session_factory=factory, storage=FakeStorage([]), tmp_dir=str(tmp_path)
    )

    assert isinstance(report, housekeeping.GcReport)
    assert report.removed == 0
