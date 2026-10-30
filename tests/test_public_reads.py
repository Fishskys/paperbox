"""Real business endpoints reject missing keys and insufficient permissions."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.services import api_key_service

# Search belongs to the read tier even though it records query history.
READS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/consistency"),
        ("GET", "/api/consistency/doi"),
        ("GET", "/api/jobs"),
        ("GET", "/api/jobs/queue"),
        ("GET", "/api/jobs/{job_id}"),
        ("GET", "/api/metadata/review"),
        ("GET", "/api/papers"),
        ("GET", "/api/papers/{paper_id}"),
        ("GET", "/api/papers/{paper_id}/chunks"),
        ("GET", "/api/papers/{paper_id}/degradations"),
        ("GET", "/api/papers/{paper_id}/file"),
        ("GET", "/api/papers/{paper_id}/metadata"),
        ("POST", "/api/search"),
        ("GET", "/api/search-logs"),
    }
)

#: Everything that changes state: the mutations (``write``) plus the two
#: whole-library operations (``admin``: rebuild index, re-extract metadata).
WRITES: dict[tuple[str, str], str] = {
    ("POST", "/api/papers/{paper_id}/file"): "write",
    ("DELETE", "/api/papers/{paper_id}"): "write",
    ("POST", "/api/jobs/{job_id}/retry"): "write",
    ("POST", "/api/metadata/apply"): "write",
    ("POST", "/api/metadata/import"): "write",
    ("POST", "/api/metadata/sources/{source_id}/attach"): "write",
    ("POST", "/api/papers/ingest"): "write",
    ("POST", "/api/papers/ingest/compressed"): "write",
    ("POST", "/api/papers/ingest/dir"): "write",
    ("POST", "/api/papers/ingest/file"): "write",
    ("POST", "/api/papers/ingest/files"): "write",
    ("POST", "/api/papers/{paper_id}/reindex"): "write",
    ("PATCH", "/api/papers/{paper_id}/metadata"): "write",
    ("DELETE", "/api/papers/{paper_id}/metadata"): "write",
    ("POST", "/api/papers/{paper_id}/metadata/reextract"): "write",
    ("POST", "/api/papers/{paper_id}/metadata/conflicts/dismiss"): "write",
    ("POST", "/api/papers/{paper_id}/metadata/rollback"): "write",
    ("POST", "/api/papers/reindex"): "admin",
    ("POST", "/api/metadata/reextract"): "admin",
}

@pytest.fixture()
def authenticated_client(monkeypatch, session_factory):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_read_access", "key")
    monkeypatch.setattr(settings, "paper_api_key", "")
    monkeypatch.setattr(settings, "paper_api_keys", "")
    monkeypatch.setattr("app.main.SessionLocal", session_factory)
    with session_factory() as session:
        keys = {}
        for role in ("read", "write"):
            _, keys[role] = api_key_service.create_key(session, prefix=f"test-{role}", role=role)
        session.commit()

    def database():
        with session_factory() as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_db, database)
    yield TestClient(app), keys


def request(client, method, template, key=None):
    path = template.format(
        paper_id="11111111-2222-4333-8444-555555555555",
        job_id="11111111-2222-4333-8444-555555555555",
        source_id="11111111-2222-4333-8444-555555555555",
    )
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return client.request(method, path, headers=headers, json={})


def test_private_business_endpoints_reject_missing_credentials(authenticated_client):
    client, _ = authenticated_client
    for method, path in sorted(READS | set(WRITES)):
        response = request(client, method, path)
        assert response.status_code == 401, (method, path, response.text)


def test_read_credentials_cannot_modify_the_library(authenticated_client):
    client, keys = authenticated_client
    for method, path in WRITES:
        response = request(client, method, path, keys["read"])
        assert response.status_code == 403, (method, path, response.text)


def test_write_credentials_cannot_run_whole_library_operations(authenticated_client):
    client, keys = authenticated_client
    for (method, path), tier in WRITES.items():
        if tier == "admin":
            response = request(client, method, path, keys["write"])
            assert response.status_code == 403, (method, path, response.text)
