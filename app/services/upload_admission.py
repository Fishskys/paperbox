"""Upload admission control for the byte-carrying ingestion endpoint (2026-09-19).

Three public entry points exist, but only ``POST /api/papers/ingest/files``
carries bytes from the client:

* ``/ingest/dir`` and ``/ingest/compressed`` are read by the server itself, so
  there is nothing to admit -- the server paces them by looking at the queue;
* ``/ingest/files`` streams multipart parts into object storage, and the client
  has no concurrency knob (decision 3 in the plan). The **server** therefore
  owns the decision, expressed as HTTP back-pressure:

  - too many uploads already in flight (``INGEST_UPLOAD_CONCURRENCY``) -> the
    request is refused with ``429 + Retry-After`` rather than buffered;
  - the *processing* backlog is at or above ``INGEST_QUEUE_HIGH_WATERMARK`` ->
    a **multi-file** request is refused with the same 429, while a single-file
    request (a human waiting on one answer) is still accepted.

The counter is guarded by a ``threading.Lock`` instead of an ``asyncio.Semaphore``:
the endpoints are ``async`` but the counting must be exact no matter which thread
touches it, and a semaphore created on one event loop cannot be used from
another (which is exactly what tests and one-off scripts do).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Value of the ``Retry-After`` header sent with a 429 (seconds).
RETRY_AFTER_SECONDS = 2


class AdmissionRejected(RuntimeError):
    """The server refused an upload request; the client must back off and retry.

    ``reason`` is a short machine-readable phrase (``"upload concurrency"`` /
    ``"ingestion backlog"``) that the endpoint puts in the 429 body; the client
    only has to honour ``retry_after``.
    """

    def __init__(
        self, reason: str, *, retry_after: int = RETRY_AFTER_SECONDS
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


def _queue_depth() -> int:
    """Backlog of the in-process ingestion queue (imported lazily, no cycle)."""
    from app.workers import queue as job_queue

    return job_queue.depth()


class UploadAdmission:
    """In-flight upload counter plus the backlog watermark predicate."""

    def __init__(
        self,
        limit: int | None = None,
        high_watermark: int | None = None,
        depth_provider: Callable[[], int] | None = None,
    ) -> None:
        requested = (
            settings.ingest_upload_concurrency if limit is None else int(limit)
        )
        watermark = (
            settings.ingest_queue_high_watermark
            if high_watermark is None
            else int(high_watermark)
        )
        self.limit = max(1, requested)
        #: 0 disables backlog throttling (an operator escape hatch).
        self.high_watermark = max(0, watermark)
        self._depth_provider = depth_provider or _queue_depth
        self._lock = threading.Lock()
        self._in_flight = 0

    # ------------------------------------------------------------------ #
    # in-flight ceiling
    # ------------------------------------------------------------------ #
    @property
    def in_flight(self) -> int:
        """How many upload requests hold a slot right now."""
        with self._lock:
            return self._in_flight

    def try_acquire(self) -> bool:
        """Take one upload slot; ``False`` when the ceiling is reached."""
        with self._lock:
            if self._in_flight >= self.limit:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        """Give a slot back (never goes negative, safe to call twice)."""
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold an upload slot for the duration of the block.

        Raises :class:`AdmissionRejected` when the ceiling is reached; the slot
        is released on every exit path (success, exception, cancellation).
        """
        if not self.try_acquire():
            raise AdmissionRejected(
                f"upload concurrency: {self.limit} request(s) already in flight"
            )
        try:
            yield
        finally:
            self.release()

    # ------------------------------------------------------------------ #
    # backlog watermark
    # ------------------------------------------------------------------ #
    def should_throttle_batch(self) -> bool:
        """Whether a *multi-file* request must be refused right now.

        A single-file request is deliberately exempt: it is one human waiting,
        and the backlog it would add is one job.
        """
        if self.high_watermark <= 0:
            return False
        return self._depth_provider() >= self.high_watermark

    def snapshot(self) -> dict[str, object]:
        """Admission state for logs and the queue endpoint."""
        depth = self._depth_provider()
        return {
            "limit": self.limit,
            "in_flight": self.in_flight,
            "high_watermark": self.high_watermark,
            "depth": depth,
            "throttling_batch": self.should_throttle_batch(),
        }


_admission: UploadAdmission | None = None


def get_admission() -> UploadAdmission:
    """Return the process-wide admission gate, creating it on first use."""
    global _admission
    if _admission is None:
        _admission = UploadAdmission()
    return _admission


def reset() -> None:
    """Drop the singleton (tests / settings reload)."""
    global _admission
    _admission = None


__all__ = [
    "RETRY_AFTER_SECONDS",
    "AdmissionRejected",
    "UploadAdmission",
    "get_admission",
    "reset",
]
