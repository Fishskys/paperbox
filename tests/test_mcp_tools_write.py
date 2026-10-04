"""MCP writing tools: switch matrix, dry-run previews, and audit-worthy results.

Contract: ``docs/architecture/11-mcp-agent-interface.md`` sections 1.1 and 5.2.

Two properties matter most here and both are asserted directly:

* a tool whose switch is off is **not registered at all** (it never shows up in
  ``tools/list``, so it cannot be discovered or guessed);
* ``dry_run=true`` (the default for delete/reindex) makes **zero** mutating service
  calls -- the test replaces the service function with one that raises, so a stray
  write fails the test instead of passing quietly.
"""

from __future__ import annotations

import contextlib
import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette

from app.core.config import settings
from app.mcp import jobs
from app.mcp import server as mcp_server_module
from app.mcp import tools_write
from app.mcp.server import build_server, build_streamable_http_app
from app.schemas.job import JobOut

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "pytest", "version": "0"},
}
WRITE_TOOLS = {"paper_import", "paper_reindex", "paper_delete", "paper_update_metadata"}


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def mounted(*, write: bool, delete: bool = False, reindex: bool = False, metadata: bool = False):
    """A TestClient over a freshly built MCP server with the given switches."""
    previous = (
        settings.mcp_write_enabled,
        settings.mcp_allow_delete,
        settings.mcp_allow_reindex,
        settings.mcp_allow_metadata_write,
    )
    settings.mcp_write_enabled = write
    settings.mcp_allow_delete = delete
    settings.mcp_allow_reindex = reindex
    settings.mcp_allow_metadata_write = metadata
    mcp_server_module.settings.mcp_allowed_hosts = "testserver,testserver:*"
    server = build_server()
    app = Starlette()

    @asynccontextmanager
    async def lifespan(_):
        async with server.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
    app.mount("/mcp", build_streamable_http_app(server))
    try:
        with TestClient(app) as client:
            _rpc(client, "initialize", INITIALIZE_PARAMS)
            yield client
    finally:
        (
            settings.mcp_write_enabled,
            settings.mcp_allow_delete,
            settings.mcp_allow_reindex,
            settings.mcp_allow_metadata_write,
        ) = previous


def _rpc(client: TestClient, method: str, params: dict | None = None):
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp/", content=json.dumps(body), headers=HEADERS)


def listing(client: TestClient) -> set[str]:
    tools = _rpc(client, "tools/list", {}).json()["result"]["tools"]
    return {tool["name"] for tool in tools}


def call(client: TestClient, tool: str, arguments: dict) -> dict:
    payload = _rpc(client, "tools/call", {"name": tool, "arguments": arguments}).json()["result"]
    if payload.get("isError"):
        raise AssertionError(payload["content"][0]["text"])
    return payload["structuredContent"]


def call_error(client: TestClient, tool: str, arguments: dict) -> dict:
    payload = _rpc(client, "tools/call", {"name": tool, "arguments": arguments}).json()["result"]
    assert payload.get("isError") is True, payload
    return json.loads(payload["content"][0]["text"].split(": ", 1)[1])


def _boom(*_args, **_kwargs):
    raise AssertionError("this service call must not happen on a dry run")


def _job(**overrides) -> JobOut:
    values = {"job_id": "job-1", "stage": "COMPLETED", "progress": 1.0, "paper_id": "paper-1"}
    values.update(overrides)
    return JobOut(**values)


@pytest.fixture()
def mcp_session(monkeypatch, session_factory):
    """A paper with two chunks and an original file, served by an SQLite session.

    Only the repository/engine boundary is stubbed (``SessionLocal``), so the tools
    exercise the real services -- the same ones the REST endpoints call.
    """
    monkeypatch.setattr(tools_write, "SessionLocal", session_factory)
    monkeypatch.setattr(
        tools_write.paper_service, "original_file", lambda paper: _record(paper.id)
    )
    paper_id = _make_paper(session_factory, title="Original title", chunks=2)
    return {"paper_id": paper_id}


def _make_paper(session_factory, *, title: str, chunks: int) -> str:
    from app.db.models import Paper, PaperChunk

    session = session_factory()
    try:
        paper = Paper(title=title, fingerprint=f"fp-{title}")
        session.add(paper)
        session.flush()
        for index in range(chunks):
            session.add(
                PaperChunk(
                    paper_id=paper.id,
                    chunk_index=index,
                    page_start=index + 1,
                    page_end=index + 1,
                    section=f"section-{index}",
                    text="x" * 40,
                    char_count=40,
                )
            )
        session.commit()
        return paper.id
    finally:
        session.close()


def _record(paper_id: str):
    """A ``PaperFile`` stand-in with the fields the writing tools read."""

    class Record:
        object_key = f"papers/{paper_id}/original.pdf"
        bucket = "paperbox"
        filename = "original.pdf"
        content_type = "application/pdf"
        size_bytes = 1234

    return Record()


# --------------------------------------------------------------------------- #
# the switch matrix
# --------------------------------------------------------------------------- #
def test_no_write_tool_exists_while_the_master_switch_is_off() -> None:
    with mounted(write=False) as client:
        names = listing(client)
    assert names and not (names & WRITE_TOOLS), names


def test_each_switch_adds_exactly_its_own_tool() -> None:
    with mounted(write=True) as client:
        only_import = listing(client)
    assert WRITE_TOOLS & only_import == {"paper_import"}

    with mounted(write=True, delete=True) as client:
        assert WRITE_TOOLS & listing(client) == {"paper_import", "paper_delete"}

    with mounted(write=True, reindex=True) as client:
        assert WRITE_TOOLS & listing(client) == {"paper_import", "paper_reindex"}

    with mounted(write=True, metadata=True) as client:
        assert WRITE_TOOLS & listing(client) == {"paper_import", "paper_update_metadata"}

    with mounted(write=True, delete=True, reindex=True, metadata=True) as client:
        assert WRITE_TOOLS & listing(client) == WRITE_TOOLS


def test_a_hidden_tool_cannot_be_called_by_name() -> None:
    with mounted(write=True) as client:
        payload = _rpc(
            client,
            "tools/call",
            {"name": "paper_delete", "arguments": {"paper_id": "x"}},
        ).json()
    assert "error" in payload or payload["result"].get("isError") is True


def test_the_defensive_check_reports_write_disabled(monkeypatch) -> None:
    """Settings changing after registration must not silently allow a tool."""
    monkeypatch.setattr(settings, "mcp_write_enabled", False)
    with pytest.raises(Exception) as failure:
        tools_write.require(tools_write.ALLOW_DELETE, "paper_delete")
    assert "WRITE_DISABLED" in str(failure.value)
    assert "MCP_ALLOW_DELETE" in str(failure.value)


# --------------------------------------------------------------------------- #
# paper_import
# --------------------------------------------------------------------------- #
def test_import_dry_run_validates_a_url_without_queueing_it(monkeypatch) -> None:
    monkeypatch.setattr(tools_write.ingestion_service, "create_job", _boom)
    with mounted(write=True) as client:
        envelope = call(
            client,
            "paper_import",
            {"source": "https://93.184.216.34/paper.pdf", "dry_run": True},
        )
    assert envelope["data"]["queued"] is False
    assert envelope["data"]["source_type"] == "url"
    assert envelope["data"]["filename"] == "paper.pdf"
    assert any("dry run" in item for item in envelope["warnings"])


def test_import_refuses_a_private_address_with_a_useful_hint() -> None:
    with mounted(write=True) as client:
        error = call_error(
            client, "paper_import", {"source": "http://169.254.169.254/latest/meta-data/"}
        )
    assert error["error"]["code"] == "SSRF_BLOCKED"
    assert "INGEST_ALLOW_PRIVATE_HOSTS" in error["error"]["hint"]
    assert error["error"]["retryable"] is False


def test_import_refuses_a_local_path_outside_the_whitelist() -> None:
    with mounted(write=True) as client:
        error = call_error(
            client,
            "paper_import",
            {"source": "/etc/passwd", "source_type": "local_path"},
        )
    assert error["error"]["code"] in {"FORBIDDEN", "INVALID_ARGUMENT"}


def test_import_previews_a_whitelisted_local_pdf(tmp_path, monkeypatch) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 tiny")
    monkeypatch.setattr(settings, "ingest_local_roots", str(tmp_path))
    monkeypatch.setattr(tools_write.ingestion_service, "create_job", _boom)
    with mounted(write=True) as client:
        envelope = call(
            client,
            "paper_import",
            {"source": str(pdf), "source_type": "local_path", "dry_run": True},
        )
    assert envelope["data"]["resolved_path"] == str(pdf.resolve())
    assert envelope["data"]["size_bytes"] == len(b"%PDF-1.4 tiny")
    assert envelope["data"]["content_type"] == "application/pdf"


def test_import_queues_the_job_and_reports_it(monkeypatch) -> None:
    captured: dict = {}

    def fake_create_job(session, **kwargs):
        captured.update(kwargs)

        class Row:
            id = "job-42"
            payload: dict = {}

        return Row()

    monkeypatch.setattr(tools_write.ingestion_service, "create_job", fake_create_job)
    monkeypatch.setattr(tools_write, "_Session", _FakeSession)
    monkeypatch.setattr("app.workers.queue.submit", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(jobs, "wait_for_job", _completed_job)

    with mounted(write=True) as client:
        envelope = call(client, "paper_import", {"source": "https://93.184.216.34/p.pdf"})
    assert envelope["data"]["job_id"] == "job-42"
    assert envelope["data"]["status"] == "completed"
    assert captured["source_type"] == "url"
    assert captured["source"] == "https://93.184.216.34/p.pdf"


async def _completed_job(job_id: str, wait_seconds: int):
    return _job(job_id=job_id), 1


class _FakeSession:
    """Session stand-in: the writing tools only need commit/close here."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def commit(self) -> None:
        return None


def test_import_rejects_an_unknown_source_type() -> None:
    with mounted(write=True) as client:
        error = call_error(
            client, "paper_import", {"source": "x", "source_type": "ftp"}
        )
    assert error["error"]["code"] == "INVALID_ARGUMENT"


# --------------------------------------------------------------------------- #
# paper_reindex
# --------------------------------------------------------------------------- #
def test_reindex_dry_run_counts_without_queueing(mcp_session, monkeypatch) -> None:
    monkeypatch.setattr(
        tools_write.ingestion_service, "create_reindex_job", _boom
    )
    with mounted(write=True, reindex=True) as client:
        envelope = call(client, "paper_reindex", {"paper_id": mcp_session["paper_id"]})
    assert envelope["data"]["queued"] is False
    assert envelope["data"]["chunks"] == 2
    assert envelope["data"]["has_original_file"] is True
    assert envelope["data"]["running_jobs"] == 0
    assert any("dry run" in item for item in envelope["warnings"])


def test_reindex_needs_the_original_file(mcp_session, monkeypatch) -> None:
    monkeypatch.setattr(tools_write.paper_service, "original_file", lambda paper: None)
    with mounted(write=True, reindex=True) as client:
        error = call_error(client, "paper_reindex", {"paper_id": mcp_session["paper_id"]})
    assert error["error"]["code"] == "NOT_FOUND"


def test_reindex_applies_and_returns_the_job(mcp_session, monkeypatch) -> None:
    queued: dict = {}

    def fake_queue(session, paper, record):
        queued["paper_id"] = paper.id

        class Row:
            id = "job-77"

        return Row()

    monkeypatch.setattr(tools_write.ingestion_service, "create_reindex_job", fake_queue)
    monkeypatch.setattr(jobs, "wait_for_job", _completed_job)
    with mounted(write=True, reindex=True) as client:
        envelope = call(
            client,
            "paper_reindex",
            {"paper_id": mcp_session["paper_id"], "dry_run": False},
        )
    assert queued["paper_id"] == mcp_session["paper_id"]
    assert envelope["data"]["job_id"] == "job-77"


# --------------------------------------------------------------------------- #
# paper_delete
# --------------------------------------------------------------------------- #
def test_delete_dry_run_reports_the_blast_radius(mcp_session, monkeypatch) -> None:
    monkeypatch.setattr(tools_write.paper_service, "purge_paper", _boom)
    # ``delete_preview`` imports the storage module inside the function, so the patch
    # target is the module itself, not an attribute of ``paper_service``.
    from app.services import object_storage

    monkeypatch.setattr(
        object_storage,
        "list_objects",
        lambda prefix, *a, **k: [_FakeObject(100), _FakeObject(50)],
    )
    with mounted(write=True, delete=True) as client:
        envelope = call(client, "paper_delete", {"paper_id": mcp_session["paper_id"]})
    data = envelope["data"]
    assert data["deleted"] is False
    assert data["chunks"] == 2
    assert data["objects"] == 2
    assert data["object_bytes"] == 150
    assert any("dry run" in item for item in envelope["warnings"])


def test_delete_applies_only_when_asked(mcp_session, monkeypatch) -> None:
    calls: dict = {}

    class Outcome:
        chunks_removed = 2
        objects_removed = 2

    def fake_purge(session, paper):
        calls["paper_id"] = paper.id
        return Outcome()

    monkeypatch.setattr(tools_write.paper_service, "purge_paper", fake_purge)
    with mounted(write=True, delete=True) as client:
        envelope = call(
            client,
            "paper_delete",
            {"paper_id": mcp_session["paper_id"], "dry_run": False},
        )
    assert calls["paper_id"] == mcp_session["paper_id"]
    assert envelope["data"]["deleted"] is True
    assert envelope["data"]["objects_removed"] == 2


class _FakeObject:
    def __init__(self, size: int) -> None:
        self.size = size


# --------------------------------------------------------------------------- #
# paper_update_metadata
# --------------------------------------------------------------------------- #
def test_metadata_dry_run_shows_values_without_writing(mcp_session, monkeypatch) -> None:
    monkeypatch.setattr(tools_write.metadata_manual, "patch_metadata", _boom)
    with mounted(write=True, metadata=True) as client:
        envelope = call(
            client,
            "paper_update_metadata",
            {
                "paper_id": mcp_session["paper_id"],
                "fields": {"title": "Corrected title"},
                "dry_run": True,
            },
        )
    changes = envelope["data"]["changes"]
    assert changes["title"]["before"] == "Original title"
    assert changes["title"]["after"] == "Corrected title"
    assert envelope["data"]["applied"] is False
    assert any("dry run" in item for item in envelope["warnings"])


def test_metadata_apply_reports_before_and_after(mcp_session) -> None:
    with mounted(write=True, metadata=True) as client:
        envelope = call(
            client,
            "paper_update_metadata",
            {
                "paper_id": mcp_session["paper_id"],
                "fields": {"title": "New title", "year": 2019},
            },
        )
    data = envelope["data"]
    assert data["applied"] is True
    assert data["changes"]["title"] == {"before": "Original title", "after": "New title"}
    assert data["changes"]["year"]["after"] == 2019
    assert set(data["changed"]) >= {"title", "year"}
    assert data["rollback"].endswith(f"/api/papers/{mcp_session['paper_id']}/metadata/rollback")


def test_metadata_requires_at_least_one_field(mcp_session) -> None:
    with mounted(write=True, metadata=True) as client:
        error = call_error(
            client, "paper_update_metadata", {"paper_id": mcp_session["paper_id"], "fields": {}}
        )
    assert error["error"]["code"] == "INVALID_ARGUMENT"
