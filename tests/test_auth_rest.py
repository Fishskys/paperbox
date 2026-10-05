"""REST auth end to end: the master switch, role tiers, and the log prefix.

The composition under test mirrors ``app.main``: ``AuthContextMiddleware``
resolves the bearer key once and binds ``key_prefix`` into the logging context,
then the router's role dependencies enforce the tier. Keys come from the
in-memory SQLite ``api_keys`` table (the same store MCP reads — G4).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.api import papers as papers_api
from app.core.config import settings
from app.core.logging import get_key_prefix
from app.db.session import get_db
from app.main import AuthContextMiddleware
from app.services import api_key_service


@pytest.fixture()
def key_material(monkeypatch, session_factory) -> dict[str, str]:
    """One live key per tier; the full keys exist only here (never in the DB)."""
    # The production middleware resolves keys through its own session; point it
    # at the in-memory engine the same way the get_db override does.
    monkeypatch.setattr("app.main.SessionLocal", session_factory)
    monkeypatch.setattr(settings, "paper_api_key", "")
    monkeypatch.setattr(settings, "paper_api_keys", "")
    monkeypatch.setattr(settings, "auth_enabled", True)
    setup = session_factory()
    try:
        material: dict[str, str] = {}
        for name, prefix, role in (
            ("reader", "readkey", "read"),
            ("writer", "writekey", "write"),
            ("admin", "adminkey", "admin"),
        ):
            _, full_key = api_key_service.create_key(
                setup, name=name, prefix=prefix, role=role
            )
            material[role] = full_key
        setup.commit()
    finally:
        setup.close()
    return material


def _papers_app(session_factory) -> FastAPI:
    """The papers router behind the production middleware (no overrides of auth)."""

    def _db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(papers_api.router)
    app.add_middleware(AuthContextMiddleware)
    app.dependency_overrides[get_db] = _db
    return app


@pytest.fixture()
def client(key_material, session_factory):
    with TestClient(_papers_app(session_factory)) as test_client:
        yield test_client


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_a_missing_credential_is_a_401_with_a_challenge(client) -> None:
    response = client.get("/api/papers")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_an_unknown_credential_is_a_403(client) -> None:
    response = client.get("/api/papers", headers=_bearer("not-a-key"))
    assert response.status_code == 403


def test_a_read_key_lists_papers(client, key_material) -> None:
    response = client.get("/api/papers", headers=_bearer(key_material["read"]))
    assert response.status_code == 200
    assert response.json() == {"total": 0, "limit": 20, "offset": 0, "papers": []}


def test_a_read_key_cannot_delete(client, key_material) -> None:
    response = client.delete(
        f"/api/papers/{uuid.uuid4()}", headers=_bearer(key_material["read"])
    )
    assert response.status_code == 403
    assert "insufficient role" in response.json()["detail"]


def test_a_write_key_cannot_delete(client, key_material) -> None:
    response = client.delete(
        f"/api/papers/{uuid.uuid4()}", headers=_bearer(key_material["write"])
    )
    assert response.status_code == 403


def test_an_admin_key_passes_the_delete_gate(client, key_material) -> None:
    """The gate passes; the paper itself does not exist, so the answer is 404."""
    response = client.delete(
        f"/api/papers/{uuid.uuid4()}", headers=_bearer(key_material["admin"])
    )
    assert response.status_code == 404


def test_the_x_api_key_header_still_works(client, key_material) -> None:
    response = client.get("/api/papers", headers={"X-API-Key": key_material["read"]})
    assert response.status_code == 200


def test_auth_off_answers_anonymously(monkeypatch, session_factory) -> None:
    """D2: the switch is all-or-nothing — off, every caller is an admin."""
    monkeypatch.setattr(settings, "paper_api_key", "")
    monkeypatch.setattr(settings, "paper_api_keys", "")
    monkeypatch.setattr(settings, "auth_enabled", False)
    with TestClient(_papers_app(session_factory)) as client:
        assert client.get("/api/papers").status_code == 200
        assert client.get(f"/api/papers/{uuid.uuid4()}").status_code == 404


def test_the_middleware_binds_the_key_prefix_for_logs(
    monkeypatch, session_factory, key_material
) -> None:
    """Requirement 3, end to end: a handler sees the caller's prefix."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    router = APIRouter()

    @router.get("/api/whoami")
    def whoami() -> dict:
        return {"prefix": get_key_prefix() or "-"}

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(AuthContextMiddleware)
    with TestClient(app) as client:
        assert (
            client.get("/api/whoami", headers=_bearer(key_material["read"])).json()[
                "prefix"
            ]
            == "readkey"
        )
        assert client.get("/api/whoami").json()["prefix"] == "-"
