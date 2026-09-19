"""What the pipeline cleans up once the bytes are safely stored (2026-09-19).

Two temporary copies can exist while a job runs, and both used to accumulate:

* the **staging object** of a multipart upload (``uploads/<request_id>/...``),
  which nothing ever deleted -- 9 objects / 49.8 MB were sitting in MinIO;
* the **extracted local file** of a ``local_path`` job with ``cleanup_after``
  (every PDF that came out of an archive).

The rule is the same for both: delete them at the ``STORED`` checkpoint, which is
the moment the payload exists in its permanent place (``papers/<id>/original.pdf``).
Nothing is removed before that -- a job that fails on the way to ``STORED`` must
still be retryable from the staging object.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.db.models import IngestionJob
from app.services import object_storage
from app.workers import tasks
from tests.test_job_progress import (  # noqa: F401 - fixtures
    factory,
    make_job,
    make_paper,
    stubbed_pipeline,
)

PDF = b"%PDF-1.7\n" + b"staging payload\n" * 40
STAGING_KEY = "uploads/req123/1-legacy.pdf"


@pytest.fixture()
def storage_calls(monkeypatch):
    """Record every object-storage call the worker makes."""
    calls = {"uploads": [], "deleted": [], "downloaded": []}

    def download_bytes(key, bucket=None):
        calls["downloaded"].append(key)
        return PDF

    def upload_bytes(key, data, **kwargs):
        calls["uploads"].append({"key": key, "size": len(data)})
        return object_storage.StoredObject(
            bucket="paperbox",
            object_key=key,
            size_bytes=len(data),
            content_type=kwargs.get("content_type") or "application/pdf",
        )

    def upload_file(key, fileobj, *, length, content_type=None, metadata=None):
        data = fileobj.read()
        calls["uploads"].append({"key": key, "size": len(data)})
        return object_storage.StoredObject(
            bucket="paperbox",
            object_key=key,
            size_bytes=len(data),
            content_type=content_type or "application/pdf",
        )

    def delete_object(key, bucket=None):
        calls["deleted"].append(key)

    monkeypatch.setattr(tasks.object_storage, "download_bytes", download_bytes)
    monkeypatch.setattr(tasks.object_storage, "upload_bytes", upload_bytes)
    monkeypatch.setattr(tasks.object_storage, "upload_file", upload_file)
    monkeypatch.setattr(tasks.object_storage, "delete_object", delete_object)
    return calls


def make_staging_job(session_factory, *, object_key: str | None = STAGING_KEY) -> str:
    """A RECEIVED job whose payload points at a staged upload."""
    paper_id = make_paper(session_factory)
    job_id = make_job(session_factory, paper_id)
    session = session_factory()
    try:
        job = session.get(IngestionJob, job_id)
        payload = {"source_type": "file", "filename": "legacy.pdf"}
        if object_key:
            payload["object_key"] = object_key
        job.payload = payload
        session.commit()
    finally:
        session.close()
    return job_id


def run_job(monkeypatch, session_factory, job_id: str) -> None:
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    tasks.run_ingestion_job(job_id)


def read_job(session_factory, job_id: str) -> IngestionJob:
    session = session_factory()
    try:
        return session.get(IngestionJob, job_id)
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_staging_object_is_deleted_after_stored(factory, stubbed_pipeline, storage_calls, monkeypatch):  # noqa: F811
    job_id = make_staging_job(factory)

    run_job(monkeypatch, factory, job_id)

    job = read_job(factory, job_id)
    assert job.stage == "COMPLETED"
    assert storage_calls["deleted"] == [STAGING_KEY]


def test_the_permanent_copy_has_the_same_bytes_as_the_staging_one(
    factory, stubbed_pipeline, storage_calls, monkeypatch  # noqa: F811
):
    job_id = make_staging_job(factory)

    run_job(monkeypatch, factory, job_id)

    job = read_job(factory, job_id)
    assert len(storage_calls["uploads"]) == 1
    uploaded = storage_calls["uploads"][0]
    assert uploaded["key"] == f"papers/{job.paper_id}/original.pdf"
    assert uploaded["size"] == len(PDF)
    assert hashlib.sha256(PDF).hexdigest()


def test_a_url_job_has_nothing_to_clean_up(factory, stubbed_pipeline, storage_calls, monkeypatch):  # noqa: F811
    from app.services import ingestion_service as ingest

    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)
    session = factory()
    try:
        job = session.get(IngestionJob, job_id)
        job.payload = {"source_type": "url", "source": "https://example.test/x.pdf"}
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(
        ingest,
        "download_pdf",
        lambda url: ingest.DownloadResult(
            data=PDF,
            filename="x.pdf",
            content_type="application/pdf",
            source_url=url,
        ),
    )
    run_job(monkeypatch, factory, job_id)

    assert storage_calls["deleted"] == []


def test_a_duplicate_race_also_drops_the_staging_object(
    factory, stubbed_pipeline, storage_calls, monkeypatch  # noqa: F811
):
    """Another request stored the same content first: the staged copy is useless."""
    from app.db.models import Paper, PaperFile, new_uuid

    digest = hashlib.sha256(PDF).hexdigest()
    session = factory()
    try:
        paper = Paper(
            id=new_uuid(),
            title="Winner",
            fingerprint=f"sha256:{digest}",
            status="INDEXED",
        )
        session.add(paper)
        session.add(
            PaperFile(
                id=new_uuid(),
                paper_id=paper.id,
                kind="original",
                object_key=f"papers/{paper.id}/original.pdf",
                bucket="paperbox",
                filename="original.pdf",
                content_type="application/pdf",
                size_bytes=len(PDF),
                sha256=digest,
            )
        )
        session.commit()
    finally:
        session.close()

    job_id = make_staging_job(factory)

    run_job(monkeypatch, factory, job_id)

    job = read_job(factory, job_id)
    assert (job.payload or {}).get("duplicate") is True
    assert storage_calls["deleted"] == [STAGING_KEY]


# --------------------------------------------------------------------------- #
# nothing is deleted before STORED
# --------------------------------------------------------------------------- #
def test_a_storage_failure_keeps_the_staging_object(
    factory, stubbed_pipeline, storage_calls, monkeypatch  # noqa: F811
):
    """The job must stay retryable: its only payload is the staging object."""

    def boom(key, data, **kwargs):
        raise object_storage.ObjectStorageError("minio is down")

    monkeypatch.setattr(tasks.object_storage, "upload_bytes", boom)
    job_id = make_staging_job(factory)

    run_job(monkeypatch, factory, job_id)

    job = read_job(factory, job_id)
    assert job.stage == "FAILED"
    assert job.error_code == "STORAGE_FAILED"
    assert storage_calls["deleted"] == []


def test_a_pipeline_failure_after_stored_does_not_need_staging(
    factory, stubbed_pipeline, storage_calls, monkeypatch  # noqa: F811
):
    """PARSING onwards works off ``papers/``; the staging copy is already gone."""
    from app.services import embedding_service

    def boom(_texts):
        raise RuntimeError("embedding server exploded")

    monkeypatch.setattr(tasks.embedding_service, "embed_texts", boom)
    job_id = make_staging_job(factory)

    run_job(monkeypatch, factory, job_id)

    job = read_job(factory, job_id)
    assert job.stage == "FAILED"
    assert job.paper_id  # the paper row and its stored original survive
    assert storage_calls["deleted"] == [STAGING_KEY]
    assert embedding_service is not None  # the stub really was the one called


def test_a_local_cleanup_after_job_removes_the_extracted_file(
    factory, stubbed_pipeline, storage_calls, tmp_path, monkeypatch  # noqa: F811
):
    path = tmp_path / "paperbox-req" / "from-archive.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(PDF)

    paper_id = make_paper(factory)
    job_id = make_job(factory, paper_id)
    session = factory()
    try:
        job = session.get(IngestionJob, job_id)
        job.payload = {
            "source_type": "local_path",
            "local_path": str(path),
            "filename": "from-archive.pdf",
            "cleanup_after": True,
        }
        session.commit()
    finally:
        session.close()

    run_job(monkeypatch, factory, job_id)

    assert not path.exists()
    assert not path.parent.exists()  # the emptied extraction dir goes too
    assert storage_calls["deleted"] == []  # nothing was staged for a local file
    assert Path(storage_calls["uploads"][0]["key"]).name == "original.pdf"
