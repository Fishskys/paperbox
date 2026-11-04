"""Maintenance uses SQLite/stubs and never opens real stores."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.security import require_admin
from app.db.models import IngestionJob
from app.db.session import get_db
from app.main import app
from app.schemas.maintenance import MaintenanceIn
from app.services import ingestion_service as ingest, maintenance_service as service
from app.workers import queue
from tests.test_metadata_merge import make_paper
from tests.test_auth_rest import key_material  # noqa: F401


@pytest.fixture(autouse=True)
def queued(monkeypatch):
    def submit(session, job_id, kind, priority=1):
        job = session.get(IngestionJob, job_id)
        job.stage = "QUEUED"
        session.commit()
        return job
    monkeypatch.setattr(queue, "submit", submit)


def body(**kwargs):
    return MaintenanceIn(request_id=uuid4(), operation="metadata", **kwargs)


def test_idempotency_and_parameter_conflicts(db_session):
    request = body()
    job = service.submit(db_session, request, actor="admin")
    assert service.submit(db_session, request, actor="admin").id == job.id
    assert db_session.scalar(select(func.count()).select_from(IngestionJob)) == 1
    with pytest.raises(service.MaintenanceError, match="different parameters"):
        service.submit(db_session, request.model_copy(update={"limit": 21}), actor="admin")
    assert service.submit(db_session, request, actor="other-admin").id != job.id


def test_preview_apply_freezes_scope_and_detects_concurrent_edits(db_session):
    paper = make_paper(db_session)
    db_session.commit()
    preview = service.submit(db_session, body(paper_id=paper.id), actor="admin")
    service.execute(db_session, preview.id)
    assert preview.stage == "COMPLETED"
    assert preview.payload["report"]["skipped_no_file"] == 1
    apply = service.submit(db_session, body(paper_id=paper.id, dry_run=False, preview_job_id=preview.id), actor="admin")
    paper.title = "Changed after preview"
    db_session.commit()
    service.execute(db_session, apply.id)
    assert apply.payload["report"]["failed"] == 1
    assert "changed since preview" in apply.payload["report"]["errors"][0]["error"]
    assert paper.title == "Changed after preview"
    with pytest.raises(service.MaintenanceError, match="matching preview"):
        service.submit(db_session, body(limit=100, dry_run=False, preview_job_id=preview.id), actor="admin")


def test_restart_retains_partial_report_without_replaying_apply(db_session):
    paper = make_paper(db_session)
    db_session.commit()
    job = service.submit(db_session, body(paper_id=paper.id), actor="admin")
    service.persist(db_session, job.id, progress=50, report={"items": [{"paper_id": paper.id}]})
    _, interrupted = ingest.recover_jobs(db_session)
    assert interrupted == [job.id]
    assert service.serialize(job)["report_available"]
    assert job.error_code == "INTERRUPTED"
    service.execute(db_session, job.id)
    assert job.stage == "FAILED"
    assert queue.kind_for_payload(job.payload) == "maintenance"


def test_consistency_progress_and_persisted_report(db_session, monkeypatch):
    from app.services import consistency_service
    class Report:
        def as_dict(self): return {"consistent": False, "errors": ["minio unavailable"]}
    def check(**kwargs):
        for progress in (30, 55, 80): kwargs["on_progress"](progress)
        return Report()
    monkeypatch.setattr(consistency_service, "check_consistency", check)
    job = service.submit(db_session, MaintenanceIn(request_id=uuid4(), operation="consistency"), actor="admin")
    service.execute(db_session, job.id)
    assert job.progress == 100
    assert job.payload["report"]["errors"] == ["minio unavailable"]


def test_api_recovery_and_report_download(session_factory):
    from types import SimpleNamespace
    def db():
        with session_factory() as session: yield session
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(prefix="admin")
    try:
        client = TestClient(app)
        request_id = str(uuid4())
        result = client.post("/api/maintenance/jobs", json={"request_id": request_id, "operation": "metadata"})
        assert result.status_code == 202
        job_id = result.json()["job_id"]
        assert client.get(f"/api/maintenance/jobs/requests/{request_id}").json()["job_id"] == job_id
        assert client.get(f"/api/maintenance/jobs/{job_id}/report").status_code == 409
        with session_factory() as session: service.execute(session, job_id)
        response = client.get(f"/api/maintenance/jobs/{job_id}/report")
        assert response.status_code == 200 and "attachment" in response.headers["Content-Disposition"]
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409
        assert client.post("/api/maintenance/jobs", json={"request_id": str(uuid4()), "operation": "metadata", "dry_run": False}).status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("role", ["read", "write"])
def test_task_and_report_endpoints_require_admin(key_material, role):
    from fastapi import FastAPI
    from app.api.maintenance import router
    from app.main import AuthContextMiddleware
    isolated = FastAPI()
    isolated.include_router(router)
    isolated.add_middleware(AuthContextMiddleware)
    client = TestClient(isolated)
    headers = {"Authorization": f"Bearer {key_material[role]}"}
    for path in (f"/api/maintenance/jobs/{uuid4()}", f"/api/maintenance/jobs/{uuid4()}/report"):
        assert client.get(path, headers=headers).status_code == 403
    assert client.post("/api/maintenance/jobs", headers=headers, json={"request_id": str(uuid4()), "operation": "metadata"}).status_code == 403