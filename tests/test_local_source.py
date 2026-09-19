"""The ``local_path`` source type: a PDF that already sits on this machine (2026-09-19).

``/ingest/dir`` and ``/ingest/compressed`` never receive bytes -- the server
reads the file itself, so there is no staging object to download. The pipeline
therefore needs a third source type next to ``url`` and ``file``:

* ``local_path`` -- the payload carries an absolute path; the worker reads it,
  dedupes on a streaming SHA256, uploads it straight to
  ``papers/<paper_id>/original.pdf`` and, when ``cleanup_after`` is set (files
  extracted from an archive), deletes the local file once the bytes are safely
  in object storage.

These tests drive ``run_ingestion_job`` end to end against a private in-memory
SQLite database with every external collaborator stubbed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.models import IngestionJob, Paper, PaperFile, new_uuid
from app.services import ingestion_service as ingest
from app.services import object_storage
from app.workers import tasks
from tests.test_job_progress import (  # noqa: F401 - fixtures
    factory,
    make_job,
    make_paper,
    stubbed_pipeline,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
PDF_BYTES = b"%PDF-1.7\nlocal source payload\n" + b"body " * 200


def write_pdf(directory: Path, name: str = "paper.pdf", payload: bytes = PDF_BYTES) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def make_local_job(
    session_factory,
    local_path: Path,
    *,
    cleanup_after: bool = False,
    payload_extra: dict | None = None,
) -> str:
    """Insert a RECEIVED job whose payload points at a server-side file."""
    session = session_factory()
    try:
        payload = {
            "source_type": "local_path",
            "local_path": str(local_path),
            "filename": local_path.name,
            "content_type": "application/pdf",
            "size_bytes": local_path.stat().st_size if local_path.exists() else 0,
            "cleanup_after": cleanup_after,
        }
        payload.update(payload_extra or {})
        job = IngestionJob(
            id=new_uuid(),
            kind="ingest",
            stage="RECEIVED",
            progress=0.0,
            payload=payload,
        )
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


def make_paper_with_sha256(session_factory, digest: str) -> str:
    """Insert a live paper whose stored original hashes to ``digest``."""
    session = session_factory()
    try:
        paper = Paper(
            id=new_uuid(),
            title="Already Here",
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
                size_bytes=10,
                sha256=digest,
            )
        )
        session.commit()
        return paper.id
    finally:
        session.close()


@pytest.fixture()
def local_uploads(monkeypatch):
    """Record every ``papers/<id>/original.pdf`` upload the worker performs."""
    calls: list[dict] = []

    def upload_file(key, fileobj, *, length, content_type=None, metadata=None):
        payload = fileobj.read()
        calls.append(
            {
                "key": key,
                "length": length,
                "bytes": payload,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "content_type": content_type,
                "metadata": metadata,
            }
        )
        return object_storage.StoredObject(
            bucket="paperbox",
            object_key=key,
            size_bytes=len(payload),
            content_type=content_type or "application/pdf",
        )

    monkeypatch.setattr(tasks.object_storage, "upload_file", upload_file)
    monkeypatch.setattr(
        tasks.object_storage,
        "upload_bytes",
        lambda key, data, **kw: object_storage.StoredObject(
            bucket="paperbox",
            object_key=key,
            size_bytes=len(data),
            content_type=kw.get("content_type") or "application/pdf",
        ),
    )
    return calls


def run_job(monkeypatch, session_factory, job_id: str) -> None:
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)
    tasks.run_ingestion_job(job_id)


def read_job(session_factory, job_id: str) -> IngestionJob:
    session = session_factory()
    try:
        return session.get(IngestionJob, job_id)
    finally:
        session.close()


def count_papers(session_factory) -> int:
    session = session_factory()
    try:
        return int(session.execute(select(func.count()).select_from(Paper)).scalar_one())
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_local_file_is_uploaded_and_the_job_completes(
    factory, stubbed_pipeline, local_uploads, tmp_path, monkeypatch  # noqa: F811
):
    path = write_pdf(tmp_path, "local-ok.pdf")
    job_id = make_local_job(factory, path)

    run_job(monkeypatch, factory, job_id)

    job = read_job(factory, job_id)
    assert job.stage == "COMPLETED"
    assert job.paper_id

    assert len(local_uploads) == 1
    uploaded = local_uploads[0]
    assert uploaded["key"] == f"papers/{job.paper_id}/original.pdf"
    assert uploaded["length"] == path.stat().st_size
    assert uploaded["sha256"] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert uploaded["bytes"] == PDF_BYTES


def test_local_file_survives_when_cleanup_is_not_requested(
    factory, stubbed_pipeline, local_uploads, tmp_path, monkeypatch  # noqa: F811
):
    path = write_pdf(tmp_path, "keep-me.pdf")

    run_job(monkeypatch, factory, make_local_job(factory, path, cleanup_after=False))

    assert path.exists()


def test_cleanup_after_removes_the_local_file(
    factory, stubbed_pipeline, local_uploads, tmp_path, monkeypatch  # noqa: F811
):
    path = write_pdf(tmp_path, "extracted.pdf")

    run_job(monkeypatch, factory, make_local_job(factory, path, cleanup_after=True))

    assert not path.exists()
    assert len(local_uploads) == 1


def test_cleanup_after_prunes_the_emptied_directory(
    factory, stubbed_pipeline, local_uploads, tmp_path, monkeypatch  # noqa: F811
):
    directory = tmp_path / "paperbox-req"
    path = write_pdf(directory, "only.pdf")

    run_job(monkeypatch, factory, make_local_job(factory, path, cleanup_after=True))

    assert not path.exists()
    assert not directory.exists()


def test_cleanup_after_keeps_a_directory_that_still_has_files(
    factory, stubbed_pipeline, local_uploads, tmp_path, monkeypatch  # noqa: F811
):
    directory = tmp_path / "paperbox-req2"
    path = write_pdf(directory, "one.pdf")
    sibling = write_pdf(directory, "two.pdf", payload=b"%PDF-1.7 second\n")

    run_job(monkeypatch, factory, make_local_job(factory, path, cleanup_after=True))

    assert not path.exists()
    assert sibling.exists()


# --------------------------------------------------------------------------- #
# dedupe
# --------------------------------------------------------------------------- #
def test_duplicate_content_creates_no_new_paper_and_cleans_up(
    factory, stubbed_pipeline, local_uploads, tmp_path, monkeypatch  # noqa: F811
):
    digest = hashlib.sha256(PDF_BYTES).hexdigest()
    existing_id = make_paper_with_sha256(factory, digest)
    before = count_papers(factory)
    path = write_pdf(tmp_path, "same-content.pdf")
    job_id = make_local_job(factory, path, cleanup_after=True)

    run_job(monkeypatch, factory, job_id)

    job = read_job(factory, job_id)
    assert job.stage == "COMPLETED"
    assert job.paper_id == existing_id
    assert (job.payload or {}).get("duplicate") is True
    assert count_papers(factory) == before
    assert local_uploads == []
    assert not path.exists()


# --------------------------------------------------------------------------- #
# failures
# --------------------------------------------------------------------------- #
def test_missing_local_file_fails_with_download_failed(
    factory, stubbed_pipeline, local_uploads, tmp_path, monkeypatch  # noqa: F811
):
    path = tmp_path / "vanished.pdf"
    job_id = make_local_job(factory, path)

    run_job(monkeypatch, factory, job_id)

    job = read_job(factory, job_id)
    assert job.stage == "FAILED"
    assert job.error_code == "DOWNLOAD_FAILED"


def test_directory_instead_of_file_fails_with_download_failed(
    factory, stubbed_pipeline, local_uploads, tmp_path, monkeypatch  # noqa: F811
):
    directory = tmp_path / "a-directory.pdf"
    directory.mkdir()
    job_id = make_local_job(factory, directory)

    run_job(monkeypatch, factory, job_id)

    job = read_job(factory, job_id)
    assert job.stage == "FAILED"
    assert job.error_code == "DOWNLOAD_FAILED"


def test_oversized_local_file_fails_with_oversized(
    factory, stubbed_pipeline, local_uploads, tmp_path, monkeypatch  # noqa: F811
):
    from app.core.config import settings

    path = write_pdf(tmp_path, "huge.pdf")
    job_id = make_local_job(
        factory,
        path,
        payload_extra={"size_bytes": (settings.ingest_max_file_mb + 1) * 1024 * 1024},
    )
    # Make the on-disk file genuinely oversized without writing 100 MB.
    monkeypatch.setattr(ingest, "max_file_bytes", lambda: 16)
    monkeypatch.setattr(tasks.ingest, "max_file_bytes", lambda: 16)

    run_job(monkeypatch, factory, job_id)

    job = read_job(factory, job_id)
    assert job.stage == "FAILED"
    assert job.error_code == "OVERSIZED"


def test_non_pdf_local_file_fails_with_unsupported_type(
    factory, stubbed_pipeline, local_uploads, tmp_path, monkeypatch  # noqa: F811
):
    path = write_pdf(tmp_path, "notes.txt", payload=b"just text")
    job_id = make_local_job(factory, path, payload_extra={"content_type": "text/plain"})

    run_job(monkeypatch, factory, job_id)

    job = read_job(factory, job_id)
    assert job.stage == "FAILED"
    assert job.error_code == "UNSUPPORTED_TYPE"


def test_empty_local_file_fails(
    factory, stubbed_pipeline, local_uploads, tmp_path, monkeypatch  # noqa: F811
):
    path = write_pdf(tmp_path, "empty.pdf", payload=b"")
    job_id = make_local_job(factory, path)

    run_job(monkeypatch, factory, job_id)

    assert read_job(factory, job_id).stage == "FAILED"


# --------------------------------------------------------------------------- #
# payload validation
# --------------------------------------------------------------------------- #
def test_create_job_carries_the_local_payload_fields(factory):  # noqa: F811
    session = factory()
    try:
        job = ingest.create_job(
            session,
            source_type=ingest.SOURCE_TYPE_LOCAL,
            filename="a.pdf",
            size_bytes=12,
            payload={"local_path": "/data/a.pdf", "cleanup_after": True},
        )
        session.commit()
        payload = dict(job.payload)
    finally:
        session.close()

    assert payload["source_type"] == "local_path"
    assert payload["local_path"] == "/data/a.pdf"
    assert payload["cleanup_after"] is True
    assert payload["filename"] == "a.pdf"


def test_create_job_rejects_a_local_source_without_a_path(factory):  # noqa: F811
    session = factory()
    try:
        with pytest.raises(ingest.UnsupportedSource):
            ingest.create_job(session, source_type=ingest.SOURCE_TYPE_LOCAL)
    finally:
        session.close()


def test_create_job_rejects_a_relative_local_path(factory):  # noqa: F811
    session = factory()
    try:
        with pytest.raises(ingest.UnsupportedSource):
            ingest.create_job(
                session,
                source_type=ingest.SOURCE_TYPE_LOCAL,
                payload={"local_path": "papers/relative.pdf"},
            )
    finally:
        session.close()


def test_create_job_leaves_the_other_source_types_alone(factory):  # noqa: F811
    session = factory()
    try:
        job = ingest.create_job(session, source_type="url", source="https://x/y.pdf")
        session.commit()
        assert dict(job.payload)["source_type"] == "url"
    finally:
        session.close()
