"""``POST /api/papers/ingest/files``: a batch upload endpoint (2026-09-19).

One request carries one or more files. Each part is validated, streamed into the
staging area (``uploads/<request_id>/<index>-<name>.pdf``) while its SHA256 is
computed, deduped, and -- if it is new -- turned into a job that goes to the
queue. A single bad file is reported in the response, not as a request failure.

Back-pressure is server-side: ``429 + Retry-After`` when too many uploads are in
flight, and (for multi-file requests only) when the processing backlog is at the
high watermark. ``422``/``413`` reject the request before anything is staged.

These tests run the real endpoints through ``TestClient`` with the database,
object storage, queue and admission gate stubbed out.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.api import ingestion as ingest_api
from app.core.security import require_api_key
from app.db.session import get_db
from app.main import app
from app.services import object_storage, upload_admission
from tests.test_local_source import factory, make_paper_with_sha256  # noqa: F401

PDF = b"%PDF-1.7\n" + b"batch upload payload\n" * 20
TXT = b"just some notes"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
class StorageRecorder:
    """Stub for the two object-storage calls the endpoint makes."""

    def __init__(self) -> None:
        self.uploads: list[dict] = []
        self.deleted: list[str] = []
        self.fail_with: Exception | None = None

    def upload_stream_hashed(
        self, key, fileobj, *, length, content_type=None, metadata=None
    ):
        if self.fail_with is not None:
            raise self.fail_with
        payload = fileobj.read()
        digest = hashlib.sha256(payload).hexdigest()
        self.uploads.append(
            {"key": key, "length": length, "sha256": digest, "bytes": payload}
        )
        return object_storage.StoredObject(
            bucket="paperbox",
            object_key=key,
            size_bytes=len(payload),
            content_type=content_type or "application/pdf",
            sha256=digest,
        )

    def upload_bytes(self, key, data, **kwargs):
        digest = hashlib.sha256(data).hexdigest()
        self.uploads.append({"key": key, "sha256": digest, "bytes": data})
        return object_storage.StoredObject(
            bucket="paperbox",
            object_key=key,
            size_bytes=len(data),
            content_type=kwargs.get("content_type") or "application/pdf",
            sha256=digest,
        )

    def delete_object(self, key, bucket=None) -> None:
        self.deleted.append(key)


class QueueRecorder:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str, int]] = []

    def __call__(self, session, job_id, kind="ingest", priority=0):
        self.submitted.append((job_id, kind, priority))
        return None


@pytest.fixture()
def storage(monkeypatch):
    recorder = StorageRecorder()
    monkeypatch.setattr(
        ingest_api.object_storage, "upload_stream_hashed", recorder.upload_stream_hashed
    )
    monkeypatch.setattr(ingest_api.object_storage, "upload_bytes", recorder.upload_bytes)
    monkeypatch.setattr(ingest_api.object_storage, "delete_object", recorder.delete_object)
    return recorder


@pytest.fixture()
def queued(monkeypatch):
    recorder = QueueRecorder()
    monkeypatch.setattr(ingest_api.job_queue, "submit", recorder)
    return recorder


@pytest.fixture()
def admission(monkeypatch):
    """An admission gate the test controls completely (no live queue)."""
    gate = upload_admission.UploadAdmission(limit=2, depth_provider=lambda: 0)
    monkeypatch.setattr(upload_admission, "get_admission", lambda: gate)
    monkeypatch.setattr(ingest_api.upload_admission, "get_admission", lambda: gate)
    return gate


@pytest.fixture()
def client(factory, storage, queued, admission):  # noqa: F811
    def _db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_api_key] = lambda: "test-key"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def post_files(client, entries, field: str = "files"):
    """``entries`` is a list of ``(filename, bytes, content_type)``."""
    return client.post(
        "/api/papers/ingest/files",
        files=[(field, (name, payload, kind)) for name, payload, kind in entries],
    )


# --------------------------------------------------------------------------- #
# happy paths
# --------------------------------------------------------------------------- #
def test_single_file_is_staged_and_queued(client, storage, queued):
    response = post_files(client, [("one.pdf", PDF, "application/pdf")])

    assert response.status_code == 202
    body = response.json()
    assert (body["accepted"], body["duplicate"], body["rejected"]) == (1, 0, 0)
    assert body["request_id"]

    item = body["results"][0]
    assert item["status"] == "accepted"
    assert item["job_id"]
    assert item["size_bytes"] == len(PDF)

    assert len(storage.uploads) == 1
    key = storage.uploads[0]["key"]
    assert key.startswith(f"uploads/{body['request_id']}/1-")
    assert key.endswith("one.pdf")
    assert storage.uploads[0]["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert storage.uploads[0]["bytes"] == PDF

    # One file is interactive: a human is waiting on it.
    assert len(queued.submitted) == 1
    assert queued.submitted[0][2] == 0


def test_multiple_files_are_queued_as_batch(client, storage, queued):
    response = post_files(
        client,
        [
            ("a.pdf", PDF, "application/pdf"),
            ("b.pdf", PDF + b"b", "application/pdf"),
            ("c.pdf", PDF + b"c", "application/pdf"),
        ],
    )

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] == 3
    assert [item["status"] for item in body["results"]] == [
        "accepted",
        "accepted",
        "accepted",
    ]
    assert len({item["job_id"] for item in body["results"]}) == 3
    assert [priority for _, _, priority in queued.submitted] == [1, 1, 1]
    assert [entry["key"].split("/")[2] for entry in storage.uploads] == [
        "1-a.pdf",
        "2-b.pdf",
        "3-c.pdf",
    ]


def test_same_filename_twice_still_gets_distinct_staging_keys(client, storage):
    response = post_files(
        client,
        [("same.pdf", PDF, "application/pdf"), ("same.pdf", PDF + b"x", "application/pdf")],
    )

    assert response.status_code == 202
    keys = [entry["key"] for entry in storage.uploads]
    assert len(set(keys)) == 2


# --------------------------------------------------------------------------- #
# per-part failure isolation
# --------------------------------------------------------------------------- #
def test_a_non_pdf_part_is_rejected_while_the_rest_go_through(client, storage, queued):
    response = post_files(
        client,
        [
            ("good.pdf", PDF, "application/pdf"),
            ("notes.txt", TXT, "text/plain"),
            ("also-good.pdf", PDF + b"z", "application/pdf"),
        ],
    )

    assert response.status_code == 202
    body = response.json()
    assert (body["accepted"], body["rejected"]) == (2, 1)

    rejected = body["results"][1]
    assert rejected["status"] == "rejected"
    assert rejected["error_code"] == "UNSUPPORTED_TYPE"
    assert rejected["job_id"] is None
    # The rejected part never reached object storage.
    assert len(storage.uploads) == 2
    assert len(queued.submitted) == 2


def test_an_empty_part_is_rejected(client, queued):
    response = post_files(
        client,
        [("empty.pdf", b"", "application/pdf"), ("ok.pdf", PDF, "application/pdf")],
    )

    body = response.json()
    assert body["rejected"] == 1
    assert body["results"][0]["status"] == "rejected"
    assert body["results"][0]["error_code"] == "UNSUPPORTED_TYPE"


def test_a_storage_failure_rejects_only_that_part(client, storage, queued):
    storage.fail_with = object_storage.ObjectStorageError("minio is down")

    response = post_files(client, [("a.pdf", PDF, "application/pdf")])

    assert response.status_code == 202
    body = response.json()
    assert body["rejected"] == 1
    assert body["results"][0]["error_code"] == "STORAGE_FAILED"
    assert queued.submitted == []


# --------------------------------------------------------------------------- #
# dedupe
# --------------------------------------------------------------------------- #
def test_duplicate_content_creates_no_job_for_a_new_paper(client, factory, storage, queued):  # noqa: F811
    digest = hashlib.sha256(PDF).hexdigest()
    existing_id = make_paper_with_sha256(factory, digest)

    response = post_files(client, [("copy.pdf", PDF, "application/pdf")])

    assert response.status_code == 202
    body = response.json()
    assert (body["accepted"], body["duplicate"]) == (0, 1)
    item = body["results"][0]
    assert item["status"] == "duplicate"
    assert item["paper_id"] == existing_id
    # The staged copy was dropped and no pipeline was started.
    assert storage.deleted == [storage.uploads[0]["key"]]
    assert queued.submitted == []


def test_duplicate_job_is_already_completed(client, factory, storage):  # noqa: F811
    from app.db.models import IngestionJob

    digest = hashlib.sha256(PDF).hexdigest()
    make_paper_with_sha256(factory, digest)

    body = post_files(client, [("copy.pdf", PDF, "application/pdf")]).json()
    job_id = body["results"][0]["job_id"]

    session = factory()
    try:
        job = session.get(IngestionJob, job_id)
        assert job.stage == "COMPLETED"
        assert job.finished_at is not None
        assert (job.payload or {}).get("duplicate") is True
    finally:
        session.close()


def test_duplicates_do_not_stop_the_new_files(client, factory, storage, queued):  # noqa: F811
    make_paper_with_sha256(factory, hashlib.sha256(PDF).hexdigest())

    response = post_files(
        client,
        [("old.pdf", PDF, "application/pdf"), ("new.pdf", PDF + b"new", "application/pdf")],
    )

    body = response.json()
    assert (body["accepted"], body["duplicate"]) == (1, 1)
    assert len(queued.submitted) == 1


# --------------------------------------------------------------------------- #
# request-level limits
# --------------------------------------------------------------------------- #
def test_too_many_files_is_a_422(client, storage, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ingest_max_files_per_request", 2)

    response = post_files(
        client,
        [(f"{index}.pdf", PDF, "application/pdf") for index in range(3)],
    )

    assert response.status_code == 422
    assert "INGEST_MAX_FILES_PER_REQUEST" in response.json()["detail"]
    assert storage.uploads == []


def test_oversized_request_is_a_413(client, storage, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ingest_max_request_mb", 1)
    big = PDF + b"x" * (1024 * 1024)

    response = post_files(client, [("big.pdf", big, "application/pdf")])

    assert response.status_code == 413
    assert storage.uploads == []


def test_oversized_single_file_is_rejected_per_part(client, storage, monkeypatch):
    # A limit that only the first part exceeds (the per-file ceiling is checked
    # before anything is staged).
    monkeypatch.setattr(
        ingest_api.ingest, "max_file_bytes", lambda: len(PDF) + 10
    )

    response = post_files(
        client,
        [("big.pdf", PDF + b"x" * 100, "application/pdf"), ("ok.pdf", PDF, "application/pdf")],
    )

    body = response.json()
    assert body["results"][0]["error_code"] == "OVERSIZED"
    assert body["accepted"] == 1
    # Only the acceptable part was staged.
    assert len(storage.uploads) == 1


# --------------------------------------------------------------------------- #
# admission (429 + Retry-After)
# --------------------------------------------------------------------------- #
def test_saturated_upload_concurrency_answers_429(client, admission):
    for _ in range(admission.limit):
        assert admission.try_acquire() is True

    response = post_files(client, [("a.pdf", PDF, "application/pdf")])

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(upload_admission.RETRY_AFTER_SECONDS)
    assert "concurrency" in response.json()["detail"]


def test_a_slot_is_released_after_a_successful_request(client, admission):
    post_files(client, [("a.pdf", PDF, "application/pdf")])

    assert admission.in_flight == 0


def test_a_slot_is_released_after_a_rejected_request(client, admission):
    post_files(client, [("nope.txt", TXT, "text/plain")])

    assert admission.in_flight == 0


def test_backlog_watermark_refuses_multi_file_requests(client, admission, monkeypatch):
    monkeypatch.setattr(admission, "high_watermark", 5)
    monkeypatch.setattr(admission, "_depth_provider", lambda: 5)

    response = post_files(
        client,
        [("a.pdf", PDF, "application/pdf"), ("b.pdf", PDF + b"b", "application/pdf")],
    )

    assert response.status_code == 429
    assert "backlog" in response.json()["detail"]


def test_backlog_watermark_lets_a_single_file_through(client, admission, storage, monkeypatch):
    monkeypatch.setattr(admission, "high_watermark", 5)
    monkeypatch.setattr(admission, "_depth_provider", lambda: 99)

    response = post_files(client, [("a.pdf", PDF, "application/pdf")])

    assert response.status_code == 202
    assert response.json()["accepted"] == 1
