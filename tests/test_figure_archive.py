"""Downloadable archives contain the visible assets and their source captions."""

from contextlib import contextmanager
import hashlib
import io
import json
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.figures import router
from app.core.config import settings
from app.db.session import get_db
from app.services import figure_service as figures, object_storage
from app.services.api_key_service import AuthIdentity
from tests import test_figures as fixture_helpers

paper_environment = fixture_helpers.environment
extract = fixture_helpers.extract
result = fixture_helpers.result


@pytest.fixture
def archive_env(paper_environment, monkeypatch):
    environment = paper_environment
    session, paper, source, store, waiting = environment

    @contextmanager
    def stream(key, **kwargs):
        if store.fail:
            raise object_storage.ObjectStorageError("offline")
        if key not in store.objects:
            raise object_storage.ObjectNotFound("missing")
        yield io.BytesIO(store.objects[key])

    monkeypatch.setattr(store, "open_stream", stream, raising=False)
    monkeypatch.setattr(object_storage, "open_stream", stream)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: session

    @app.middleware("http")
    async def identity(request, call_next):
        role = request.headers.get("x-api-key")
        request.state.auth_identity = (
            AuthIdentity(prefix="fixture", role=role, source="fixture")
            if role in ("read", "write")
            else None
        )
        request.state.auth_token_present = bool(role)
        return await call_next(request)

    with TestClient(app) as client:
        yield environment, client, f"/api/papers/{paper.id}/figures/download"


def test_download_zip_includes_original_assets_captions_and_manifest(
    archive_env, tmp_path
):
    environment, client, url = archive_env
    session, paper, source, store, waiting = environment
    request, outcome = extract(environment, tmp_path)
    original = dict(store.objects)
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-paperbox-figure-generation"] == request["generation_id"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        row = manifest["figures"][0]
        assert row["figure_number"] == "7"
        assert row["parts"][0]["page"] == 2
        assert (
            archive.read(row["archive_files"][0]["path"]) == b"%PDF-independent-asset"
        )
        assert (
            archive.read(
                next(
                    path for path in archive.namelist() if path.endswith("caption.txt")
                )
            ).decode()
            == "Fig. 7. Circuit under test."
        )
        assert "object_key" not in json.dumps(manifest) and "bucket" not in json.dumps(
            manifest
        )
    assert original == store.objects and source.deleted_at is None


def test_duplicate_numbers_and_all_parts_have_distinct_paths(archive_env, tmp_path):
    environment, client, url = archive_env
    session, paper, source, store, waiting = environment
    request, _ = figures.request_extraction(session, paper.id)
    outcome, second = result(tmp_path), result(tmp_path)
    outcome["figures"].extend(second["figures"])
    for row in outcome["figures"]:
        for part in (1, 2):
            content = b"preview " + str(part).encode()
            path = row["id"] + f"/preview-{part}.png"
            (tmp_path / path).write_bytes(content)
            row["assets"].append(
                {
                    "path": path,
                    "role": "preview",
                    "part": part,
                    "mime_type": "image/png",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "width": 1,
                    "height": 1,
                    "dpi": 300,
                }
            )
    figures.publish(session, request["generation_id"], outcome, tmp_path, store=store)
    response = client.get(url, params={"variant": "all"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert len([name for name in names if name.endswith(".pdf")]) == 2
        assert len([name for name in names if name.endswith(".png")]) == 4
        assert (
            len({name.split("/")[0] for name in names if name != "manifest.json"}) == 2
        )


def test_missing_preview_is_explained_in_the_archive(archive_env, tmp_path):
    environment, client, url = archive_env
    extract(environment, tmp_path)
    response = client.get(url, params={"variant": "preview"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["figures"][0]["archive_files"] == []
        assert any(
            warning["code"] == "FIGURE_ARCHIVE_VARIANT_UNAVAILABLE"
            for warning in manifest["warnings"]
        )


def test_not_extracted_pinned_generation_and_deleted_figures(archive_env, tmp_path):
    environment, client, url = archive_env
    assert client.get(url).status_code == 202
    request, outcome = extract(environment, tmp_path)
    assert (
        client.get(url, params={"generation_id": "old-generation"}).status_code == 409
    )
    session, paper, source, store, waiting = environment
    figures.delete_figures(
        session, paper.id, figure_id=outcome["figures"][0]["id"], store=store
    )
    response = client.get(url)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["manifest.json"]
        assert json.loads(archive.read("manifest.json"))["figures"] == []


@pytest.mark.parametrize(
    "failure,expected",
    [("missing", 404), ("storage", 503), ("corrupt", 409), ("budget", 413)],
)
def test_archive_failure_never_returns_a_partial_zip(
    archive_env, tmp_path, monkeypatch, failure, expected
):
    environment, client, url = archive_env
    session, paper, source, store, waiting = environment
    extract(environment, tmp_path)
    key = next(key for key in store.objects if key.endswith("figure.pdf"))
    if failure == "missing":
        del store.objects[key]
    elif failure == "storage":
        store.fail = True
    elif failure == "corrupt":
        store.objects[key] = b"x" * len(store.objects[key])
    else:
        monkeypatch.setattr(settings, "figure_max_temp_bytes", 100)
    response = client.get(url)
    assert response.status_code == expected
    assert response.headers["content-type"] == "application/json"


def test_delete_during_packaging_invalidates_the_archive(
    archive_env, tmp_path, monkeypatch
):
    environment, client, url = archive_env
    session, paper, source, store, waiting = environment
    extract(environment, tmp_path)
    original = store.open_stream

    @contextmanager
    def concurrent_delete(key, **kwargs):
        with original(key, **kwargs) as content:
            figures.delete_figures(session, paper.id, store=store)
            yield content

    monkeypatch.setattr(object_storage, "open_stream", concurrent_delete)
    response = client.get(url)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "FIGURE_GENERATION_CHANGED"


def test_download_starts_one_job_and_returns_zip_when_ready(archive_env, tmp_path):
    environment, client, url = archive_env
    session, paper, source, store, waiting = environment
    first = client.get(url, params={"variant": "all"})
    assert first.status_code == 202
    pending = first.json()
    assert first.headers["location"] == pending["status_url"]
    assert first.headers["retry-after"] == "2"
    assert first.headers["cache-control"] == "private, no-store"
    assert pending["download_url"].endswith("?variant=all")
    second = client.get(pending["download_url"])
    assert second.status_code == 202 and second.json()["job_id"] == pending["job_id"]
    figures.publish(
        session, pending["generation_id"], result(tmp_path), tmp_path, store=store
    )
    ready = client.get(pending["download_url"])
    assert (
        ready.status_code == 200 and ready.headers["content-type"] == "application/zip"
    )
    with zipfile.ZipFile(io.BytesIO(ready.content)) as archive:
        assert (
            json.loads(archive.read("manifest.json"))["generation_id"]
            == pending["generation_id"]
        )


def test_failed_or_deleted_history_is_not_automatically_recreated(
    archive_env, tmp_path
):
    from app.db.models import FigureExtractionRun as Run
    from sqlalchemy import func, select

    environment, client, url = archive_env
    session, paper, source, store, waiting = environment
    pending = client.get(url).json()
    figures.fail(session, pending["generation_id"], "EXTRACTOR", "extraction failed")
    assert client.get(url).status_code == 409
    assert session.scalar(select(func.count()).select_from(Run)) == 1
    extract(environment, tmp_path)
    figures.delete_figures(session, paper.id, store=store)
    assert client.get(url).status_code == 409
    assert session.scalar(select(func.count()).select_from(Run)) == 2


def test_pinned_missing_generation_never_triggers_extraction(archive_env):
    from app.db.models import FigureExtractionRun as Run
    from sqlalchemy import func, select

    environment, client, url = archive_env
    session, paper, source, store, waiting = environment
    response = client.get(url, params={"generation_id": "old-generation"})
    assert response.status_code == 409
    assert session.scalar(select(func.count()).select_from(Run)) == 0


def test_first_download_requires_write_but_existing_zip_obeys_read_policy(
    archive_env, tmp_path, monkeypatch
):
    environment, client, url = archive_env
    session, paper, source, store, waiting = environment
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_read_access", "public")
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-API-Key": "read"}).status_code == 403
    pending = client.get(url, headers={"X-API-Key": "write"})
    assert pending.status_code == 202
    assert client.get(url, headers={"X-API-Key": "read"}).status_code == 202
    figures.publish(
        session,
        pending.json()["generation_id"],
        result(tmp_path),
        tmp_path,
        store=store,
    )
    assert client.get(url).status_code == 200
    monkeypatch.setattr(settings, "auth_read_access", "key")
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-API-Key": "read"}).status_code == 200


@pytest.mark.parametrize(
    "failure,status", [("pdf", 409), ("backend", 503), ("queue", 503)]
)
def test_auto_extraction_preconditions_do_not_leave_queued_jobs(
    archive_env, monkeypatch, failure, status
):
    from app.db.models import FigureExtractionRun as Run
    from app.workers import queue
    from sqlalchemy import func, select
    from types import SimpleNamespace

    environment, client, url = archive_env
    session, paper, source, store, waiting = environment
    if failure == "pdf":
        source.deleted_at = figures.now()
        session.commit()
    elif failure == "backend":

        def unavailable():
            raise figures.FigureError(
                "FIGURE_BACKEND_UNAVAILABLE", "backend unavailable", 503
            )

        monkeypatch.setattr(figures, "options", unavailable)
    else:
        monkeypatch.setattr(queue, "get_queue", lambda: SimpleNamespace(started=False))
    assert client.get(url).status_code == status
    assert session.scalar(select(func.count()).select_from(Run)) == 0


def test_completed_empty_extraction_returns_manifest_without_starting_again(
    archive_env,
):
    environment, client, url = archive_env
    session, paper, source, store, waiting = environment
    pending = client.get(url).json()
    outcome = {
        "source_sha256": "a" * 64,
        "figures": [],
        "warnings": [],
        "status": "completed",
    }
    from tempfile import TemporaryDirectory
    from pathlib import Path

    with TemporaryDirectory() as directory:
        figures.publish(
            session, pending["generation_id"], outcome, Path(directory), store=store
        )
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["x-paperbox-figure-generation"] == pending["generation_id"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert json.loads(archive.read("manifest.json"))["figures"] == []
