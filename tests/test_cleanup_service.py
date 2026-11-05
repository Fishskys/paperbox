"""Cleanup preview/execution contracts and destructive guards use only SQLite/fakes."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api import cleanup as api, jobs as jobs_api
from app.core.config import settings
from app.core.security import require_admin, require_read
from app.db.models import CleanupCandidate, CleanupReceipt, IngestionJob, ManagedSearchIndex, Paper, SearchQuery
from app.db.session import get_db
from app.schemas.cleanup import CleanupIn
from app.services import cleanup_indices, cleanup_objects, cleanup_papers, cleanup_policy as policy, cleanup_service as service
from app.services.api_key_service import AuthIdentity
from app.services.cleanup_resources import Resources
from tests.test_metadata_merge import make_paper, make_source
from tests.test_auth_rest import key_material  # noqa: F401


def body(**values):
    return CleanupIn(request_id=uuid4(), categories=["search_logs"], **values)


def submit(session, request=None):
    return service.submit(session, request or body(), actor="admin", enqueue=False)


def query(session, **values):
    row = SearchQuery(id=str(uuid4()), query="private query", mode="keyword", top_k=10, rerank=False, returned=0,
        created_at=policy.now() - timedelta(days=100), **values)
    session.add(row)
    session.commit()
    return row


def preview_apply(session, request=None, resources=None):
    request = request or body()
    preview = submit(session, request)
    service.execute(session, preview.id, resources=resources)
    apply = submit(session, request.model_copy(update={"request_id": uuid4(), "dry_run": False, "preview_job_id": UUID(preview.id)}))
    return preview, apply


def test_retention_freezes_scope_and_does_not_delete_on_preview(db_session):
    old = query(db_session)
    preview, apply = preview_apply(db_session)
    assert db_session.get(SearchQuery, old.id)
    newer = query(db_session)
    service.execute(db_session, apply.id)
    assert db_session.scalar(select(SearchQuery.id).where(SearchQuery.id == old.id)) is None
    assert db_session.get(SearchQuery, newer.id)
    assert service.report(db_session, preview.id)["ready"] == 1
    assert service.report(db_session, apply.id)["completed"] == 1
    service.execute(db_session, apply.id)
    assert service.report(db_session, apply.id)["completed"] == 1


def test_changed_row_is_skipped(db_session):
    row = query(db_session)
    _, apply = preview_apply(db_session)
    row.query = "edited"
    db_session.commit()
    service.execute(db_session, apply.id)
    assert db_session.get(SearchQuery, row.id)
    assert service.report(db_session, apply.id)["skipped"] == 1


def test_receipts_prevent_replay_after_report_gc(db_session):
    request = body()
    job = submit(db_session, request)
    assert submit(db_session, request).id == job.id
    db_session.execute(delete(IngestionJob).where(IngestionJob.id == job.id))
    db_session.commit()
    assert db_session.get(CleanupReceipt, job.id)
    with pytest.raises(service.CleanupError) as error:
        submit(db_session, request)
    assert error.value.status == 410
    with pytest.raises(service.CleanupError, match="different"):
        submit(db_session, request.model_copy(update={"operation": "inventory"}))


@pytest.mark.parametrize("change", ["expiry", "policy", "scope", "inventory"])
def test_apply_requires_valid_matching_cleanup_preview(db_session, monkeypatch, change):
    preview = submit(db_session, body(operation="inventory") if change == "inventory" else body())
    service.execute(db_session, preview.id)
    values = {"request_id": uuid4(), "dry_run": False, "preview_job_id": UUID(preview.id)}
    if change == "expiry":
        preview.payload = {**preview.payload, "expires_at": (policy.now() - timedelta(seconds=1)).isoformat()}
        db_session.commit()
    if change == "policy":
        monkeypatch.setattr(settings, "cleanup_log_days", settings.cleanup_log_days + 1)
    if change == "scope":
        values["categories"] = ["jobs"]
    with pytest.raises(service.CleanupError):
        submit(db_session, body().model_copy(update=values))


def test_failed_adapter_retries_only_failed_candidates(db_session):
    class Fake:
        fail = True
        removed = []
        def scan(self, category, request, moment):
            for key in ("one", "two"):
                yield {"category": category, "resource_key": key, "revision": "v1", "detail": {}, "state": "ready"}
        def remove(self, candidate):
            if candidate.resource_key == "two" and self.fail:
                raise OSError("storage unavailable")
            self.removed.append(candidate.resource_key)
    fake = Fake()
    _, apply = preview_apply(db_session, resources=fake)
    service.execute(db_session, apply.id, resources=fake)
    assert apply.stage == "FAILED"
    assert service.report(db_session, apply.id)["completed"] == 1
    fake.fail = False
    retry = submit(db_session, body(dry_run=False, retry_job_id=apply.id))
    service.execute(db_session, retry.id, resources=fake)
    assert fake.removed.count("one") == 1
    assert fake.removed.count("two") == 1
    assert retry.stage == "COMPLETED"


def test_preview_is_bounded_and_report_paged(db_session, monkeypatch):
    monkeypatch.setattr(settings, "cleanup_batch_size", 2)
    for _ in range(4):
        query(db_session)
    job = submit(db_session)
    service.execute(db_session, job.id)
    report = service.report(db_session, job.id, offset=1, limit=1)
    assert report["limited"] and report["total"] == 2 and len(report["items"]) == 1
    assert report["size_bytes"] is None


class Store:
    def __init__(self):
        self.objects = {}
        self.version = None
        self.fail = None
    def versioning(self, bucket):
        return self.version
    def list_objects(self, prefix, bucket=None):
        return [obj for (b, key), obj in self.objects.items() if b == bucket and key.startswith(prefix)]
    def add(self, key, bucket=None):
        self.objects[(bucket or settings.minio_bucket, key)] = SimpleNamespace(object_name=key, etag="stable", size=3,
            last_modified=policy.now() - timedelta(days=100))
    def delete_object(self, key, bucket=None):
        if self.fail == key:
            raise OSError("object deletion failed")
        self.objects.pop((bucket, key), None)


class Client:
    def __init__(self):
        self.data = {}
        self.counts = {}
        self.indices = SimpleNamespace(get=self.get, delete=self.delete, exists=lambda index: index in self.data)
        self.cat = SimpleNamespace(indices=lambda **_: [{"index": key} for key in self.data])
    def add(self, name, aliases=None):
        self.data[name] = {"settings": {"index": {"uuid": str(uuid4())}}, "aliases": aliases or {},
            "mappings": {"properties": {"paper_id": {"type": "keyword"}, "embedding": {"type": "knn_vector"}}}}
    def get(self, index):
        return {index: self.data[index]}
    def delete(self, index):
        del self.data[index]
    def info(self):
        return {"cluster_uuid": "test-cluster"}
    def count(self, index, body):
        return {"count": self.counts.get((index, body["query"]["term"]["paper_id"]), 0)}
    def delete_by_query(self, index, body, **kwargs):
        self.counts[(index, body["query"]["term"]["paper_id"])] = 0
        return {"failures": []}


def test_parse_cache_preserves_original_and_can_resume_partial_failure(db_session):
    paper = make_paper(db_session)
    db_session.commit()
    store = Store()
    original = f"papers/{paper.id}/original.pdf"
    keys = [f"papers/{paper.id}/extracted/parsed/{n}.json" for n in range(2)]
    for key in [original, *keys]:
        store.add(key)
    resource = Resources(db_session, store=store)
    request = CleanupIn(request_id=uuid4(), categories=["parse_cache"])
    _, apply = preview_apply(db_session, request, resource)
    store.fail = keys[1]
    service.execute(db_session, apply.id, resources=resource)
    assert apply.stage == "FAILED"
    store.fail = None
    retry = submit(db_session, request.model_copy(update={"request_id": uuid4(), "dry_run": False, "retry_job_id": UUID(apply.id)}))
    service.execute(db_session, retry.id, resources=resource)
    assert retry.stage == "COMPLETED"
    assert list(store.objects) == [(settings.minio_bucket, original)]


def test_cache_protects_versioned_buckets_and_active_papers(db_session):
    paper = make_paper(db_session)
    store = Store()
    store.add(f"papers/{paper.id}/extracted/parsed/cache.json")
    store.version = "Enabled"
    request = CleanupIn(request_id=uuid4(), categories=["parse_cache"])
    preview = submit(db_session, request)
    resource = Resources(db_session, store=store)
    service.execute(db_session, preview.id, resources=resource)
    assert service.report(db_session, preview.id)["protected"] == 1
    store.version = None
    _, apply = preview_apply(db_session, request.model_copy(update={"request_id": uuid4()}), resource)
    db_session.add(IngestionJob(paper_id=paper.id, kind="reindex", stage="QUEUED", payload={}))
    db_session.commit()
    service.execute(db_session, apply.id, resources=resource)
    assert service.report(db_session, apply.id)["skipped"] == 1 and store.objects


def test_index_deletion_requires_claim_no_alias_and_other_rollback(db_session):
    client = Client()
    client.add("custom-archive")
    client.add("rollback-copy")
    registry = cleanup_indices.IndexRegistry(db_session, client)
    assert "not claimed" in registry.deletion_guard("custom-archive")[1]
    registry.claim("custom-archive", actor="admin")
    assert "rollback" in registry.deletion_guard("custom-archive")[1]
    registry.claim("rollback-copy", actor="admin", rollback=True)
    assert registry.deletion_guard("custom-archive")[1] is None
    client.data["custom-archive"]["aliases"] = {"live": {}}
    assert "aliases" in registry.deletion_guard("custom-archive")[1]
    client.data["custom-archive"]["settings"]["index"]["uuid"] = "recreated"
    assert "not claimed" in registry.deletion_guard("custom-archive")[1]


def test_hard_purge_relational_closure_and_history_index_copies(db_session):
    paper = make_paper(db_session, deleted_at=policy.now(), status="DELETED")
    source = make_source(db_session, paper.id, "manual")
    identifier, source_id = paper.id, source.id
    job = IngestionJob(paper_id=paper.id, kind="ingest", stage="COMPLETED", payload={"title": "old title"})
    db_session.add(job)
    db_session.commit()
    query(db_session, results=[{"paper_id": paper.id, "title": "private title"}, {"paper_id": "other", "title": "keep"}])
    client, store = Client(), Store()
    for name in ("current", "historical"):
        client.add(name, {"live": {}} if name == "current" else {})
        client.counts[(name, paper.id)] = 1
        cleanup_indices.IndexRegistry(db_session, client).claim(name, actor="admin")
    store.add(f"papers/{paper.id}/original.pdf")
    db_session.commit()
    request = CleanupIn(request_id=uuid4(), categories=["deleted_papers"], paper_ids=[paper.id])
    resource = Resources(db_session, store=store, client=client)
    _, apply = preview_apply(db_session, request, resource)
    service.execute(db_session, apply.id, resources=resource)
    result = service.report(db_session, apply.id)
    assert result["completed"] == 1, result
    assert db_session.scalar(select(Paper.id).where(Paper.id == identifier)) is None
    from app.db.models import PaperSource
    assert db_session.scalar(select(PaperSource.id).where(PaperSource.id == source_id)) is None
    assert db_session.get(CleanupReceipt, job.id)
    assert not store.objects and all(count == 0 for count in client.counts.values())
    assert db_session.scalar(select(SearchQuery.results)) == [{"paper_id": "other", "title": "keep"}]


def test_hard_purge_protects_unclaimed_historical_copies(db_session):
    paper = make_paper(db_session, deleted_at=policy.now())
    client = Client()
    client.add("unclaimed")
    client.counts[("unclaimed", paper.id)] = 1
    request = CleanupIn(request_id=uuid4(), categories=["deleted_papers"], paper_ids=[paper.id])
    preview = submit(db_session, request)
    service.execute(db_session, preview.id, resources=Resources(db_session, store=Store(), client=client))
    report = service.report(db_session, preview.id)
    assert report["protected"] == 1 and "unclaimed" in report["items"][0]["error"]


def test_active_log_and_symlink_are_protected(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_objects, "LOG_ROOT", tmp_path)
    import os
    old = (policy.now() - timedelta(days=100)).timestamp()
    path = tmp_path / "paperbox.log"
    path.write_text("test")
    os.utime(path, (old, old))
    with path.open() as stream:
        assert stream.read()
        rows = list(cleanup_objects.scan_logs(policy.now()))
        assert rows[0]["state"] == "protected"
    (tmp_path / "unsafe.log").symlink_to(path)
    rows = list(cleanup_objects.scan_logs(policy.now()))
    assert next(row for row in rows if row["resource_key"] == "unsafe.log")["state"] == "protected"
    with pytest.raises(ValueError):
        cleanup_objects.log_snapshot("../paperbox.log")


def test_api_cleanup_hidden_from_reader_and_generic_retry(session_factory):
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(jobs_api.router)
    app.dependency_overrides[get_db] = lambda: session_factory()
    reader = AuthIdentity(prefix="reader", role="read", source="test")
    app.dependency_overrides[require_read] = lambda: reader
    from fastapi import HTTPException
    def deny():
        raise HTTPException(403)
    app.dependency_overrides[require_admin] = deny
    with session_factory() as session:
        job = submit(session)
        identifier = job.id
    with TestClient(app) as client:
        for url in ("/policy", "/jobs", f"/jobs/{identifier}/report", "/deleted-papers", "/indices"):
            assert client.get("/api/admin/cleanup" + url).status_code == 403
        assert client.get("/api/jobs").json()["total"] == 0
        assert client.get(f"/api/jobs/{identifier}").status_code == 404


def test_cli_preview_apply_uses_saved_scope(session_factory, monkeypatch, capsys):
    from scripts import cleanup as cli
    with session_factory() as session:
        paper = make_paper(session, deleted_at=policy.now())
        identifier = paper.id
        session.commit()
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    store, client = Store(), Client()
    monkeypatch.setattr(service, "Resources", lambda session: Resources(session, store=store, client=client))
    assert cli.main(["--paper-id", identifier]) == 0
    import json
    preview = json.loads(capsys.readouterr().out)
    with session_factory() as session:
        assert session.get(Paper, identifier)
    assert cli.main(["--apply-preview", preview["job_id"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["completed"] == 1
    with session_factory() as session:
        assert session.get(Paper, identifier) is None
    with pytest.raises(SystemExit):
        cli.main([])


def test_index_cli_registers_target_and_protects_migration_source(db_session, monkeypatch):
    from scripts import create_index as cli
    from app.db import session as database
    client = Client()
    for name in ("old-name", "new-name"):
        client.add(name)
    monkeypatch.setattr(database, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(cli, "get_client", lambda: client)
    monkeypatch.setattr(cli, "migrate", lambda *args, **kwargs: 0)
    assert cli.main(["--index", "new-name", "--migrate-from", "old-name"]) == 0
    assert db_session.get(ManagedSearchIndex, "old-name").is_rollback
    assert not db_session.get(ManagedSearchIndex, "new-name").is_rollback


def test_default_scheduler_only_retries_invalid_figures(db_session, monkeypatch):
    from app.workers import cleanup_scheduler
    monkeypatch.setattr(cleanup_scheduler, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(settings, "cleanup_auto_enabled", False)
    monkeypatch.setattr(cleanup_scheduler.figure_service, "cleanup", lambda session: 3)
    def unexpected(*args, **kwargs):
        raise AssertionError("ordinary retention must remain opt-in")
    monkeypatch.setattr(service, "submit", unexpected)
    assert cleanup_scheduler.collect().as_dict()["figure_objects"] == 3


@pytest.mark.parametrize("role", ["read", "write", "admin"])
def test_real_authentication_guards_policy_reports_and_execution(key_material, role, session_factory):
    from app.main import AuthContextMiddleware
    app = FastAPI()
    app.include_router(api.router)
    app.add_middleware(AuthContextMiddleware)
    def database():
        with session_factory() as session:
            yield session
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {key_material[role]}"}
        expected = 200 if role == "admin" else 403
        for path in ("policy", "jobs", "deleted-papers"):
            assert client.get(f"/api/admin/cleanup/{path}", headers=headers).status_code == expected
        if role != "admin":
            assert client.get(f"/api/admin/cleanup/jobs/{uuid4()}/report", headers=headers).status_code == 403
            assert client.get(f"/api/admin/cleanup/jobs/{uuid4()}/download", headers=headers).status_code == 403
            assert client.post("/api/admin/cleanup/jobs", headers=headers, json=body().model_dump(mode="json")).status_code == 403
            assert client.post("/api/admin/cleanup/indices/claim", headers=headers, json={"name": "archive"}).status_code == 403


def test_job_gc_keeps_receipt_and_active_parent_references(db_session):
    from app.schemas.maintenance import MaintenanceIn
    from app.services import maintenance_service
    old_request = MaintenanceIn(request_id=uuid4(), operation="consistency")
    old = IngestionJob(id=maintenance_service.request_job_id("owner", old_request.request_id), kind="maintenance", stage="COMPLETED",
        payload={"request": old_request.model_dump(mode="json", exclude={"request_id"}), "actor": "owner"}, finished_at=policy.now() - timedelta(days=200))
    protected = IngestionJob(id=str(uuid4()), kind="ingest", stage="COMPLETED", payload={}, finished_at=policy.now() - timedelta(days=200))
    db_session.add_all([old, protected])
    db_session.flush()
    db_session.add(IngestionJob(kind="maintenance", stage="QUEUED", payload={"children": [{"job_id": protected.id}]}))
    db_session.commit()
    request = CleanupIn(request_id=uuid4(), categories=["jobs"])
    preview, apply = preview_apply(db_session, request)
    assert service.report(db_session, preview.id)["protected"] == 1
    service.execute(db_session, apply.id)
    assert db_session.scalar(select(IngestionJob.id).where(IngestionJob.id == old.id)) is None
    assert db_session.get(IngestionJob, protected.id)
    with pytest.raises(maintenance_service.MaintenanceError) as error:
        maintenance_service.submit(db_session, old_request, actor="owner")
    assert error.value.status == 410


def test_unused_author_cleanup_preserves_references_from_soft_deleted_papers(db_session):
    from app.db.models import Author, PaperAuthor
    paper = make_paper(db_session, deleted_at=policy.now())
    old = policy.now() - timedelta(days=200)
    unused = Author(name="Unused", normalized_name="unused", created_at=old)
    used = Author(name="Used", normalized_name="used", created_at=old)
    db_session.add_all([unused, used])
    db_session.flush()
    db_session.add(PaperAuthor(paper_id=paper.id, author_id=used.id, author_order=0))
    db_session.commit()
    _, apply = preview_apply(db_session, CleanupIn(request_id=uuid4(), categories=["orphan_records"]))
    service.execute(db_session, apply.id)
    assert db_session.scalar(select(Author.id).where(Author.id == unused.id)) is None
    assert db_session.get(Author, used.id)


def test_venue_scope_cannot_expand_after_preview(db_session):
    from app.db.models import Venue, VenueEdition
    venue = Venue(name="Unused conference", normalized_name="unused conference", created_at=policy.now() - timedelta(days=200))
    db_session.add(venue)
    db_session.commit()
    request = CleanupIn(request_id=uuid4(), categories=["orphan_records"])
    _, apply = preview_apply(db_session, request)
    db_session.add(VenueEdition(venue_id=venue.id, year=2026))
    db_session.commit()
    service.execute(db_session, apply.id)
    assert service.report(db_session, apply.id)["skipped"] == 1
    assert db_session.get(Venue, venue.id)
    _, apply = preview_apply(db_session, request.model_copy(update={"request_id": uuid4()}))
    service.execute(db_session, apply.id)
    assert service.report(db_session, apply.id)["completed"] == 1
    assert db_session.scalar(select(VenueEdition.id)) is None


def test_configured_file_logging_rotates_and_rejects_other_directories(tmp_path, monkeypatch):
    import logging
    from logging.handlers import RotatingFileHandler
    from app.core import config, logging as app_logging
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(settings, "log_file", "logs/app/test.log")
    monkeypatch.setattr(settings, "log_max_bytes", 1024)
    monkeypatch.setattr(settings, "log_backup_count", 2)
    root = logging.getLogger()
    previous, level = list(root.handlers), root.level
    try:
        app_logging.configure_logging()
        handler = next(item for item in root.handlers if isinstance(item, RotatingFileHandler))
        for _ in range(20):
            handler.handle(logging.LogRecord("cleanup-test", logging.INFO, "", 0, "x" * 300, (), None))
        assert (tmp_path / "logs/app/test.log.2").exists()
        assert not (tmp_path / "logs/app/test.log.3").exists()
        monkeypatch.setattr(settings, "log_file", "elsewhere/test.log")
        with pytest.raises(ValueError, match="logs/app"):
            app_logging.configure_logging()
    finally:
        for handler in root.handlers:
            if isinstance(handler, RotatingFileHandler):
                handler.close()
        root.handlers = previous
        root.setLevel(level)
