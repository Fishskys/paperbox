"""In-process ingestion queue with a bounded number of parallel pipelines.

Every ingestion request used to hand its job straight to FastAPI
``BackgroundTasks``, so ten quick uploads started ten concurrent pipelines:
each one streamed a PDF into MinIO and then hit the embedding server
(``MAX_BATCH=16`` per request, 4 ORT threads) and OpenSearch at the same time.
Nothing was serialised, and the caller had no way to see how much work was
pending.

This module keeps the "no Redis, no Celery" design (plan section 30) but adds:

* **a concurrency ceiling** -- ``INGEST_CONCURRENCY`` (default 2) pipelines run
  at once, everything else waits in an in-process queue in stage ``QUEUED``;
* **priority classes** -- one file is interactive, a folder dump is batch, and
  an interactive job is picked up before any waiting batch job;
* **visibility** -- :func:`stats` (exposed as ``GET /api/jobs/queue``) reports
  the ceiling, the in-flight jobs, the waiting ones and the split between the
  two priority classes.

The queue is deliberately in-process: the pipeline lives in the web process
(``--workers 1``, see ``AGENTS.md`` section 1), so the queue is the single owner
of job execution. A restart does not lose work either -- :func:`recover` re-queues
jobs left in ``RECEIVED``/``QUEUED`` and marks jobs that were mid-pipeline as
``INTERRUPTED``, which ``POST /api/jobs/{id}/retry`` can re-drive.

Worker coroutines never touch the database directly: they hand the job id to the
blocking pipeline (``app.workers.tasks``) in a worker thread, so the event loop
stays free to serve requests while up to ``concurrency`` pipelines run.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.services import ingestion_service as ingest
from app.workers import tasks

logger = get_logger(__name__)

#: Job flavours the queue knows how to run (the key is stored nowhere -- it only
#: selects the pipeline entry point).
KIND_INGEST = "ingest"
KIND_REINDEX = "reindex"
KIND_RETRY = "retry"

#: Priority classes. A single-file upload is a human waiting on the answer; a
#: multi-file upload is a folder dump that can afford to wait. Lower wins.
PRIORITY_INTERACTIVE = 0
PRIORITY_BATCH = 1
DEFAULT_PRIORITY = PRIORITY_INTERACTIVE

Runner = Callable[[str], None]

DEFAULT_RUNNERS: dict[str, Runner] = {
    KIND_INGEST: tasks.run_ingestion_job,
    KIND_REINDEX: tasks.run_reindex_job,
    KIND_RETRY: tasks.run_retry_job,
}

#: ``payload["source_type"]`` value that means "this job re-parses a stored PDF".
SOURCE_TYPE_REINDEX = "reindex"


@dataclass(frozen=True)
class _Item:
    """One queued unit of work."""

    job_id: str
    kind: str
    priority: int = DEFAULT_PRIORITY


def kind_for_payload(payload: Mapping[str, object] | None) -> str:
    """Pick the runner for a recovered job from its stored payload."""
    source_type = str((payload or {}).get("source_type") or "").lower()
    return KIND_REINDEX if source_type == SOURCE_TYPE_REINDEX else KIND_INGEST


class IngestQueue:
    """Priority job queue driven by ``concurrency`` worker coroutines."""

    def __init__(
        self,
        concurrency: int | None = None,
        runners: Mapping[str, Runner] | None = None,
    ) -> None:
        requested = settings.ingest_concurrency if concurrency is None else concurrency
        self.concurrency = max(1, int(requested))
        self._runners: dict[str, Runner] = dict(runners or DEFAULT_RUNNERS)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.PriorityQueue[tuple[int, int, _Item]] | None = None
        self._workers: list[asyncio.Task[None]] = []
        #: job_id -> (kind, priority), waiting for a worker (insertion ordered).
        self._pending: dict[str, tuple[str, int]] = {}
        #: job_id -> kind, currently being run by a worker.
        self._running: dict[str, str] = {}
        #: Monotonic tie-breaker: ``(priority, seq)`` keeps FIFO inside a class.
        self._seq = 0

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    @property
    def started(self) -> bool:
        """Whether the worker coroutines are alive."""
        with self._lock:
            return bool(self._workers)

    def start(self) -> None:
        """Create the worker coroutines (must run inside the event loop)."""
        if self.started:
            return
        loop = asyncio.get_running_loop()
        with self._lock:
            self._loop = loop
            self._queue = asyncio.PriorityQueue()
            self._pending.clear()
            self._running.clear()
            self._seq = 0
            self._workers = [
                loop.create_task(self._worker(index), name=f"paperbox-ingest-{index}")
                for index in range(1, self.concurrency + 1)
            ]
        logger.info(
            "ingest queue started",
            extra={"extra_fields": {"concurrency": self.concurrency}},
        )

    async def stop(self) -> None:
        """Cancel the workers. A pipeline already inside a thread keeps running
        to completion (threads are not cancellable); its job row stays in an
        in-flight stage and :meth:`recover` marks it ``INTERRUPTED`` next start.
        """
        with self._lock:
            workers, self._workers = self._workers, []
            self._loop = None
            self._queue = None
        for task in workers:
            task.cancel()
        for task in workers:
            with suppress(asyncio.CancelledError):
                await task
        if workers:
            logger.info("ingest queue stopped")

    async def join(self) -> None:
        """Wait until every queued job has been picked up and finished."""
        queue = self._queue
        if queue is not None:
            await queue.join()

    # ------------------------------------------------------------------ #
    # admission
    # ------------------------------------------------------------------ #
    def enqueue(
        self,
        job_id: str,
        kind: str = KIND_INGEST,
        priority: int = DEFAULT_PRIORITY,
    ) -> bool:
        """Hand one job to the workers.

        ``priority`` is :data:`PRIORITY_INTERACTIVE` (default) or
        :data:`PRIORITY_BATCH`; lower values are picked up first, and jobs of the
        same class keep FIFO order.

        Returns ``True`` when the job was queued. When the queue was never
        started (one-off scripts, unit tests) the pipeline runs inline instead
        and ``False`` is returned: work must never be dropped silently. The
        call is safe from any thread -- the actual hand-off is scheduled on the
        event loop.
        """
        if kind not in self._runners:
            raise ValueError(f"unknown job kind: {kind}")
        with self._lock:
            if job_id in self._pending or job_id in self._running:
                logger.warning("job %s is already queued or running", job_id)
                return False
            loop, queue = self._loop, self._queue
            live = bool(self._workers) and loop is not None and queue is not None
            if live:
                self._seq += 1
                seq = self._seq
                self._pending[job_id] = (kind, int(priority))
        if not live:
            logger.warning(
                "ingest queue is not running; executing job %s inline", job_id
            )
            self._run_inline(job_id, kind)
            return False
        entry = (int(priority), seq, _Item(job_id, kind, int(priority)))
        if self._on_loop(loop):
            # Callers inside the event loop (async endpoints, tests) hand the
            # item over synchronously, so ``join()`` cannot miss it.
            self._hand_off(entry)
        else:
            loop.call_soon_threadsafe(self._hand_off, entry)
        return True

    @staticmethod
    def _on_loop(loop: asyncio.AbstractEventLoop) -> bool:
        """True when the caller already runs inside the queue's event loop."""
        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:
            return False

    def submit(
        self,
        session: Session,
        job_id: str,
        kind: str = KIND_INGEST,
        priority: int = DEFAULT_PRIORITY,
    ):
        """Mark the job ``QUEUED`` (committed) and hand it to the workers.

        Returns the refreshed job row, or ``None`` when the job had already
        moved past ``RECEIVED``/``QUEUED`` (nothing is enqueued in that case).
        """
        job = ingest.mark_queued(session, job_id)
        if job is None:
            logger.warning("job %s is not queueable; stage already moved on", job_id)
            return None
        self.enqueue(job_id, kind, priority)
        return job

    # ------------------------------------------------------------------ #
    # startup recovery
    # ------------------------------------------------------------------ #
    def recover(self) -> dict[str, int]:
        """Reconcile jobs a previous process left behind (call at startup)."""
        session = SessionLocal()
        try:
            requeue, interrupted = ingest.recover_jobs(session)
        finally:
            session.close()
        for job_id, payload in requeue:
            self.enqueue(job_id, kind_for_payload(payload))
        if requeue or interrupted:
            logger.warning(
                "ingest queue recovered jobs from a previous process",
                extra={
                    "extra_fields": {
                        "requeued": len(requeue),
                        "interrupted": len(interrupted),
                    }
                },
            )
        return {"requeued": len(requeue), "interrupted": len(interrupted)}

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #
    def depth(self) -> int:
        """Number of jobs waiting for a free pipeline slot (safe from any thread).

        This is the queue depth the upload admission uses as its high watermark;
        running jobs are *not* counted, so the value is exactly the backlog the
        server has not started on yet.
        """
        with self._lock:
            return len(self._pending)

    def stats(self) -> dict[str, object]:
        """Queue depth snapshot (safe from any thread)."""
        with self._lock:
            pending = list(self._pending)
            running = list(self._running)
            started = bool(self._workers)
            high = sum(1 for _, (_, priority) in self._pending.items() if priority <= 0)
            low = sum(1 for _, (_, priority) in self._pending.items() if priority > 0)
        return {
            "started": started,
            "concurrency": self.concurrency,
            "running": len(running),
            "queued": len(pending),
            "queued_high": high,
            "queued_low": low,
            "running_job_ids": running,
            "queued_job_ids": pending,
        }

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    async def _worker(self, index: int) -> None:
        """Pull jobs forever; one pipeline at a time per worker."""
        queue = self._queue
        if queue is None:  # pragma: no cover - start() always creates it
            return
        while True:
            _, _, item = await queue.get()
            with self._lock:
                self._pending.pop(item.job_id, None)
                self._running[item.job_id] = item.kind
            try:
                runner = self._runners.get(item.kind)
                if runner is None:  # pragma: no cover - enqueue() rejects these
                    logger.error("unknown job kind %s for %s", item.kind, item.job_id)
                else:
                    logger.info(
                        "queue worker %d picked up %s job %s (priority=%d)",
                        index,
                        item.kind,
                        item.job_id,
                        item.priority,
                    )
                    await asyncio.to_thread(runner, item.job_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a worker must never die
                logger.exception(
                    "queue worker %d failed on %s job %s", index, item.kind, item.job_id
                )
            finally:
                with self._lock:
                    self._running.pop(item.job_id, None)
                queue.task_done()

    def _hand_off(self, entry: tuple[int, int, _Item]) -> None:
        """Put an item on the loop-owned queue (runs on the event loop)."""
        queue = self._queue
        if queue is None:
            # stop() raced with the hand-off: leave the job QUEUED so the next
            # start() recovers it instead of running it outside the ceiling.
            logger.warning("queue stopped before %s was handed off", entry[2].job_id)
            return
        queue.put_nowait(entry)

    def _run_inline(self, job_id: str, kind: str) -> None:
        """Run a job on the calling thread (queue not started)."""
        runner = self._runners.get(kind)
        if runner is None:  # pragma: no cover - enqueue() rejects these
            logger.error("unknown job kind %s for %s", kind, job_id)
            return
        runner(job_id)


_queue: IngestQueue | None = None


def get_queue() -> IngestQueue:
    """Return the process-wide queue, creating it on first use."""
    global _queue
    if _queue is None:
        _queue = IngestQueue()
    return _queue


def start() -> None:
    """Start the workers (called from the FastAPI lifespan)."""
    get_queue().start()


async def stop() -> None:
    """Stop the workers (called from the FastAPI lifespan)."""
    await get_queue().stop()


def recover() -> dict[str, int]:
    """Reconcile jobs left by a previous process (called at startup)."""
    return get_queue().recover()


def enqueue(
    job_id: str, kind: str = KIND_INGEST, priority: int = DEFAULT_PRIORITY
) -> bool:
    """Queue one job id."""
    return get_queue().enqueue(job_id, kind, priority)


def submit(
    session: Session,
    job_id: str,
    kind: str = KIND_INGEST,
    priority: int = DEFAULT_PRIORITY,
):
    """Mark a job ``QUEUED`` and queue it."""
    return get_queue().submit(session, job_id, kind, priority)


def depth() -> int:
    """Number of jobs waiting for a pipeline slot."""
    return get_queue().depth()


def stats() -> dict[str, object]:
    """Queue depth snapshot."""
    return get_queue().stats()


async def join() -> None:
    """Wait for the queue to drain."""
    await get_queue().join()


__all__ = [
    "DEFAULT_PRIORITY",
    "DEFAULT_RUNNERS",
    "IngestQueue",
    "KIND_INGEST",
    "KIND_REINDEX",
    "KIND_RETRY",
    "PRIORITY_BATCH",
    "PRIORITY_INTERACTIVE",
    "SOURCE_TYPE_REINDEX",
    "depth",
    "enqueue",
    "get_queue",
    "join",
    "kind_for_payload",
    "recover",
    "start",
    "stats",
    "stop",
    "submit",
]
