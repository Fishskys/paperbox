"""User-visible figure lifecycle with real SQL transactions and fake storage."""

import hashlib
import json
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.figures import router
from app.core.config import settings
from app.db.models import Paper, PaperFile, FigureExtractionRun as Run
from app.db.session import get_db
from app.services import figure_service as figures
from app.workers import queue


class Store:
    def __init__(self):
        self.objects = {}
        self.fail = False

    def upload_file(self, key, content, **kwargs):
        if self.fail:
            raise RuntimeError("storage offline")
        self.objects[key] = content.read()

    def upload_bytes(self, key, content, **kwargs):
        if self.fail:
            raise RuntimeError("storage offline")
        self.objects[key] = content

    def list_objects(self, prefix, **kwargs):
        if self.fail:
            raise RuntimeError("storage offline")
        return [
            SimpleNamespace(object_name=key)
            for key in self.objects
            if key.startswith(prefix)
        ]

    def delete_object(self, key, **kwargs):
        self.objects.pop(key, None)


@pytest.fixture
def environment(db_session, monkeypatch):
    paper = Paper(
        id=str(uuid.uuid4()),
        fingerprint=str(uuid.uuid4()),
        title="Independent fixture",
        status="INDEXED",
    )
    db_session.add(paper)
    db_session.flush()
    source = PaperFile(
        paper_id=paper.id,
        kind="original",
        bucket="test",
        object_key=f"papers/{paper.id}/original.pdf",
        sha256="a" * 64,
        size_bytes=123,
        is_primary=True,
    )
    db_session.add(source)
    db_session.commit()
    monkeypatch.setattr(
        figures,
        "options",
        lambda: {"backend_version": "fixture", "max_temp_bytes": 100000},
    )
    waiting = []
    monkeypatch.setattr(
        queue,
        "get_queue",
        lambda: SimpleNamespace(
            started=True, enqueue=lambda *args, **kwargs: waiting.append(args) or True
        ),
    )
    return db_session, paper, source, Store(), waiting


def result(directory, number="7"):
    identifier = str(uuid.uuid4())
    folder = directory / identifier
    folder.mkdir()
    content = b"%PDF-independent-asset"
    (folder / "figure.pdf").write_bytes(content)
    return {
        "source_sha256": "a" * 64,
        "status": "partial",
        "warnings": [{"code": "FIGURE_REVIEW_REQUIRED"}],
        "figures": [
            {
                "id": identifier,
                "label_raw": "Fig. " + number,
                "figure_number": number,
                "caption_text": "Fig. " + number + ". Circuit under test.",
                "parts": [{"part": 1, "page": 2}],
                "match_status": "needs_review",
                "assets": [
                    {
                        "path": identifier + "/figure.pdf",
                        "role": "native",
                        "part": 0,
                        "mime_type": "application/pdf",
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "width": None,
                        "height": None,
                        "dpi": None,
                    }
                ],
            }
        ],
    }


def extract(environment, tmp_path):
    session, paper, source, store, waiting = environment
    request, code = figures.request_extraction(session, paper.id, force=True)
    outcome = result(tmp_path)
    figures.publish(session, request["generation_id"], outcome, tmp_path, store=store)
    return request, outcome


def test_list_public_assets_and_reuse(environment, tmp_path):
    session, paper, source, store, waiting = environment
    assert figures.list_figures(session, paper.id)["status"] == "not_extracted"
    request, outcome = extract(environment, tmp_path)
    listed = figures.list_figures(session, paper.id)
    assert listed["total"] == 1
    assert listed["figures"][0]["display_name"] == "figure-7"
    assert listed["figures"][0]["caption_text"] == "Fig. 7. Circuit under test."
    assert "object_key" not in json.dumps(listed)
    assert figures.request_extraction(session, paper.id)[1] == 200
    assert source.sha256 == "a" * 64 and paper.title == "Independent fixture"


def test_failure_keeps_existing_generation(environment, tmp_path):
    session, paper, source, store, waiting = environment
    first, _ = extract(environment, tmp_path)
    second, _ = figures.request_extraction(session, paper.id, force=True)
    directory = tmp_path / "second"
    directory.mkdir()
    store.fail = True
    with pytest.raises(RuntimeError, match="offline"):
        figures.publish(
            session, second["generation_id"], result(directory), directory, store=store
        )
    session.rollback()
    figures.fail(session, second["generation_id"], "STORAGE", "storage offline")
    assert (
        figures.list_figures(session, paper.id)["generation_id"]
        == first["generation_id"]
    )


def test_delete_cancels_and_cannot_republish(environment, tmp_path):
    session, paper, source, store, waiting = environment
    first, _ = extract(environment, tmp_path)
    second, _ = figures.request_extraction(session, paper.id, force=True)
    deleted = figures.delete_figures(session, paper.id, store=store)
    assert deleted["figures_deleted"] == 1
    assert figures.list_figures(session, paper.id)["total"] == 0
    assert not store.objects
    with pytest.raises(figures.FigureError, match="cancelled"):
        figures.check_run(session, second["generation_id"])
    assert source.deleted_at is None


def test_single_delete_cleanup_survives_storage_outage(environment, tmp_path):
    session, paper, source, store, waiting = environment
    first, outcome = extract(environment, tmp_path)
    identifier = outcome["figures"][0]["id"]
    store.fail = True
    assert figures.delete_figures(session, paper.id, figure_id=identifier, store=store)[
        "cleanup_pending"
    ]
    assert figures.list_figures(session, paper.id)["total"] == 0
    store.fail = False
    figures.cleanup(session, store=store)
    assert not store.objects or list(store.objects) == [
        figures.prefix(session.get(Run, first["generation_id"])) + "manifest.json"
    ]
    assert not figures.delete_figures(
        session, paper.id, figure_id=identifier, store=store
    )["cleanup_pending"]


def test_generation_pagination_rejects_replaced_result(environment, tmp_path):
    session, paper, source, store, waiting = environment
    first, _ = extract(environment, tmp_path)
    directory = tmp_path / "next"
    directory.mkdir()
    extract(environment, directory)
    with pytest.raises(figures.FigureError) as caught:
        figures.list_figures(session, paper.id, generation_id=first["generation_id"])
    assert caught.value.status == 409


def test_http_permissions_not_found_and_native_conditional_get(
    environment, tmp_path, monkeypatch
):
    session, paper, source, store, waiting = environment
    _, outcome = extract(environment, tmp_path)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: session
    from app.api import figures as api
    from contextlib import contextmanager
    import io

    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(api.object_storage, "stat_object", lambda *args, **kwargs: None)

    @contextmanager
    def stream(key, **kwargs):
        yield io.BytesIO(store.objects[key])

    monkeypatch.setattr(api.object_storage, "open_stream", stream)
    with TestClient(application) as client:
        base = f"/api/papers/{paper.id}/figures"
        assert client.get(base).json()["total"] == 1
        url = base + "/" + outcome["figures"][0]["id"] + "/file?variant=native"
        response = client.get(url)
        assert response.content == b"%PDF-independent-asset"
        assert response.headers["content-type"] == "application/pdf"
        assert (
            client.get(
                url, headers={"If-None-Match": response.headers["etag"]}
            ).status_code
            == 304
        )
        assert client.get(base + "/" + str(uuid.uuid4())).status_code == 404
        assert (
            client.post(
                base + "/extract", json={"force": True, "surprise": 1}
            ).status_code
            == 422
        )


def test_pdf_required_and_offline_queue_are_explicit(environment, monkeypatch):
    session, paper, source, store, waiting = environment
    monkeypatch.setattr(queue, "get_queue", lambda: SimpleNamespace(started=False))
    with pytest.raises(figures.FigureError) as caught:
        figures.request_extraction(session, paper.id)
    assert caught.value.status == 503
    source.deleted_at = figures.now()
    session.commit()
    with pytest.raises(figures.FigureError) as caught:
        figures.request_extraction(session, paper.id)
    assert caught.value.code == "FIGURE_PDF_REQUIRED"


def test_read_key_cannot_extract_or_delete(environment, monkeypatch):
    session, paper, source, store, waiting = environment
    from app.services.api_key_service import AuthIdentity

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_read_access", "key")
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: session

    @application.middleware("http")
    async def identity(request, call_next):
        token = request.headers.get("x-api-key")
        request.state.auth_identity = (
            AuthIdentity(prefix="fixture", role=token, source="fixture")
            if token in ("read", "write")
            else None
        )
        request.state.auth_token_present = bool(token)
        return await call_next(request)

    with TestClient(application) as client:
        base = f"/api/papers/{paper.id}/figures"
        assert client.get(base).status_code == 401
        assert client.get(base, headers={"X-API-Key": "read"}).status_code == 200
        assert (
            client.post(base + "/extract", headers={"X-API-Key": "read"}).status_code
            == 403
        )
        assert client.delete(base, headers={"X-API-Key": "read"}).status_code == 403
        assert (
            client.post(base + "/extract", headers={"X-API-Key": "write"}).status_code
            == 202
        )


def test_retry_reuses_job_but_has_a_new_generation(environment):
    from app.db.models import IngestionJob

    session, paper, source, store, waiting = environment
    first, _ = figures.request_extraction(session, paper.id)
    figures.fail(session, first["generation_id"], "EXTRACTOR", "interrupted")
    job = session.get(IngestionJob, first["job_id"])
    second, code = figures.request_extraction(
        session, paper.id, force=True, retry_job=job
    )
    assert code == 202 and second["job_id"] == first["job_id"]
    assert second["generation_id"] != first["generation_id"]
    assert queue.kind_for_payload(job.payload) == "figures"


def test_source_changed_and_dry_run_preserve_current_assets(environment, tmp_path):
    session, paper, source, store, waiting = environment
    first, _ = extract(environment, tmp_path)
    before = dict(store.objects)
    assert (
        figures.delete_figures(session, paper.id, dry_run=True, store=store)[
            "figures_deleted"
        ]
        == 1
    )
    assert store.objects == before
    assert figures.list_figures(session, paper.id)["total"] == 1
    source.sha256 = "b" * 64
    session.commit()
    assert figures.list_figures(session, paper.id)["status"] == "stale"


def test_delete_during_upload_cannot_resurrect_figures(environment, tmp_path):
    session, paper, source, store, waiting = environment
    first, _ = figures.request_extraction(session, paper.id)
    run = session.get(Run, first["generation_id"])
    run.status, run.started_at = "running", figures.now()
    session.commit()
    real_upload = store.upload_file

    def delete_during_upload(key, content, **kwargs):
        figures.delete_figures(session, paper.id, store=store)
        real_upload(key, content, **kwargs)

    store.upload_file = delete_during_upload
    with pytest.raises(figures.FigureError, match="cancelled"):
        figures.publish(session, run.id, result(tmp_path), tmp_path, store=store)
    figures.fail(session, run.id, "CANCELLED", "cancelled")
    figures.cleanup(session, store=store)
    assert figures.list_figures(session, paper.id)["total"] == 0
    assert not store.objects


def test_failed_job_does_not_expose_local_paths(environment, monkeypatch):
    import io
    from contextlib import contextmanager
    from app.workers import figure_tasks
    from app.db.models import IngestionJob

    session, paper, source, store, waiting = environment
    request, _ = figures.request_extraction(session, paper.id)
    monkeypatch.setattr(figure_tasks, "SessionLocal", lambda: session)

    @contextmanager
    def stream(*args, **kwargs):
        yield io.BytesIO(b"fixture PDF")

    def broken(*args, **kwargs):
        raise OSError("private-temp-directory/options.json cannot be read")

    cleanup = figures.cleanup
    monkeypatch.setattr(figure_tasks.object_storage, "open_stream", stream)
    monkeypatch.setattr(figure_tasks, "extract", broken)
    monkeypatch.setattr(
        figures,
        "cleanup",
        lambda session, **kwargs: cleanup(session, store=store, **kwargs),
    )
    figure_tasks.run(request["job_id"])
    job = session.get(IngestionJob, request["job_id"])
    assert job.stage == "FAILED" and job.error_code == "FIGURE_EXTRACTION_FAILED"
    assert "private-temp-directory" not in job.error_message
