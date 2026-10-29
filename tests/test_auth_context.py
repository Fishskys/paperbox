"""The effective identity endpoint must not confuse public reads with a valid key."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth
from app.core.config import settings
from app.main import AuthContextMiddleware
from tests.test_auth_rest import key_material  # noqa: F401


@pytest.fixture()
def client(key_material):
    app = FastAPI()
    app.include_router(auth.router)
    app.add_middleware(AuthContextMiddleware)
    with TestClient(app) as transport:
        yield transport


@pytest.mark.parametrize("policy", ["public", "key"])
@pytest.mark.parametrize("role", ["read", "write", "admin"])
def test_context_reports_effective_role_and_inherited_capabilities(client, key_material, monkeypatch, role, policy):
    monkeypatch.setattr(settings, "auth_read_access", policy)
    response = client.get("/api/auth/context", headers={"Authorization": f"Bearer {key_material[role]}"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["role"] == role
    assert payload["prefix"] == {"read": "readkey", "write": "writekey", "admin": "adminkey"}[role]
    assert payload["is_anonymous"] is False
    assert payload["capabilities"] == {"read": True, "write": role in ("write", "admin"), "admin": role == "admin"}
    assert "no-store" in response.headers["cache-control"]
    assert key_material[role] not in response.text
    assert "key_hash" not in response.text


def test_anonymous_public_reader_has_no_write_capabilities(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_read_access", "public")
    payload = client.get("/api/auth/context").json()
    assert payload["is_anonymous"] is True
    assert payload["capabilities"] == {"read": True, "write": False, "admin": False}


def test_closed_read_door_requires_a_key(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_read_access", "key")
    assert client.get("/api/auth/context").status_code == 401


@pytest.mark.parametrize("policy", ["public", "key"])
def test_invalid_key_is_not_reported_as_anonymous_read(client, monkeypatch, policy):
    monkeypatch.setattr(settings, "auth_read_access", policy)
    assert client.get("/api/auth/context", headers={"Authorization": "Bearer invalid-context-key"}).status_code == 403


def test_disabled_auth_keeps_the_existing_anonymous_admin_policy(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    payload = client.get("/api/auth/context").json()
    assert payload["auth_enabled"] is False
    assert payload["is_anonymous"] is True
    assert all(payload["capabilities"].values())


def test_environment_bootstrap_key_uses_the_same_context(client, monkeypatch):
    monkeypatch.setattr(settings, "paper_api_key", "context-test-bootstrap")
    payload = client.get("/api/auth/context", headers={"X-API-Key": "context-test-bootstrap"}).json()
    assert payload["role"] == "admin"
    assert payload["source"] == "PAPER_API_KEY"
    assert payload["prefix"] == "default"