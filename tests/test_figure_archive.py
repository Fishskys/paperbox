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
    assert client.get(url).status_code == 409
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
