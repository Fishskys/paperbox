"""Queue priorities: an interactive upload must jump ahead of a batch (2026-09-19).

``/api/papers/ingest/files`` classifies a request by how many files it carries:
one file is *interactive* (a human waiting), two or more is *batch* (a folder
dump). The queue is therefore a ``asyncio.PriorityQueue`` ordered by
``(priority, seq)`` -- the sequence number keeps FIFO order inside one
priority class.

The tests drive the real queue with a fake runner (no database, no HTTP) and
block the single worker on a ``threading.Event`` so the waiting set is stable
while it is inspected.
"""

from __future__ import annotations

import asyncio
import threading

from app.workers import queue as job_queue


class BlockingRunner:
    """Runs until released; records the order in which jobs started."""

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.started: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, job_id: str) -> None:
        with self.lock:
            self.started.append(job_id)
        self.release.wait(3.0)


def drive(setup) -> tuple[list[str], dict]:
    """Start a 1-slot queue, run ``setup(queue, runner, release)``, drain it.

    Returns ``(order, snapshot)``: the order jobs started in *after* the queue
    drained, and whatever ``setup`` inspected while the worker was blocked.
    """

    async def _main() -> tuple[list[str], dict]:
        release = threading.Event()
        runner = BlockingRunner(release)
        queue = job_queue.IngestQueue(concurrency=1, runners={"ingest": runner})
        queue.start()
        try:
            snapshot = await setup(queue, runner, release)
            release.set()
            await queue.join()
            return list(runner.started), snapshot
        finally:
            await queue.stop()

    return asyncio.run(_main())


# --------------------------------------------------------------------------- #
# priority classes
# --------------------------------------------------------------------------- #
def test_priority_constants_order_interactive_first():
    assert job_queue.PRIORITY_INTERACTIVE < job_queue.PRIORITY_BATCH
    assert job_queue.PRIORITY_INTERACTIVE == 0


def test_interactive_job_jumps_ahead_of_queued_batch_jobs():
    async def setup(queue, runner, release):
        for index in range(4):
            queue.enqueue(f"batch-{index}", priority=job_queue.PRIORITY_BATCH)
        await asyncio.sleep(0.2)  # the single worker picks up batch-0
        queue.enqueue("interactive", priority=job_queue.PRIORITY_INTERACTIVE)
        await asyncio.sleep(0.2)
        return queue.stats()

    order, _ = drive(setup)

    assert order == ["batch-0", "interactive", "batch-1", "batch-2", "batch-3"]


def test_same_priority_class_stays_fifo():
    async def setup(queue, runner, release):
        for job_id in ("batch-a", "batch-b", "batch-c"):
            queue.enqueue(job_id, priority=job_queue.PRIORITY_BATCH)
        await asyncio.sleep(0.2)
        queue.enqueue("interactive", priority=job_queue.PRIORITY_INTERACTIVE)
        await asyncio.sleep(0.2)
        return queue.stats()

    order, _ = drive(setup)

    assert order == ["batch-a", "interactive", "batch-b", "batch-c"]


def test_default_priority_is_interactive():
    async def setup(queue, runner, release):
        queue.enqueue("implicit")
        await asyncio.sleep(0.2)
        return queue.stats()

    _, snapshot = drive(setup)

    assert snapshot["queued_high"] == 0
    assert snapshot["queued_low"] == 0


# --------------------------------------------------------------------------- #
# introspection
# --------------------------------------------------------------------------- #
def test_stats_split_queued_jobs_by_priority_class():
    async def setup(queue, runner, release):
        queue.enqueue("running", priority=job_queue.PRIORITY_BATCH)
        await asyncio.sleep(0.2)  # worker takes it, then blocks
        for index in range(2):
            queue.enqueue(f"high-{index}", priority=job_queue.PRIORITY_INTERACTIVE)
        for index in range(3):
            queue.enqueue(f"low-{index}", priority=job_queue.PRIORITY_BATCH)
        await asyncio.sleep(0.1)
        return queue.stats()

    _, snapshot = drive(setup)

    assert snapshot["running"] == 1
    assert snapshot["queued"] == 5
    assert snapshot["queued_high"] == 2
    assert snapshot["queued_low"] == 3
    # ``queued_job_ids`` stays in insertion order so the API snapshot is stable.
    assert snapshot["queued_job_ids"] == [
        "high-0",
        "high-1",
        "low-0",
        "low-1",
        "low-2",
    ]


def test_depth_counts_waiting_jobs_only():
    async def setup(queue, runner, release):
        queue.enqueue("running")
        await asyncio.sleep(0.2)
        queue.enqueue("waiting-1")
        queue.enqueue("waiting-2")
        await asyncio.sleep(0.1)
        return {"depth": queue.depth()}

    _, snapshot = drive(setup)

    assert snapshot["depth"] == 2


def test_depth_is_zero_when_idle():
    queue = job_queue.IngestQueue(concurrency=2, runners={"ingest": lambda _: None})

    assert queue.depth() == 0
