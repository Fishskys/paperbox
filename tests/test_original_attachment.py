"""Targeted original upload: SQLite, fake storage and an isolated pipeline."""

import hashlib

import pytest
from sqlalchemy import func, select

from app.db.models import IngestionJob, Paper, PaperFile, new_uuid
from app.services import ingestion_service as ingest
from app.workers import tasks
from app.parsing.pdf import EmbeddedMetadata
from app.services import original_attachment
from tests.test_ingest_files import (  # noqa: F401
    PDF, admission, client, factory, queued, storage,
)
from tests.test_job_progress import stubbed_pipeline  # noqa: F401
from tests.test_local_source import make_paper_with_sha256


def shell(factory):
    with factory() as session:
        paper = Paper(id=new_uuid(), title="Imported title", doi="10.1234/imported",
                      fingerprint=f"sha256:{new_uuid()}", status="AWAITING_FILE")
        session.add(paper)
        session.commit()
        return paper.id


def upload(client, paper_id, data=PDF):
    return client.post(f"/api/papers/{paper_id}/file",
                       files={"file": ("original.pdf", data, "application/pdf")})


def test_upload_is_bound_to_the_selected_shell(client, factory, storage, queued):
    paper_id = shell(factory)
    response = upload(client, paper_id)
    assert response.status_code == 202
    assert response.json()["paper_id"] == paper_id
    with factory() as session:
        job = session.get(IngestionJob, response.json()["job_id"])
        assert job.paper_id == paper_id
        assert job.payload["target_paper_id"] == paper_id
        assert job.payload["object_key"] == storage.uploads[0]["key"]
        assert session.get(Paper, paper_id).status == "AWAITING_FILE"
    assert len(queued.submitted) == 1


def test_missing_paper_and_existing_original_are_rejected_before_staging(client, factory, storage):
    assert upload(client, "not-a-uuid").status_code == 404
    existing = make_paper_with_sha256(factory, hashlib.sha256(PDF).hexdigest())
    assert upload(client, existing).status_code == 409
    assert storage.uploads == []


def test_content_owned_by_another_paper_does_not_rebind_the_job(client, factory, storage):
    paper_id = shell(factory)
    make_paper_with_sha256(factory, hashlib.sha256(PDF).hexdigest())
    response = upload(client, paper_id)
    assert response.status_code == 409
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(IngestionJob)) == 0
        assert session.get(Paper, paper_id).status == "AWAITING_FILE"
    assert storage.deleted == [storage.uploads[0]["key"]]


def test_second_upload_while_attachment_is_queued_is_rejected(client, factory, storage):
    paper_id = shell(factory)
    assert upload(client, paper_id).status_code == 202
    assert upload(client, paper_id).status_code == 409
    assert len(storage.uploads) == 1


def test_bad_pdf_type_and_saturated_gate_keep_existing_contract(client, factory, admission):
    paper_id = shell(factory)
    response = client.post(f"/api/papers/{paper_id}/file",
                           files={"file": ("x.txt", b"bad", "text/plain")})
    assert response.status_code == 422
    for _ in range(admission.limit):
        admission.try_acquire()
    assert upload(client, paper_id).status_code == 429


def test_worker_uses_existing_id_and_never_automatches_to_another_paper(
    client, factory, storage, queued, stubbed_pipeline, monkeypatch,
):
    paper_id = shell(factory)
    job_id = upload(client, paper_id).json()["job_id"]
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(tasks, "_match_existing_paper", lambda *a: pytest.fail("must not auto-match"))
    monkeypatch.setattr(tasks, "extract_embedded_metadata", lambda data: type("Meta", (), {"doi": None, "arxiv_id": None})())
    tasks.run_ingestion_job(job_id)
    with factory() as session:
        job = session.get(IngestionJob, job_id)
        assert job.stage == "COMPLETED", job.error_message
        assert job.paper_id == paper_id
        assert session.scalar(select(func.count()).select_from(Paper)) == 1
        paper = session.get(Paper, paper_id)
        assert paper.title == "Imported title"
        assert paper.doi == "10.1234/imported"
        assert paper.status == "INDEXED"
        assert session.scalar(select(PaperFile.paper_id)) == paper_id


def test_retry_before_original_is_stored_repeats_ingestion(factory, monkeypatch):
    paper_id = shell(factory)
    with factory() as session:
        job = ingest.create_job(session, source_type="file", payload={"target_paper_id": paper_id})
        job.paper_id = paper_id
        session.commit()
        job_id = job.id
    calls = []
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(tasks, "run_ingestion_job", lambda job_id: calls.append("ingest"))
    monkeypatch.setattr(tasks, "run_reindex_job", lambda job_id: calls.append("reindex"))
    tasks.run_retry_job(job_id)
    assert calls == ["ingest"]


def test_conflicting_embedded_identifier_fails_before_storage(
    client, factory, storage, queued, stubbed_pipeline, monkeypatch,
):
    paper_id = shell(factory)
    job_id = upload(client, paper_id).json()["job_id"]
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(tasks, "extract_embedded_metadata", lambda data: EmbeddedMetadata(doi="10.1234/other"))
    tasks.run_ingestion_job(job_id)
    with factory() as session:
        job = session.get(IngestionJob, job_id)
        assert job.stage == "FAILED"
        assert "不一致" in job.error_message
        assert session.get(Paper, paper_id).status == "AWAITING_FILE"
        assert session.scalar(select(func.count()).select_from(PaperFile)) == 0


def test_stored_original_survives_parse_failure_and_retry(
    client, factory, storage, queued, stubbed_pipeline, monkeypatch,
):
    paper_id = shell(factory)
    job_id = upload(client, paper_id).json()["job_id"]
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(tasks, "extract_embedded_metadata", lambda data: EmbeddedMetadata())
    parse = tasks.parser_service.parse_paper_file
    def fail(*args, **kwargs):
        raise RuntimeError("parser offline")
    monkeypatch.setattr(tasks.parser_service, "parse_paper_file", fail)
    tasks.run_ingestion_job(job_id)
    with factory() as session:
        job = session.get(IngestionJob, job_id)
        assert job.stage == "FAILED"
        assert job.payload["attachment_stored"] is True
        assert session.get(Paper, paper_id).status == "FAILED"
        assert session.scalar(select(func.count()).select_from(PaperFile)) == 1
        ingest.prepare_retry(session, job_id)
    monkeypatch.setattr(tasks.parser_service, "parse_paper_file", parse)
    tasks.run_retry_job(job_id)
    with factory() as session:
        assert session.get(IngestionJob, job_id).stage == "COMPLETED"
        assert session.get(Paper, paper_id).status == "INDEXED"
        assert session.scalar(select(func.count()).select_from(Paper)) == 1
        assert session.scalar(select(func.count()).select_from(PaperFile)) == 1


def test_identifier_already_owned_by_another_paper_is_rejected(factory):
    paper_id = shell(factory)
    other_id = shell(factory)
    with factory() as session:
        paper = session.get(Paper, paper_id)
        paper.doi = None
        with pytest.raises(original_attachment.AttachmentError, match=other_id):
            original_attachment.validate_identifiers(session, paper, EmbeddedMetadata(doi="10.1234/imported"))
