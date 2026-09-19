"""Upload admission: the server decides how many uploads are in flight (2026-09-19).

``POST /api/papers/ingest/files`` is the only entry point that carries bytes, so
it is the only one that needs an admission gate:

* ``INGEST_UPLOAD_CONCURRENCY`` caps how many upload requests may be *in flight*
  at once -- the excess is answered with ``429 + Retry-After`` instead of being
  buffered;
* ``INGEST_QUEUE_HIGH_WATERMARK`` caps how deep the *processing* backlog may get
  before the server refuses a **multi-file** request (a single-file request is a
  human waiting, so it is never refused on backlog).

The gate is a counter behind a ``threading.Lock``: the endpoint is ``async`` but
the pipeline and the accounting must be exact under threads, so a semaphore with
its own loop affinity would be the wrong tool.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.services import upload_admission


# --------------------------------------------------------------------------- #
# in-flight ceiling
# --------------------------------------------------------------------------- #
def test_slots_are_granted_up_to_the_limit_then_refused():
    admission = upload_admission.UploadAdmission(limit=2, depth_provider=lambda: 0)

    assert admission.try_acquire() is True
    assert admission.try_acquire() is True
    assert admission.try_acquire() is False
    assert admission.in_flight == 2
    assert admission.snapshot()["limit"] == 2


def test_release_frees_a_slot():
    admission = upload_admission.UploadAdmission(limit=1, depth_provider=lambda: 0)
    admission.try_acquire()

    admission.release()

    assert admission.in_flight == 0
    assert admission.try_acquire() is True


def test_release_without_a_slot_never_goes_negative():
    admission = upload_admission.UploadAdmission(limit=1, depth_provider=lambda: 0)

    admission.release()
    admission.release()

    assert admission.in_flight == 0


def test_slot_context_manager_releases_on_success_and_on_error():
    admission = upload_admission.UploadAdmission(limit=1, depth_provider=lambda: 0)

    with admission.slot():
        assert admission.in_flight == 1

    assert admission.in_flight == 0

    with pytest.raises(RuntimeError):
        with admission.slot():
            raise RuntimeError("upload exploded")

    assert admission.in_flight == 0


def test_slot_raises_admission_rejected_when_saturated():
    admission = upload_admission.UploadAdmission(limit=1, depth_provider=lambda: 0)
    admission.try_acquire()

    with pytest.raises(upload_admission.AdmissionRejected) as excinfo:
        with admission.slot():
            pass  # pragma: no cover - never reached

    assert excinfo.value.retry_after == upload_admission.RETRY_AFTER_SECONDS
    assert "in flight" in excinfo.value.reason
    assert admission.in_flight == 1


def test_concurrent_threads_never_exceed_the_limit():
    admission = upload_admission.UploadAdmission(limit=3, depth_provider=lambda: 0)
    lock = threading.Lock()
    concurrent = 0
    max_seen = 0
    granted = 0
    start = threading.Barrier(12)

    def worker() -> None:
        nonlocal concurrent, max_seen, granted
        start.wait(5.0)
        if not admission.try_acquire():
            return
        with lock:
            granted += 1
            concurrent += 1
            max_seen = max(max_seen, concurrent)
        time.sleep(0.05)
        with lock:
            concurrent -= 1
        admission.release()

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)

    assert max_seen == 3
    assert granted >= 3
    assert admission.in_flight == 0


def test_threads_are_refused_while_the_slots_are_held():
    admission = upload_admission.UploadAdmission(limit=2, depth_provider=lambda: 0)
    for _ in range(2):
        assert admission.try_acquire() is True
    refused: list[bool] = []

    def worker() -> None:
        refused.append(not admission.try_acquire())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)

    assert refused == [True] * 5
    assert admission.in_flight == 2


# --------------------------------------------------------------------------- #
# backlog watermark
# --------------------------------------------------------------------------- #
def test_batch_requests_are_throttled_at_the_high_watermark():
    depth = {"value": 49}
    admission = upload_admission.UploadAdmission(
        limit=2, high_watermark=50, depth_provider=lambda: depth["value"]
    )

    assert admission.should_throttle_batch() is False

    depth["value"] = 50
    assert admission.should_throttle_batch() is True

    depth["value"] = 500
    assert admission.should_throttle_batch() is True


def test_high_watermark_of_zero_disables_backlog_throttling():
    admission = upload_admission.UploadAdmission(
        limit=1, high_watermark=0, depth_provider=lambda: 9999
    )

    assert admission.should_throttle_batch() is False


def test_snapshot_reports_the_backlog_and_the_watermark():
    admission = upload_admission.UploadAdmission(
        limit=4, high_watermark=7, depth_provider=lambda: 3
    )
    admission.try_acquire()

    snapshot = admission.snapshot()

    assert snapshot == {
        "limit": 4,
        "in_flight": 1,
        "high_watermark": 7,
        "depth": 3,
        "throttling_batch": False,
    }


def test_defaults_come_from_settings():
    admission = upload_admission.UploadAdmission()

    from app.core.config import settings

    assert admission.limit == settings.ingest_upload_concurrency
    assert admission.high_watermark == settings.ingest_queue_high_watermark


def test_module_level_singleton_is_reused():
    assert upload_admission.get_admission() is upload_admission.get_admission()
