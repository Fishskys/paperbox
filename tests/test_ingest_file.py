"""Regression tests for the original single-file endpoint (2026-09-19).

``POST /api/papers/ingest/file`` is the endpoint the WebUI and the scripts have
always used. It was rewritten to share the streaming staging path with
``/ingest/files`` (hash while writing, dedupe by content, one queue entry), so
these tests pin the parts of its contract callers depend on:

* the response is still ``IngestAccepted`` (``job_id``/``status``/``stage``/...);
* a bad file is still a ``422`` on the request itself;
* a duplicate still comes back as a normal ``202`` with ``duplicate=true``;
* the bytes still land in ``uploads/<request_id>/`` staging, and the job payload
  still points at the staging object (which the pipeline then deletes at STORED).
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import func, select

from app.db.models import IngestionJob, Paper
from app.services import upload_admission
from tests.test_ingest_files import (  # noqa: F401 - fixtures
    PDF,
    TXT,
    admission,
    client,
    factory,
    queued,
    storage,
)
from tests.test_local_source import make_paper_with_sha256  # noqa: F401 - fixture


def post_single(client, name: str, payload: bytes, content_type: str):
    return client.post(
        "/api/papers/ingest/file",
        files={"file": (name, payload, content_type)},
    )


def count(session_factory, model) -> int:
    session = session_factory()
    try:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #
def test_response_shape_is_unchanged(client, storage, queued):
    response = post_single(client, "legacy.pdf", PDF, "application/pdf")

    assert response.status_code == 202
    body = response.json()
    assert set(body) >= {
        "job_id",
        "paper_id",
        "status",
        "duplicate",
        "stage",
        "created_at",
        "message",
    }
    assert body["status"] == "RECEIVED"
    assert body["duplicate"] is False
    assert body["stage"] == "RECEIVED"
    assert body["message"]


def test_the_upload_is_staged_under_a_request_id(client, storage, queued):
    body = post_single(client, "legacy.pdf", PDF, "application/pdf").json()

    assert body["job_id"]
    assert len(storage.uploads) == 1
    key = storage.uploads[0]["key"]
    assert key.startswith("uploads/")
    assert key.endswith("legacy.pdf")
    assert storage.uploads[0]["sha256"] == hashlib.sha256(PDF).hexdigest()


def test_the_job_payload_points_at_the_staging_object(client, storage, factory):  # noqa: F811
    body = post_single(client, "legacy.pdf", PDF, "application/pdf").json()

    session = factory()
    try:
        job = session.get(IngestionJob, body["job_id"])
        payload = dict(job.payload)
    finally:
        session.close()

    assert payload["source_type"] == "file"
    assert payload["object_key"] == storage.uploads[0]["key"]
    assert payload["filename"] == "legacy.pdf"


def test_the_job_is_queued_as_interactive(client, queued):
    post_single(client, "legacy.pdf", PDF, "application/pdf")

    assert len(queued.submitted) == 1
    assert queued.submitted[0][2] == 0


# --------------------------------------------------------------------------- #
# rejections
# --------------------------------------------------------------------------- #
def test_a_non_pdf_is_still_a_422(client, storage, queued):
    response = post_single(client, "notes.txt", TXT, "text/plain")

    assert response.status_code == 422
    assert "PDF" in response.json()["detail"]
    assert storage.uploads == []
    assert queued.submitted == []


def test_an_empty_file_is_still_a_422(client):
    response = post_single(client, "empty.pdf", b"", "application/pdf")

    assert response.status_code == 422


def test_an_oversized_file_is_still_a_422(client, monkeypatch):
    from app.api import ingestion as ingest_api

    monkeypatch.setattr(ingest_api.ingest, "max_file_bytes", lambda: 16)

    response = post_single(client, "big.pdf", PDF, "application/pdf")

    assert response.status_code == 422
    assert "large" in response.json()["detail"].lower()


def test_a_saturated_upload_gate_is_a_429(client, admission):
    for _ in range(admission.limit):
        admission.try_acquire()

    response = post_single(client, "legacy.pdf", PDF, "application/pdf")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(upload_admission.RETRY_AFTER_SECONDS)


# --------------------------------------------------------------------------- #
# dedupe
# --------------------------------------------------------------------------- #
def test_a_duplicate_is_reported_without_creating_a_paper(client, factory, storage):  # noqa: F811
    existing_id = make_paper_with_sha256(factory, hashlib.sha256(PDF).hexdigest())
    before = count(factory, Paper)

    response = post_single(client, "copy.pdf", PDF, "application/pdf")

    assert response.status_code == 202
    body = response.json()
    assert body["duplicate"] is True
    assert body["paper_id"] == existing_id
    assert body["stage"] == "COMPLETED"
    assert count(factory, Paper) == before
    # The staged copy was dropped instead of being left for the pipeline.
    assert storage.deleted == [storage.uploads[0]["key"]]
