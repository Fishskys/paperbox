"""REST auth end to end: the read door, the master switch, the tiers, the prefix.

The composition under test mirrors ``app.main``: ``AuthContextMiddleware`` resolves
the caller once (and binds ``key_prefix`` into the logging context), then the
route's role dependency enforces the tier. Keys come from the in-memory SQLite
``api_keys`` table — the same store MCP reads.

The gate matrix is driven on purpose-built probe routes rather than on the real
routers: it pins the *gate* contract (plan D4) independently of which endpoint
declares which tier, which is ``tests/test_public_reads.py``'s job. The papers
router keeps its own end-to-end cases (``DELETE`` is a ``write`` endpoint, so the
highest tier a caller needs for it is ``write`` — plan D8).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import papers as papers_api
from app.core import security
from app.core.config import settings
from app.core.logging import get_key_prefix
from app.db.session import get_db
from app.main import AuthContextMiddleware
from app.services import api_key_service

#: Credential states a probe can present.
NONE = "none"
INVALID = "invalid"
READ = "read"
WRITE = "write"
ADMIN = "admin"
CREDENTIALS = (NONE, INVALID, READ, WRITE, ADMIN)

#: Plan D4 with ``AUTH_ENABLED=true``: reads answer per policy, writes need
#: ``write``, bulk reindex needs ``admin``. The two rows that matter for the
#: invariant of plan D2 are the last two columns — they never change with the
#: policy.
MATRIX = {
    NONE: {"public_read": 200, "key_read": 401, "write": 401, "admin": 401},
    INVALID: {"public_read": 200, "key_read": 403, "write": 403, "admin": 403},
    READ: {"public_read": 200, "key_read": 200, "write": 403, "admin": 403},
    WRITE: {"public_read": 200, "key_read": 200, "write": 200, "admin": 403},
    ADMIN: {"public_read": 200, "key_read": 200, "write": 200, "admin": 200},
}

#: Probe routes: one per tier, no handler side effects ("pass" is always 200).
READ_PATH = "/api/probe/read"
WRITE_PATH = "/api/probe/write"
ADMIN_PATH = "/api/probe/admin"


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
        for prefix, role in (
            ("readkey", "read"),
            ("writekey", "write"),
            ("adminkey", "admin"),
        ):
            _, full_key = api_key_service.create_key(setup, prefix=prefix, role=role)
            material[role] = full_key
        setup.commit()
    finally:
        setup.close()
    return material


def _probe_router() -> APIRouter:
    """Three routes, one per tier, so the matrix reads exactly like plan D4."""
    router = APIRouter()

    @router.get(READ_PATH, dependencies=[Depends(security.require_read)])
    def read_probe() -> dict:
        return {"tier": "read"}

    @router.post(WRITE_PATH, dependencies=[Depends(security.require_write)])
    def write_probe() -> dict:
        return {"tier": "write"}

    @router.post(ADMIN_PATH, dependencies=[Depends(security.require_admin)])
    def admin_probe() -> dict:
        return {"tier": "admin"}

    return router


def _probe_app(session_factory) -> FastAPI:
    """The probe router behind the production middleware (no auth overrides)."""
    app = FastAPI()
    app.include_router(_probe_router())
    app.add_middleware(AuthContextMiddleware)
    return app


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


def _headers(credential: str, key_material: dict[str, str]) -> dict[str, str]:
    if credential in (NONE,):
        return {}
    if credential == INVALID:
        return {"Authorization": "Bearer not-a-key"}
    return {"Authorization": f"Bearer {key_material[credential]}"}


def _statuses(client: TestClient, credential: str, key_material: dict[str, str]) -> dict:
    """One column of plan D4: the three surfaces, answered with one credential."""
    headers = _headers(credential, key_material)
    return {
        "read": client.get(READ_PATH, headers=headers).status_code,
        "write": client.post(WRITE_PATH, headers=headers).status_code,
        "admin": client.post(ADMIN_PATH, headers=headers).status_code,
    }


@pytest.fixture()
def client(key_material, session_factory):
    with TestClient(_probe_app(session_factory)) as test_client:
        yield test_client


@pytest.mark.parametrize("policy", ["public", "key"])
@pytest.mark.parametrize("credential", CREDENTIALS)
def test_the_gate_matrix(
    monkeypatch, client, key_material, policy: str, credential: str
) -> None:
    """Plan D4 verbatim: one case per cell of the table."""
    monkeypatch.setattr(settings, "auth_read_access", policy)
    expected = MATRIX[credential]
    observed = _statuses(client, credential, key_material)
    assert observed["read"] == expected[f"{policy}_read"]
    assert observed["write"] == expected["write"]
    assert observed["admin"] == expected["admin"]


@pytest.mark.parametrize("credential", CREDENTIALS)
def test_the_write_and_admin_surfaces_ignore_the_read_door(
    monkeypatch, client, key_material, credential: str
) -> None:
    """Plan D2 invariant: only the read column moves when the policy moves."""
    monkeypatch.setattr(settings, "auth_read_access", "public")
    open_door = _statuses(client, credential, key_material)
    monkeypatch.setattr(settings, "auth_read_access", "key")
    closed_door = _statuses(client, credential, key_material)
    assert open_door["write"] == closed_door["write"]
    assert open_door["admin"] == closed_door["admin"]


def test_an_anonymous_caller_on_a_write_is_a_401_with_a_challenge(client) -> None:
    """D4 judgement 1: the caller may still present a key, so 401 (not 403)."""
    response = client.post(WRITE_PATH)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "Missing API key"


def test_an_anonymous_caller_on_the_admin_surface_is_a_401(client) -> None:
    response = client.post(ADMIN_PATH)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_read_key_is_told_its_tier_is_too_low(client, key_material) -> None:
    """403 with the tier in the message: "ask for a stronger key", not "rotate"."""
    response = client.post(
        WRITE_PATH, headers=_headers(READ, key_material)
    )
    assert response.status_code == 403
    assert "insufficient role" in response.json()["detail"]
    assert "read" in response.json()["detail"]


def test_an_unknown_key_on_a_write_is_a_403(client, key_material) -> None:
    response = client.post(WRITE_PATH, headers=_headers(INVALID, key_material))
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid API key"


def test_the_master_switch_off_opens_every_surface(monkeypatch, session_factory) -> None:
    """D2: with ``AUTH_ENABLED=false`` the policy is inert — the same 200s."""
    monkeypatch.setattr(settings, "paper_api_key", "")
    monkeypatch.setattr(settings, "paper_api_keys", "")
    monkeypatch.setattr(settings, "auth_enabled", False)
    with TestClient(_probe_app(session_factory)) as client:
        for policy in ("public", "key"):
            monkeypatch.setattr(settings, "auth_read_access", policy)
            assert _statuses(client, NONE, {}) == {
                "read": 200,
                "write": 200,
                "admin": 200,
            }


def test_a_read_needs_no_credential_by_default(client) -> None:
    response = client.get(READ_PATH)
    assert response.status_code == 200
    assert response.headers.get("www-authenticate") is None


def test_an_invalid_credential_is_ignored_on_a_read(client, key_material) -> None:
    """B2 (2026-10-12): a stale key must not turn a public read into a 403."""
    response = client.get(READ_PATH, headers=_headers(INVALID, key_material))
    assert response.status_code == 200
    assert response.headers.get("www-authenticate") is None


def test_a_key_policy_read_without_a_credential_is_a_401(
    monkeypatch, client, key_material
) -> None:
    monkeypatch.setattr(settings, "auth_read_access", "key")
    response = client.get(READ_PATH)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_key_policy_read_with_an_unknown_credential_is_a_403(
    monkeypatch, client, key_material
) -> None:
    monkeypatch.setattr(settings, "auth_read_access", "key")
    response = client.get(READ_PATH, headers=_headers(INVALID, key_material))
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid API key"


def test_a_key_policy_read_with_a_read_key_passes(
    monkeypatch, client, key_material
) -> None:
    monkeypatch.setattr(settings, "auth_read_access", "key")
    response = client.get(READ_PATH, headers=_headers(READ, key_material))
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# the real papers router (per-endpoint behaviour, not the gate contract)
# --------------------------------------------------------------------------- #
def test_the_papers_router_serves_a_public_read(key_material, session_factory) -> None:
    with TestClient(_papers_app(session_factory)) as client:
        response = client.get("/api/papers")
    assert response.status_code == 200
    assert response.json() == {"total": 0, "limit": 20, "offset": 0, "papers": []}


def test_a_write_key_can_delete_a_paper(key_material, session_factory) -> None:
    """D8: deletion is a ``write`` endpoint; the paper does not exist → 404."""
    with TestClient(_papers_app(session_factory)) as client:
        response = client.delete(
            f"/api/papers/{uuid.uuid4()}", headers=_headers(WRITE, key_material)
        )
    assert response.status_code == 404


def test_an_admin_key_can_delete_a_paper(key_material, session_factory) -> None:
    with TestClient(_papers_app(session_factory)) as client:
        response = client.delete(
            f"/api/papers/{uuid.uuid4()}", headers=_headers(ADMIN, key_material)
        )
    assert response.status_code == 404


def test_a_read_key_cannot_delete_a_paper(key_material, session_factory) -> None:
    with TestClient(_papers_app(session_factory)) as client:
        response = client.delete(
            f"/api/papers/{uuid.uuid4()}", headers=_headers(READ, key_material)
        )
    assert response.status_code == 403
    assert "insufficient role" in response.json()["detail"]


def test_a_delete_without_a_credential_is_a_401(key_material, session_factory) -> None:
    with TestClient(_papers_app(session_factory)) as client:
        response = client.delete(f"/api/papers/{uuid.uuid4()}")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_an_unknown_credential_on_a_delete_is_a_403(
    key_material, session_factory
) -> None:
    with TestClient(_papers_app(session_factory)) as client:
        response = client.delete(
            f"/api/papers/{uuid.uuid4()}", headers=_headers(INVALID, key_material)
        )
    assert response.status_code == 403


def test_the_x_api_key_header_still_works(key_material, session_factory) -> None:
    """The header fallback is a *write* path now; the write key passes the gate."""
    with TestClient(_papers_app(session_factory)) as client:
        response = client.delete(
            f"/api/papers/{uuid.uuid4()}",
            headers={"X-API-Key": key_material[WRITE]},
        )
    assert response.status_code == 404


def test_auth_off_answers_anonymously(monkeypatch, session_factory) -> None:
    """D2: the switch is all-or-nothing — off, every caller is an admin."""
    monkeypatch.setattr(settings, "paper_api_key", "")
    monkeypatch.setattr(settings, "paper_api_keys", "")
    monkeypatch.setattr(settings, "auth_enabled", False)
    with TestClient(_papers_app(session_factory)) as client:
        assert client.get("/api/papers").status_code == 200
        assert client.get(f"/api/papers/{uuid.uuid4()}").status_code == 404


# --------------------------------------------------------------------------- #
# the log prefix (requirement 3, end to end)
# --------------------------------------------------------------------------- #
def _prefix_app() -> FastAPI:
    router = APIRouter()

    @router.get("/api/whoami")
    def whoami() -> dict:
        return {"prefix": get_key_prefix() or "-"}

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(AuthContextMiddleware)
    return app


def test_the_middleware_binds_the_key_prefix_for_logs(
    monkeypatch, key_material
) -> None:
    """A handler sees the caller's prefix instead of nothing."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    with TestClient(_prefix_app()) as client:
        assert (
            client.get(
                "/api/whoami",
                headers=_headers(READ, key_material),
            ).json()["prefix"]
            == "readkey"
        )


def test_an_open_read_is_attributed_to_anonymous(monkeypatch, key_material) -> None:
    """G3: the anonymous caller owns a name now, so the log stops saying nothing."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_read_access", "public")
    with TestClient(_prefix_app()) as client:
        assert client.get("/api/whoami").json()["prefix"] == "anonymous"


def test_a_closed_read_door_binds_nothing(monkeypatch, key_material) -> None:
    """``key`` mode: a caller without a credential has no prefix to bind."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_read_access", "key")
    with TestClient(_prefix_app()) as client:
        assert client.get("/api/whoami").json()["prefix"] == "-"
