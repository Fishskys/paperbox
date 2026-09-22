"""Housekeeping: clean up what a crash or a race left behind (2026-09-19).

Two kinds of debris accumulate while uploads are being processed, and neither
belongs to a job that will ever read it again:

* **staging objects** (``uploads/<request_id>/...``) -- the multipart uploads of
  ``/ingest/files``. The pipeline deletes each one at the ``STORED`` checkpoint,
  but a process killed mid-upload leaves a half-written object, and a request
  that crashed between staging and job creation leaves a whole one. Nothing used
  to delete either (9 objects / 49.8 MB were sitting in MinIO).
* **extraction directories** (``<tmp>/paperbox-<request_id>/``) -- where
  ``/ingest/compressed`` unpacks. Each job deletes its own file and prunes the
  directory when it empties, but a job that was never queued, or a process that
  died mid-unpack, leaves a directory nobody owns.

The collector runs once at startup and then every ``INGEST_GC_INTERVAL_S``. It is
**advisory only**: it never touches a job row (no state is invented, no job is
failed), it only removes files, it is idempotent, and every failure is logged and
reported rather than raised -- a broken collector must not take the app down.

**Staging bytes a retry still needs are kept** (2026-09-22): a job that failed
*before* the ``STORED`` checkpoint has no paper row (``paper_id IS NULL``) and
``POST /api/jobs/{id}/retry`` re-reads ``payload["object_key"]`` to redo the
download/store transaction. Treating a terminal row as "nobody needs this" deleted
those bytes in the very next pass (the default interval is 300s), which made the
documented retry of a pre-``STORED`` failure impossible -- a restart left behind
``INTERRUPTED`` jobs in exactly that state. Such an object is now held for
``STAGING_RETRY_GRACE_HOURS`` after ``finished_at`` and collected afterwards.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import IngestionJob
from app.db.session import SessionLocal
from app.services import archive_service, object_storage

logger = get_logger(__name__)

STAGING_PREFIX = f"{object_storage.UPLOAD_PREFIX}/"

#: How long the bytes of a job that failed *before* ``STORED`` are held for a
#: retry (see the module docstring). A code constant on purpose: this is a
#: housekeeping policy, not a deployment knob (``AGENTS.md`` section 3.4).
STAGING_RETRY_GRACE_HOURS = 72


@dataclass
class GcReport:
    """What one collection pass found and removed."""

    orphan_staging: list[str] = field(default_factory=list)
    stale_staging: list[str] = field(default_factory=list)
    expired_dirs: list[str] = field(default_factory=list)
    expired_archives: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def removed(self) -> int:
        return (
            len(self.orphan_staging)
            + len(self.stale_staging)
            + len(self.expired_dirs)
            + len(self.expired_archives)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "removed": self.removed,
            "orphan_staging": len(self.orphan_staging),
            "stale_staging": len(self.stale_staging),
            "expired_dirs": len(self.expired_dirs),
            "expired_archives": len(self.expired_archives),
            "errors": len(self.errors),
        }


@dataclass(frozen=True)
class JobRef:
    """The little a collection pass needs to know about a job."""

    job_id: str
    live: bool
    object_key: str | None
    local_path: str | None
    #: Failed before the ``STORED`` checkpoint (no paper row): its retry re-reads
    #: the staging object, so those bytes must survive the collector.
    retryable: bool = False
    finished_at: datetime | None = None


def load_job_refs(session_factory=SessionLocal) -> list[JobRef]:
    """Every job that could still own a staging object or an extraction file.

    ``finished_at IS NULL`` is the definition of *live* used everywhere else (a
    job that is running or waiting has no finish timestamp), so a live job is
    never collected from under itself.

    ``retryable`` marks the jobs a retry can still re-drive from their staging
    bytes: ``FAILED`` with no ``paper_id`` means the pipeline died before the
    ``STORED`` checkpoint (see ``app/services/ingestion_service.py``); once a
    paper row exists the original lives under ``papers/`` and the staging copy
    is debris again.
    """
    session = session_factory()
    try:
        rows = session.execute(
            select(
                IngestionJob.id,
                IngestionJob.stage,
                IngestionJob.finished_at,
                IngestionJob.payload,
                IngestionJob.paper_id,
            )
        ).all()
    finally:
        session.close()
    refs: list[JobRef] = []
    for job_id, stage, finished_at, payload, paper_id in rows:
        data = payload if isinstance(payload, dict) else {}
        refs.append(
            JobRef(
                job_id=str(job_id),
                live=finished_at is None and stage not in ("COMPLETED", "FAILED"),
                object_key=_as_text(data.get("object_key")),
                local_path=_as_text(data.get("local_path")),
                retryable=stage == "FAILED" and paper_id is None,
                finished_at=finished_at,
            )
        )
    return refs


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _norm(path: str) -> str:
    """Case-folded, separator-normalized form used for prefix matching."""
    return str(path).replace("\\", "/").rstrip("/").lower()


def _is_under(path: str, directory: str) -> bool:
    candidate = _norm(path)
    base = _norm(directory)
    return candidate == base or candidate.startswith(base + "/")


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive timestamps, PostgreSQL aware ones (compare safe)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _keeps_staging(ref: JobRef, moment: datetime) -> bool:
    """Should this job's staging bytes survive this pass?

    A live job always keeps them (it is about to read them). A job that failed
    before ``STORED`` keeps them for ``STAGING_RETRY_GRACE_HOURS`` so its retry can
    redo the download/store transaction, then they are collected like any other
    stale object -- a job nobody retries must not pin storage forever.
    """
    if ref.live:
        return True
    if not ref.retryable or ref.finished_at is None:
        return False
    return _aware(ref.finished_at) > moment - timedelta(hours=STAGING_RETRY_GRACE_HOURS)


def run_gc(
    *,
    session_factory: Callable[[], object] = SessionLocal,
    storage=object_storage,
    tmp_dir: str | None = None,
    ttl_hours: int | None = None,
    now: datetime | None = None,
) -> GcReport:
    """Run one collection pass and return what it removed.

    ``storage``/``session_factory``/``tmp_dir`` are injectable so the pass can be
    exercised without MinIO, PostgreSQL or the real temp directory.
    """
    report = GcReport()
    moment = now or datetime.now(timezone.utc)
    ttl = timedelta(hours=settings.ingest_archive_ttl_hours if ttl_hours is None else ttl_hours)
    base = archive_service.tmp_base(tmp_dir or settings.ingest_archive_tmp_dir or None)

    try:
        refs = load_job_refs(session_factory)
    except Exception as exc:  # noqa: BLE001 - the collector never raises
        logger.exception("housekeeping could not read the jobs table")
        report.errors.append(f"jobs: {type(exc).__name__}: {exc}")
        return report

    staging_owners: dict[str, list[JobRef]] = {}
    for ref in refs:
        if ref.object_key:
            staging_owners.setdefault(ref.object_key, []).append(ref)

    # ---- 1/2: staging objects ------------------------------------------- #
    try:
        keys = [obj.object_name for obj in storage.list_objects(prefix=STAGING_PREFIX)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("housekeeping could not list staging objects: %s", exc)
        report.errors.append(f"list staging: {type(exc).__name__}: {exc}")
        keys = []

    for key in keys:
        owners = staging_owners.get(key)
        if owners and any(_keeps_staging(owner, moment) for owner in owners):
            continue  # a waiting, running or retryable job still needs these bytes
        bucket_name = "orphan" if not owners else "stale"
        if not _delete(storage, key, report):
            continue
        (report.orphan_staging if bucket_name == "orphan" else report.stale_staging).append(key)

    # ---- 3: extraction directories and leftover archives ----------------- #
    cutoff = moment - ttl
    for directory in archive_service.iter_extraction_dirs(base):
        owners = [ref for ref in refs if ref.local_path and _is_under(ref.local_path, str(directory))]
        if any(owner.live for owner in owners):
            continue
        if _mtime(directory) > cutoff:
            continue  # give the jobs a grace period to finish
        archive_service.cleanup_dir(directory)
        report.expired_dirs.append(str(directory))

    try:
        leftovers = [
            entry
            for entry in base.iterdir()
            if entry.is_file() and entry.name.startswith("paperbox-")
        ]
    except OSError:
        leftovers = []
    for entry in leftovers:
        if _mtime(entry) > cutoff:
            continue
        archive_service.remove_file(entry)
        report.expired_archives.append(str(entry))

    logger.info(
        "housekeeping pass finished",
        extra={"extra_fields": report.as_dict()},
    )
    return report


def _delete(storage, key: str, report: GcReport) -> bool:
    try:
        storage.delete_object(key)
    except Exception as exc:  # noqa: BLE001 - one stuck object must not stop the pass
        logger.warning("housekeeping could not delete %s: %s", key, exc)
        report.errors.append(f"delete {key}: {type(exc).__name__}: {exc}")
        return False
    return True


def _mtime(path: Path) -> datetime:
    """Modification time as an aware datetime (missing path counts as new)."""
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime, tz=timezone.utc)
    except OSError:  # pragma: no cover - vanished between listing and stat
        return datetime.now(timezone.utc)


class Housekeeping:
    """Periodic GC loop driven by the FastAPI lifespan."""

    def __init__(
        self,
        interval: int | None = None,
        runner: Callable[[], GcReport] | None = None,
    ) -> None:
        requested = settings.ingest_gc_interval_s if interval is None else interval
        self.interval = max(1, int(requested))
        self._runner = runner or run_gc
        self._lock = threading.Lock()
        self._task: asyncio.Task[None] | None = None
        self.passes = 0
        self.last: dict[str, object] | None = None

    @property
    def started(self) -> bool:
        with self._lock:
            return self._task is not None

    def start(self) -> None:
        """Start the loop (must run inside the event loop); it fires immediately."""
        if self.started:
            return
        loop = asyncio.get_running_loop()
        with self._lock:
            self._task = loop.create_task(self._loop(), name="paperbox-housekeeping")
        logger.info(
            "housekeeping started",
            extra={"extra_fields": {"interval_s": self.interval}},
        )

    async def stop(self) -> None:
        with self._lock:
            task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        logger.info("housekeeping stopped")

    async def _loop(self) -> None:
        while True:
            try:
                report = await asyncio.to_thread(self._runner)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                logger.exception("housekeeping pass failed")
            else:
                self.passes += 1
                self.last = report.as_dict() if isinstance(report, GcReport) else None
            await asyncio.sleep(self.interval)

    def stats(self) -> dict[str, object]:
        return {
            "started": self.started,
            "interval_s": self.interval,
            "passes": self.passes,
            "last": self.last,
        }


_housekeeping: Housekeeping | None = None


def get_housekeeping() -> Housekeeping:
    """Return the process-wide collector, creating it on first use."""
    global _housekeeping
    if _housekeeping is None:
        _housekeeping = Housekeeping()
    return _housekeeping


def start() -> None:
    """Start the periodic pass (called from the FastAPI lifespan)."""
    get_housekeeping().start()


async def stop() -> None:
    """Stop the periodic pass (called from the FastAPI lifespan)."""
    await get_housekeeping().stop()


def stats() -> dict[str, object]:
    """Snapshot of the collector (for logs and diagnostics)."""
    return get_housekeeping().stats()


def collect_once(**kwargs) -> GcReport:
    """Run a single pass now (used by the lifespan startup and by scripts)."""
    return run_gc(**kwargs)


__all__ = [
    "GcReport",
    "Housekeeping",
    "JobRef",
    "STAGING_PREFIX",
    "STAGING_RETRY_GRACE_HOURS",
    "collect_once",
    "get_housekeeping",
    "load_job_refs",
    "run_gc",
    "start",
    "stats",
    "stop",
]
