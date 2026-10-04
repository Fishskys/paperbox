"""MCP transport contract: allowlist, stateless HTTP, lifespan, tool listing.

These tests pin the three things that decide whether a LAN-shared MCP endpoint works
at all (``docs/architecture/11-mcp-agent-interface.md`` section 2):

* the ``Host`` allowlist is **explicit** -- the SDK default accepts localhost only and
  answers everything else with a bare-text ``421`` that no client can explain;
* an empty allowlist is a **startup error**, not a silent fallback;
* the Streamable HTTP session manager is owned by the **host** app's lifespan -- a
  mounted sub-application's lifespan never runs.

The tests mount the MCP app exactly the way ``app.main`` does (``streamable_http_path``
is ``"/"`` inside the sub-application, so its route only exists once mounted), and
nothing here touches PostgreSQL/OpenSearch/MinIO: the job lookup is stubbed at the
boundary the tool layer owns.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette

from app.core.config import Settings
from app.mcp import server as mcp_server_module
from app.mcp import tools_read
from app.mcp.models import Envelope, ToolMeta
from app.mcp.server import build_server, build_streamable_http_app
from app.schemas.job import JobOut

INITIALIZE_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "pytest", "version": "0"},
}

#: Headers a Streamable HTTP client sends (both content types are advertised even
#: when the server answers with a single JSON body).
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

#: Two envelopes used to exercise the typed output schema without a database.
SAMPLE_ENVELOPE = Envelope[JobOut](
    data=JobOut(job_id="job-1", paper_id="paper-1", stage="COMPLETED", progress=1.0),
    meta=ToolMeta(tool="paper_job_status", agent="pytest", toolset="v1", took_ms=3),
)


def _settings(**overrides) -> Settings:
    """Build settings with a working allowlist unless a test says otherwise."""
    values = {"mcp_enabled": True, "mcp_allowed_hosts": "testserver,testserver:*"}
    values.update(overrides)
    return Settings(**values)


def _mount_mcp(*, with_session_manager: bool = True) -> Starlette:
    """Mounted layout mirroring ``app.main`` (``/mcp`` + host-owned lifespan)."""
    server = build_server()
    app = Starlette()
    if with_session_manager:

        @asynccontextmanager
        async def lifespan(_):
            async with server.session_manager.run():
                yield

        app.router.lifespan_context = lifespan
    app.mount("/mcp", build_streamable_http_app(server))
    return app


def _rpc(client: TestClient, method: str, params: dict | None = None, *, host: str | None = None):
    headers = dict(MCP_HEADERS)
    if host is not None:
        headers["Host"] = host
    body: dict = {"jsonrpc": "2.0", "id": 2, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp/", content=json.dumps(body), headers=headers)


@pytest.fixture
def mcp_app(monkeypatch) -> Starlette:
    monkeypatch.setattr(mcp_server_module, "settings", _settings())
    return _mount_mcp()


# --------------------------------------------------------------------------- config


def test_empty_allowlist_refuses_to_start() -> None:
    """``MCP_ENABLED=true`` without hosts must fail loudly (no SDK default fallback)."""
    with pytest.raises(Exception) as failure:
        Settings(mcp_enabled=True, mcp_allowed_hosts="   ")
    assert "MCP_ALLOWED_HOSTS" in str(failure.value)


def test_allowlist_is_parsed_and_blanks_dropped() -> None:
    parsed = _settings(mcp_allowed_hosts=" 127.0.0.1 ,, 10.0.0.5:*,host.lan").mcp_allowed_host_list
    assert parsed == ["127.0.0.1", "10.0.0.5:*", "host.lan"]


def test_build_app_requires_an_explicit_allowlist(monkeypatch) -> None:
    """Belt and braces: the builder itself refuses to rely on the SDK default."""
    monkeypatch.setattr(
        mcp_server_module, "settings", Settings(mcp_enabled=False, mcp_allowed_hosts="")
    )
    with pytest.raises(RuntimeError) as failure:
        build_streamable_http_app()
    assert "MCP_ALLOWED_HOSTS" in str(failure.value)


# ------------------------------------------------------------------------ transport


def test_host_outside_the_allowlist_gets_421(mcp_app: Starlette) -> None:
    """The negative half of the contract: a foreign Host is refused, and *loudly*."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INITIALIZE_PARAMS}
    )
    with TestClient(mcp_app) as client:
        allowed = client.post(
            "/mcp/", content=payload, headers={**MCP_HEADERS, "Host": "testserver"}
        )
        refused = client.post(
            "/mcp/", content=payload, headers={**MCP_HEADERS, "Host": "evil.example"}
        )
    assert allowed.status_code == 200
    assert refused.status_code == 421


def test_session_manager_must_be_entered_by_the_host_lifespan(monkeypatch) -> None:
    """Mount it without entering the manager: the first call fails.

    Regression guard for the one line everyone forgets in ``app.main.lifespan`` --
    a mounted sub-application's own lifespan is dead code.
    """
    monkeypatch.setattr(mcp_server_module, "settings", _settings())
    app = _mount_mcp(with_session_manager=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = _rpc(client, "initialize", INITIALIZE_PARAMS)
    assert response.status_code >= 500


def test_mounting_with_the_manager_running_serves_calls(mcp_app: Starlette) -> None:
    with TestClient(mcp_app) as client:
        response = _rpc(client, "initialize", INITIALIZE_PARAMS)
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "paperbox"


def test_calls_do_not_need_a_session_id(mcp_app: Starlette) -> None:
    """Stateless HTTP: two independent calls, no ``Mcp-Session-Id`` handshake."""
    with TestClient(mcp_app) as client:
        first = _rpc(client, "initialize", INITIALIZE_PARAMS)
        second = _rpc(client, "tools/list", {})
    assert first.status_code == 200
    assert second.status_code == 200
    assert "tools" in second.json()["result"]


# --------------------------------------------------------------------------- tools


def test_job_status_tool_is_listed_with_its_schema_and_toolset(mcp_app: Starlette) -> None:
    with TestClient(mcp_app) as client:
        _rpc(client, "initialize", INITIALIZE_PARAMS)
        listing = _rpc(client, "tools/list", {}).json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in listing}
    assert "paper_job_status" in by_name
    tool = by_name["paper_job_status"]
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"job_id", "wait_seconds"}
    assert schema["required"] == ["job_id"]
    # No bare `object` anywhere: the agent must see the shape (contract section 5).
    for name, prop in schema["properties"].items():
        assert prop.get("type") != "object", f"{name} has an untyped object schema"


def test_output_schema_bans_bare_objects() -> None:
    """The tool output schema is generated from the typed envelope, not hand-written."""
    schema = SAMPLE_ENVELOPE.model_json_schema()
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"data", "meta", "warnings", "citations"}


def test_job_status_returns_an_envelope(mcp_app: Starlette, monkeypatch) -> None:
    """One successful call end to end, with the database boundary stubbed."""
    job = JobOut(job_id="job-1", paper_id="paper-1", stage="COMPLETED", progress=1.0)
    monkeypatch.setattr(tools_read, "_load_job", lambda job_id: job)
    with TestClient(mcp_app) as client:
        _rpc(client, "initialize", INITIALIZE_PARAMS)
        response = _rpc(
            client, "tools/call", {"name": "paper_job_status", "arguments": {"job_id": "job-1"}}
        )
    assert response.status_code == 200
    payload = response.json()["result"]
    assert payload.get("isError") in (None, False)
    structured = payload["structuredContent"]
    assert structured["data"]["job_id"] == "job-1"
    assert structured["data"]["stage"] == "COMPLETED"
    assert structured["meta"]["tool"] == "paper_job_status"
    assert structured["warnings"] == []
    assert structured["citations"] == []


def test_unknown_job_id_reports_the_contract_error_code(mcp_app: Starlette, monkeypatch) -> None:
    """Failures arrive as ``isError`` + a code the caller can branch on."""
    from app.mcp import errors

    def _missing(job_id: str):
        raise errors.not_found("job", job_id)

    monkeypatch.setattr(tools_read, "_load_job", _missing)
    with TestClient(mcp_app) as client:
        _rpc(client, "initialize", INITIALIZE_PARAMS)
        response = _rpc(
            client, "tools/call", {"name": "paper_job_status", "arguments": {"job_id": "nope"}}
        )
    payload = response.json()["result"]
    assert payload.get("isError") is True
    # The SDK's failure channel is text only, so the contract's error object has to
    # survive inside it (prefixed by the SDK with the tool name).
    text = payload["content"][0]["text"]
    assert text.startswith("Error executing tool paper_job_status")
    body = json.loads(text.split(": ", 1)[1])
    assert body["error"]["code"] == "NOT_FOUND"
    assert "nope" in body["error"]["message"]
    assert body["error"]["retryable"] is False
    assert "content" not in json.dumps(body)  # never the whole payload back at us
