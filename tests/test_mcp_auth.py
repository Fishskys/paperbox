"""MCP auth: bearer-only, per-agent names, and the ordering against the Host allowlist.

Contract: ``docs/architecture/11-mcp-agent-interface.md`` section 4.

The composed app here mirrors ``app.main`` (middleware added, then the MCP app
mounted), because the thing under test *is* that composition: raw ASGI middleware in
front of a mounted sub-application, with the agent identity handed to the tool layer
through a context variable.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings, settings
from app.mcp import auth, server as mcp_server_module
from app.mcp.models import Envelope
from app.mcp.server import build_server, build_streamable_http_app

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
#: ``params`` of the ``initialize`` call (the helper builds the JSON-RPC envelope).
INITIALIZE_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "pytest", "version": "0"},
}
HERMES_KEY = "hermes-key-1"
CODEX_KEY = "codex-key-2"
SHARED_KEY = "shared-key"


@pytest.fixture()
def keys(monkeypatch):
    """Two named agents plus the shared key (the deployment's usual shape)."""
    monkeypatch.setattr(settings, "paper_api_key", SHARED_KEY)
    monkeypatch.setattr(
        settings, "paper_api_keys", f"hermes:{HERMES_KEY};codex:{CODEX_KEY}"
    )
    monkeypatch.setattr(settings, "mcp_allowed_hosts", "testserver,testserver:*")
    monkeypatch.setattr(settings, "auth_enabled", True)
    assert settings.agent_keys == {"hermes": HERMES_KEY, "codex": CODEX_KEY}
    return settings


@pytest.fixture()
def mcp_http(keys) -> TestClient:
    """The production composition: auth middleware outermost, MCP mounted at /mcp."""
    server = build_server()
    app = FastAPI()

    @asynccontextmanager
    async def lifespan(_):
        async with server.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
    app.add_middleware(auth.McpAuthMiddleware)
    app.mount("/mcp", build_streamable_http_app(server))
    with TestClient(app) as client:
        yield client


def rpc(
    client: TestClient,
    method: str,
    params: dict | None = None,
    *,
    token: str | None = None,
    host: str | None = None,
    query: str = "",
):
    headers = dict(HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if host is not None:
        headers["Host"] = host
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(f"/mcp/{query}", content=json.dumps(body), headers=headers)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def test_enabling_mcp_without_any_credential_refuses_to_start() -> None:
    """Fail fast: an authenticated MCP endpoint with no key is not deployable."""
    with pytest.raises(Exception) as failure:
        Settings(
            mcp_enabled=True,
            auth_enabled=True,
            mcp_allowed_hosts="127.0.0.1",
            paper_api_key="",
            paper_api_keys="",
        )
    assert "credential" in str(failure.value)


def test_enabling_auth_with_the_default_key_refuses_to_start() -> None:
    """'change-me' is a public literal: it must never guard an authed service."""
    with pytest.raises(Exception) as failure:
        Settings(auth_enabled=True, paper_api_key="change-me", paper_api_keys="")
    assert "change-me" in str(failure.value)


def test_enabling_mcp_with_auth_off_needs_no_credential() -> None:
    """AUTH_ENABLED is the master switch: off, MCP needs no key at all (D1)."""
    Settings(
        mcp_enabled=True,
        auth_enabled=False,
        mcp_allowed_hosts="127.0.0.1",
        paper_api_key="",
        paper_api_keys="",
    )  # no raise


def test_keys_are_parsed_into_agent_names(keys) -> None:
    assert keys.agent_keys["hermes"] == HERMES_KEY
    assert keys.agent_keys["codex"] == CODEX_KEY


def test_a_key_shared_with_paper_api_key_names_the_agent(monkeypatch, caplog) -> None:
    """``PAPER_API_KEYS`` wins over the shared key -- loudly, so it is not a mystery."""
    monkeypatch.setattr(settings, "paper_api_key", SHARED_KEY)
    monkeypatch.setattr(settings, "paper_api_keys", f"hermes:{SHARED_KEY}")
    clashes = auth.warn_about_shared_keys()
    assert clashes == ["hermes"]
    identity = auth.resolve_agent(SHARED_KEY)
    assert identity is not None and identity.name == "hermes"
    assert identity.source == auth.SOURCE_KEYS


# --------------------------------------------------------------------------- #
# the identity mapping
# --------------------------------------------------------------------------- #
def test_parse_bearer_rejects_other_schemes(keys) -> None:
    assert auth.parse_bearer("Bearer abc") == "abc"
    assert auth.parse_bearer("bearer  abc ") == "abc"
    assert auth.parse_bearer("Basic abc") is None
    assert auth.parse_bearer("Bearer") is None
    assert auth.parse_bearer(None) is None
    assert auth.parse_bearer("") is None


def test_resolve_agent_covers_named_shared_and_unknown(keys) -> None:
    named = auth.resolve_agent(HERMES_KEY)
    assert named is not None and named.name == "hermes" and not named.is_shared
    shared = auth.resolve_agent(SHARED_KEY)
    assert shared is not None and shared.name == auth.SHARED_AGENT and shared.is_shared
    assert auth.resolve_agent("nope") is None
    assert auth.resolve_agent("") is None


# --------------------------------------------------------------------------- #
# the endpoint
# --------------------------------------------------------------------------- #
def test_a_named_key_authenticates_and_is_carried_into_the_response(mcp_http) -> None:
    response = rpc(mcp_http, "initialize", INITIALIZE_PARAMS, token=HERMES_KEY)
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "paperbox"
    # The identity is what the audit line and every envelope.meta reports.
    assert auth.current_agent.get() is None  # request-scoped, not leaked


def test_agent_name_reaches_the_tool_envelope(mcp_http) -> None:
    """``Envelope.meta.agent`` must say who called, not 'default'."""
    rpc(mcp_http, "initialize", INITIALIZE_PARAMS, token=CODEX_KEY)
    listing = rpc(mcp_http, "tools/list", {}, token=CODEX_KEY).json()["result"]["tools"]
    assert "paper_job_status" in {tool["name"] for tool in listing}

    # Call a tool whose data path is stubbed at the repository boundary; only the
    # identity plumbing is under test here.
    from app.mcp import jobs
    from app.schemas.job import JobOut

    job = JobOut(job_id="job-1", stage="COMPLETED", progress=1.0)
    original = jobs.load_job
    jobs.load_job = lambda job_id: job
    try:
        payload = rpc(
            mcp_http,
            "tools/call",
            {"name": "paper_job_status", "arguments": {"job_id": "job-1"}},
            token=CODEX_KEY,
        ).json()["result"]
    finally:
        jobs.load_job = original
    envelope = Envelope[object].model_validate(payload["structuredContent"])
    assert envelope.meta.agent == "codex"


def test_the_shared_key_reports_the_default_agent(mcp_http) -> None:
    response = rpc(mcp_http, "initialize", INITIALIZE_PARAMS, token=SHARED_KEY)
    assert response.status_code == 200


def test_a_missing_header_is_a_401_with_a_challenge(mcp_http) -> None:
    response = rpc(mcp_http, "initialize", INITIALIZE_PARAMS)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert "Authorization" in response.json()["error"]


def test_an_unknown_key_is_a_403(mcp_http) -> None:
    response = rpc(mcp_http, "initialize", INITIALIZE_PARAMS, token="not-a-key")
    assert response.status_code == 403


def test_a_non_bearer_scheme_is_refused(mcp_http) -> None:
    response = mcp_http.post(
        "/mcp/",
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INITIALIZE_PARAMS}),
        headers={**HEADERS, "Authorization": f"Basic {HERMES_KEY}"},
    )
    assert response.status_code == 401


def test_a_key_in_the_query_string_does_not_authenticate(mcp_http) -> None:
    """v1 explicitly does not support ``?key=``: keys in URLs end up in logs."""
    response = rpc(mcp_http, "initialize", INITIALIZE_PARAMS, query=f"?key={HERMES_KEY}")
    assert response.status_code == 401


def test_auth_runs_before_the_host_allowlist(mcp_http) -> None:
    """Ordering: an unauthenticated caller learns nothing about our Host policy.

    A foreign Host without credentials is 401 (not 421); with credentials it is 421,
    which is the transport's own decision (hard requirement 2).
    """
    anonymous = rpc(mcp_http, "initialize", INITIALIZE_PARAMS, host="evil.example")
    assert anonymous.status_code == 401

    authenticated = rpc(mcp_http, "initialize", INITIALIZE_PARAMS, token=HERMES_KEY, host="evil.example")
    assert authenticated.status_code == 421


def test_authenticated_traffic_from_an_allowed_host_is_served(mcp_http) -> None:
    response = rpc(
        mcp_http, "initialize", INITIALIZE_PARAMS, token=HERMES_KEY, host="testserver"
    )
    assert response.status_code == 200


def test_rest_routes_keep_their_own_auth(monkeypatch) -> None:
    """The MCP guard must not have relaxed (or tightened) the REST surface."""
    from app.main import app as paperbox_app

    monkeypatch.setattr(settings, "mcp_allowed_hosts", "testserver,testserver:*")
    monkeypatch.setattr(settings, "auth_enabled", True)
    with TestClient(paperbox_app) as client:
        assert client.get("/health").status_code == 200
        # /api/* is still guarded by the REST dependency, whatever the MCP keys are.
        assert client.get("/api/papers").status_code in (401, 403)


# --------------------------------------------------------------------------- #
# the master switch, all-on/all-off (plan §3 D1)
# --------------------------------------------------------------------------- #
def test_auth_off_the_endpoint_answers_anonymously(monkeypatch, keys) -> None:
    """AUTH_ENABLED=false: no credential, and the identity is anonymous admin."""
    monkeypatch.setattr(settings, "auth_enabled", False)
    server = build_server()
    app = FastAPI()

    @asynccontextmanager
    async def lifespan(_):
        async with server.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
    app.add_middleware(auth.McpAuthMiddleware)
    app.mount("/mcp", build_streamable_http_app(server))
    with TestClient(app) as client:
        response = rpc(client, "initialize", INITIALIZE_PARAMS, token=None)
        assert response.status_code == 200
    identity = auth.current_agent.get()
    assert identity is None  # contextvar is request-scoped; nothing leaked


def test_anonymous_identity_is_an_admin_with_the_anonymous_prefix() -> None:
    from app.services import api_key_service

    identity = api_key_service.anonymous()
    assert identity.role == api_key_service.ROLE_ADMIN
    assert identity.prefix == "anonymous"
    assert identity.source == api_key_service.SOURCE_ANONYMOUS


def test_a_database_key_authenticates_through_the_shared_service(
    monkeypatch, session_factory
) -> None:
    """A key created in the table works on the MCP surface too (G4)."""
    from app.services import api_key_service

    monkeypatch.setattr(settings, "paper_api_key", "")
    monkeypatch.setattr(settings, "paper_api_keys", "")
    session = session_factory()
    try:
        _, full_key = api_key_service.create_key(
            session, name="ci-agent", prefix="ciagent", role="read"
        )
        session.commit()
        identity = api_key_service.authenticate(session, full_key)
        assert identity is not None
        assert identity.name == "ci-agent"
        assert identity.prefix == "ciagent"
        assert identity.role == "read"
        assert identity.source == api_key_service.SOURCE_DB
        # and through the MCP wrapper, with the session the middleware would pass
        assert auth.resolve_agent(full_key, session).name == "ci-agent"
    finally:
        session.close()


def test_a_revoked_database_key_stops_authenticating(monkeypatch, session_factory) -> None:
    from app.services import api_key_service

    monkeypatch.setattr(settings, "paper_api_key", "")
    monkeypatch.setattr(settings, "paper_api_keys", "")
    session = session_factory()
    try:
        _, full_key = api_key_service.create_key(
            session, name="temp", prefix="temp", role="write"
        )
        session.commit()
        assert api_key_service.authenticate(session, full_key) is not None
        api_key_service.revoke_key(session, "temp")
        session.commit()
        assert api_key_service.authenticate(session, full_key) is None
    finally:
        session.close()
