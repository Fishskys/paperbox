"""MCP read tools: budgets, paging, citations, and the signed download link.

The tests drive the tools through the real transport (mounted exactly like
``app.main`` does) and a real SQLAlchemy session on SQLite, with only the
repository/engine boundary stubbed. That is deliberate: the bugs this layer can
have are "the page is cut in the wrong place", "the citation lost its page" and
"the signed link does not work", and none of those show up against a mock of the
tool itself.

Contract: ``docs/architecture/11-mcp-agent-interface.md`` sections 3 and 5.1.
"""

from __future__ import annotations

import io
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette

from app.core.config import settings
from app.core.security import require_api_key
from app.db.models import Paper, PaperChunk
from app.db.session import get_db
from app.main import app as paperbox_app
from app.mcp import server as mcp_server_module
from app.mcp import tools_read
from app.mcp.models import ChunkView
from app.mcp.server import build_server, build_streamable_http_app
from app.schemas.search import (
    SearchEvidence,
    SearchRerankInfo,
    SearchResponse,
    SearchResult,
    SearchRewriteInfo,
)
from app.services import download_signing, search_pipeline, search_service

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "pytest", "version": "0"},
}
READ_TOOLS = {
    "paper_search",
    "paper_get",
    "paper_get_chunks",
    "paper_get_context",
    "paper_get_file",
    "paper_job_status",
}


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
@pytest.fixture()
def mcp_client(monkeypatch, session_factory) -> TestClient:
    """Mounted MCP endpoint backed by an in-memory database and a stub secret."""
    monkeypatch.setattr(mcp_server_module, "settings", _settings())
    monkeypatch.setattr(tools_read, "SessionLocal", session_factory)
    monkeypatch.setattr(settings, "mcp_download_secret", "test-secret")
    server = build_server()
    mounted = Starlette()

    @asynccontextmanager
    async def lifespan(_):
        async with server.session_manager.run():
            yield

    mounted.router.lifespan_context = lifespan
    mounted.mount("/mcp", build_streamable_http_app(server))
    with TestClient(mounted) as client:
        _rpc(client, "initialize", INITIALIZE)
        yield client


def _settings():
    values = settings.model_copy(deep=True)
    values.mcp_enabled = True
    values.mcp_allowed_hosts = "testserver,testserver:*"
    return values


def _rpc(client: TestClient, method: str, params: dict | None = None):
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp/", content=json.dumps(body), headers=HEADERS)


def call(client: TestClient, tool: str, arguments: dict | None = None) -> dict:
    """Call one tool; return the ``structuredContent`` envelope or raise on error."""
    payload = _rpc(
        client, "tools/call", {"name": tool, "arguments": arguments or {}}
    ).json()["result"]
    if payload.get("isError"):
        text = payload["content"][0]["text"]
        raise AssertionError(text)
    return payload["structuredContent"]


def call_error(client: TestClient, tool: str, arguments: dict | None = None) -> dict:
    """Call one tool expecting a contract error; return the parsed error object."""
    payload = _rpc(
        client, "tools/call", {"name": tool, "arguments": arguments or {}}
    ).json()["result"]
    assert payload.get("isError") is True, payload
    text = payload["content"][0]["text"]
    return json.loads(text.split(": ", 1)[1])


def make_paper(session_factory, *, title="A paper", chunks=3, text="x" * 40) -> str:
    """One paper with ``chunks`` chunks of ``text`` each, in reading order."""
    session = session_factory()
    try:
        paper = Paper(title=title, fingerprint=f"fp-{uuid.uuid4().hex}")
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
                    text=text,
                    char_count=len(text),
                )
            )
        session.commit()
        return paper.id
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #
def test_every_read_tool_is_listed_and_typed(mcp_client) -> None:
    tools = _rpc(mcp_client, "tools/list", {}).json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    assert READ_TOOLS <= set(by_name)

    for name in READ_TOOLS:
        tool = by_name[name]
        schema = tool["inputSchema"]
        assert schema["type"] == "object", name
        assert "ctx" not in schema["properties"], f"{name} leaks the SDK context object"
        assert tool.get("_meta", {}).get("toolset") == "v1"
        assert tool.get("outputSchema"), f"{name} has no typed output schema"
        _assert_no_bare_objects(schema, name)


def _assert_no_bare_objects(node, name: str, depth: int = 0) -> None:
    """Walk a JSON Schema and fail on an untyped object (contract invariant 10)."""
    if depth > 8 or not isinstance(node, dict):
        return
    if node.get("type") == "object":
        assert "properties" in node or "additionalProperties" in node, f"{name}: bare object"
    for value in node.values():
        if isinstance(value, dict):
            _assert_no_bare_objects(value, name, depth + 1)
        elif isinstance(value, list):
            for item in value:
                _assert_no_bare_objects(item, name, depth + 1)


# --------------------------------------------------------------------------- #
# paper_search
# --------------------------------------------------------------------------- #
def _fake_response() -> SearchResponse:
    return SearchResponse(
        query="q",
        mode="hybrid",
        backend="native",
        total=7,
        candidates=12,
        took_ms=5481.0,
        rerank=SearchRerankInfo(enabled=True, model="cross-encoder", took_ms=3000),
        rewrite=SearchRewriteInfo(enabled=False),
        results=[
            SearchResult(
                paper_id="p1",
                title="Paper one",
                score=0.9,
                relevance="high",
                evidence=[
                    SearchEvidence(
                        chunk_id="c1",
                        page=7,
                        section="III-B",
                        text="  the answer   is  here  " + "y" * 400,
                    )
                ],
            )
        ],
    )


def test_search_returns_the_response_and_expanded_citations(
    mcp_client, monkeypatch
) -> None:
    captured: dict = {}

    async def fake_run_search(request):
        captured["request"] = request
        return _fake_response()

    monkeypatch.setattr(search_pipeline, "run_search", fake_run_search)
    envelope = call(mcp_client, "paper_search", {"query": "low power SRAM"})

    # Same shape as POST /api/search, so an agent and a REST client see one contract.
    assert envelope["data"]["total"] == 7
    assert envelope["data"]["results"][0]["paper_id"] == "p1"
    assert envelope["meta"]["tool"] == "paper_search"
    # Citations carry what a readable reference needs, and quote is capped.
    citation = envelope["citations"][0]
    assert (citation["paper_id"], citation["page"], citation["section"]) == ("p1", 7, "III-B")
    assert citation["chunk_id"] == "c1"
    assert len(citation["quote"]) == 200
    assert citation["quote"].startswith("the answer is here")
    # The MCP default is the quality path: rerank on.
    assert captured["request"].rerank is True
    assert captured["request"].top_k == 10
    # 7 papers match but one was returned: the agent is told the result is partial.
    assert envelope["warnings"] == ["showing 1 of 7 matching papers"]


def test_search_warns_when_more_papers_match_than_are_returned(
    mcp_client, monkeypatch
) -> None:
    async def fake_run_search(request):
        return _fake_response()

    monkeypatch.setattr(search_pipeline, "run_search", fake_run_search)
    envelope = call(mcp_client, "paper_search", {"query": "q", "rerank": False})
    assert any("of 7 matching papers" in item for item in envelope["warnings"])


def test_search_rejects_an_out_of_range_top_k(mcp_client) -> None:
    error = call_error(mcp_client, "paper_search", {"query": "q", "top_k": 999})
    assert error["error"]["code"] == "INVALID_ARGUMENT"
    assert "top_k" in error["error"]["message"]


def test_search_maps_a_dead_backend_to_a_retryable_error(mcp_client, monkeypatch) -> None:
    async def boom(request):
        raise search_service.SearchError("connection refused")

    monkeypatch.setattr(search_pipeline, "run_search", boom)
    error = call_error(mcp_client, "paper_search", {"query": "q"})
    assert error["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert error["error"]["retryable"] is True
    assert error["error"]["retry_after"] == 2


# --------------------------------------------------------------------------- #
# paper_get
# --------------------------------------------------------------------------- #
def test_paper_get_returns_metadata_provenance_and_chunks_count(
    mcp_client, session_factory
) -> None:
    paper_id = make_paper(session_factory, title="Readable", chunks=2)
    envelope = call(mcp_client, "paper_get", {"paper_id": paper_id})
    assert envelope["data"]["paper"]["paper_id"] == paper_id
    assert envelope["data"]["paper"]["title"] == "Readable"
    assert envelope["data"]["chunk_count"] == 2
    assert envelope["data"]["degradations"] == []
    assert "provenance" in envelope["data"]
    assert envelope["warnings"] == []


def test_paper_get_reports_a_soft_deleted_paper_as_missing(
    mcp_client, session_factory
) -> None:
    from datetime import datetime, timezone

    session = session_factory()
    try:
        paper = Paper(title="gone", fingerprint=f"fp-{uuid.uuid4().hex}")
        paper.deleted_at = datetime.now(timezone.utc)
        session.add(paper)
        session.commit()
        paper_id = paper.id
    finally:
        session.close()
    error = call_error(mcp_client, "paper_get", {"paper_id": paper_id})
    assert error["error"]["code"] == "NOT_FOUND"


def test_paper_get_warns_about_an_unparsed_paper(mcp_client, session_factory) -> None:
    paper_id = make_paper(session_factory, chunks=0)
    envelope = call(mcp_client, "paper_get", {"paper_id": paper_id})
    assert envelope["data"]["chunk_count"] == 0
    assert any("no chunks" in item for item in envelope["warnings"])


# --------------------------------------------------------------------------- #
# paper_get_chunks
# --------------------------------------------------------------------------- #
def test_chunks_are_paged_in_reading_order_with_citations(
    mcp_client, session_factory
) -> None:
    paper_id = make_paper(session_factory, chunks=5, text="a" * 20)
    page = call(mcp_client, "paper_get_chunks", {"paper_id": paper_id, "limit": 2})
    assert [chunk["chunk_index"] for chunk in page["data"]["chunks"]] == [0, 1]
    assert page["data"]["total"] == 5
    assert page["data"]["next_offset"] == 2
    assert page["data"]["truncated"] is False
    assert len(page["citations"]) == 2
    assert page["citations"][0]["page"] == 1

    second = call(
        mcp_client, "paper_get_chunks", {"paper_id": paper_id, "limit": 2, "offset": 2}
    )
    assert [chunk["chunk_index"] for chunk in second["data"]["chunks"]] == [2, 3]
    # No overlap between pages, and the tail reports that nothing is left.
    tail = call(
        mcp_client, "paper_get_chunks", {"paper_id": paper_id, "limit": 2, "offset": 4}
    )
    assert tail["data"]["next_offset"] is None
    assert tail["data"]["returned"] == 1


def test_chunk_budget_cuts_the_page_and_says_where_to_continue(
    mcp_client, session_factory
) -> None:
    paper_id = make_paper(session_factory, chunks=3, text="b" * 100)
    page = call(
        mcp_client, "paper_get_chunks", {"paper_id": paper_id, "limit": 3, "max_chars": 250}
    )
    chunks = page["data"]["chunks"]
    assert page["data"]["truncated"] is True
    # 100 + 100 + 50: the third chunk is cut rather than dropped, and the caller is
    # told exactly which offset to ask for next.
    assert [chunk["chars"] for chunk in chunks] == [100, 100, 50]
    assert chunks[-1]["truncated"] is True
    assert page["data"]["next_offset"] == 3
    assert any("truncated at 250" in item for item in page["warnings"])


def test_chunk_budget_above_the_ceiling_is_an_error_not_a_clamp(
    mcp_client, session_factory
) -> None:
    paper_id = make_paper(session_factory, chunks=1)
    error = call_error(
        mcp_client,
        "paper_get_chunks",
        {"paper_id": paper_id, "max_chars": settings.mcp_max_chars_ceiling + 1},
    )
    assert error["error"]["code"] == "INVALID_ARGUMENT"
    assert str(settings.mcp_max_chars_ceiling) in error["error"]["message"]


def test_chunk_limit_and_offset_are_validated(mcp_client, session_factory) -> None:
    paper_id = make_paper(session_factory, chunks=1)
    assert (
        call_error(mcp_client, "paper_get_chunks", {"paper_id": paper_id, "limit": 0})[
            "error"
        ]["code"]
        == "INVALID_ARGUMENT"
    )
    assert (
        call_error(mcp_client, "paper_get_chunks", {"paper_id": paper_id, "offset": -1})[
            "error"
        ]["code"]
        == "INVALID_ARGUMENT"
    )


# --------------------------------------------------------------------------- #
# paper_get_context
# --------------------------------------------------------------------------- #
def test_context_returns_the_neighbours_in_order_with_the_target_marked(
    mcp_client, session_factory
) -> None:
    paper_id = make_paper(session_factory, chunks=5, text="c" * 30)
    session = session_factory()
    try:
        middle = (
            session.query(PaperChunk)
            .filter_by(paper_id=paper_id, chunk_index=2)
            .one()
        )
        target_id = middle.id
    finally:
        session.close()

    envelope = call(mcp_client, "paper_get_context", {"chunk_id": target_id})
    chunks = envelope["data"]["chunks"]
    assert [chunk["chunk_index"] for chunk in chunks] == [1, 2, 3]
    assert [chunk["primary"] for chunk in chunks] == [False, True, False]
    assert envelope["data"]["target_chunk_id"] == target_id
    assert envelope["data"]["title"]
    assert len(envelope["citations"]) == 3
    assert envelope["warnings"] == []


def test_context_at_the_paper_start_says_so(mcp_client, session_factory) -> None:
    paper_id = make_paper(session_factory, chunks=3, text="d" * 30)
    session = session_factory()
    try:
        first = (
            session.query(PaperChunk).filter_by(paper_id=paper_id, chunk_index=0).one()
        )
        target_id = first.id
    finally:
        session.close()

    envelope = call(mcp_client, "paper_get_context", {"chunk_id": target_id, "before": 2})
    assert envelope["data"]["missing_before"] == 2
    assert [chunk["chunk_index"] for chunk in envelope["data"]["chunks"]] == [0, 1]
    assert any("the paper starts here" in item for item in envelope["warnings"])


def test_context_rejects_an_unknown_chunk(mcp_client) -> None:
    error = call_error(
        mcp_client,
        "paper_get_context",
        {"chunk_id": "11111111-2222-3333-4444-555555555555"},
    )
    assert error["error"]["code"] == "NOT_FOUND"
    assert "chunk_id" in (error["error"].get("hint") or "")


def test_context_with_a_tiny_budget_still_answers_about_the_target(
    mcp_client, session_factory
) -> None:
    paper_id = make_paper(session_factory, chunks=3, text="e" * 200)
    session = session_factory()
    try:
        middle = (
            session.query(PaperChunk).filter_by(paper_id=paper_id, chunk_index=1).one()
        )
        target_id = middle.id
    finally:
        session.close()

    envelope = call(
        mcp_client, "paper_get_context", {"chunk_id": target_id, "max_chars": 50}
    )
    chunks = envelope["data"]["chunks"]
    assert len(chunks) == 1
    assert chunks[0]["chunk_id"] == target_id
    assert chunks[0]["primary"] is True
    assert chunks[0]["chars"] == 50
    assert chunks[0]["truncated"] is True
    assert envelope["data"]["truncated"] is True


# --------------------------------------------------------------------------- #
# paper_get_file (signed link)
# --------------------------------------------------------------------------- #
def test_get_file_returns_a_signed_link_without_credentials(
    mcp_client, session_factory, monkeypatch
) -> None:
    paper_id = make_paper(session_factory, chunks=2)
    record = _record(paper_id=paper_id)
    monkeypatch.setattr(
        tools_read.paper_service, "original_file", lambda paper: record
    )
    monkeypatch.setattr(settings, "mcp_public_base_url", "http://paperbox.lan:8077")

    envelope = call(mcp_client, "paper_get_file", {"paper_id": paper_id})
    url = envelope["data"]["download_url"]
    # The host comes from the request the agent actually made (testserver here),
    # never from a hard-coded public URL: the same server answers on several
    # addresses on a LAN.
    assert url.startswith(f"http://testserver/api/downloads/{paper_id}?")
    assert "sig=" in url and "exp=" in url
    # No long-lived credential ever appears in a URL an agent will paste around.
    assert settings.paper_api_key not in url
    assert envelope["data"]["bytes"] == 1234
    assert envelope["data"]["page_count"] == 2
    assert any("expires at" in item for item in envelope["warnings"])

    query = dict(part.split("=") for part in url.split("?", 1)[1].split("&"))
    assert download_signing.verify(paper_id, int(query["exp"]), query["sig"])
    # Flip one character for real: patching a hex digit with itself (an earlier
    # version of this test wrote "0" over a "0") would assert nothing.
    tampered = query["sig"][:-1] + ("f" if query["sig"][-1] != "f" else "0")
    assert download_signing.verify(paper_id, int(query["exp"]), tampered) is False


def test_download_host_falls_back_to_configuration_without_a_request(monkeypatch) -> None:
    """stdio shells have no HTTP request: the configured base URL is the fallback."""
    monkeypatch.setattr(settings, "mcp_public_base_url", "http://paperbox.lan:8077")
    assert tools_read._base_url(None) == "http://paperbox.lan:8077"

    monkeypatch.setattr(settings, "mcp_public_base_url", "")
    with pytest.raises(Exception) as failure:
        tools_read._base_url(None)
    assert "MCP_PUBLIC_BASE_URL" in str(failure.value)


def test_get_file_needs_a_stored_original(mcp_client, session_factory, monkeypatch) -> None:
    paper_id = make_paper(session_factory, chunks=0)
    monkeypatch.setattr(tools_read.paper_service, "original_file", lambda paper: None)
    error = call_error(mcp_client, "paper_get_file", {"paper_id": paper_id})
    assert error["error"]["code"] == "NOT_FOUND"


def _record(paper_id: str):
    """A ``PaperFile`` stand-in with just the fields the tool reads."""

    class Record:
        object_key = f"papers/{paper_id}/original.pdf"
        bucket = "paperbox"
        filename = "original.pdf"
        content_type = "application/pdf"
        size_bytes = 1234
        kind = "original"

    return Record()


# --------------------------------------------------------------------------- #
# the signed route itself
# --------------------------------------------------------------------------- #
@pytest.fixture()
def paper_client(session_factory):
    """A TestClient over the real app with the database and API key stubbed."""

    def _db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    paperbox_app.dependency_overrides[get_db] = _db
    paperbox_app.dependency_overrides[require_api_key] = lambda: "test-key"
    try:
        yield TestClient(paperbox_app)
    finally:
        paperbox_app.dependency_overrides.clear()


def test_signed_route_streams_the_file(paper_client, session_factory, monkeypatch) -> None:
    paper_id = make_paper(session_factory, chunks=1)
    session = session_factory()
    try:
        paper = session.get(Paper, paper_id)
        from app.db.models import PaperFile
        from app.services.paper_service import primary_priority  # noqa: F401

        session.add(
            PaperFile(
                paper_id=paper.id,
                kind="original",
                object_key=f"papers/{paper_id}/original.pdf",
                bucket="paperbox",
                filename="original.pdf",
                content_type="application/pdf",
                size_bytes=5,
                is_primary=True,
            )
        )
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(settings, "mcp_download_secret", "test-secret")

    class _Body(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    monkeypatch.setattr(
        "app.api.downloads.object_storage.open_stream",
        lambda key, bucket=None: _Body(b"%PDF-"),
    )

    url, _ = download_signing.build_url(paper_id, "http://testserver")
    response = paper_client.get(url.replace("http://testserver", ""))
    assert response.status_code == 200
    assert response.content == b"%PDF-"
    assert response.headers["content-disposition"].startswith("attachment")


def test_a_cjk_filename_does_not_break_the_download(paper_client, session_factory, monkeypatch) -> None:
    """Live-corpus bug: a non-ASCII filename used to answer 500.

    ``Content-Disposition: attachment; filename=<中文>.pdf`` is encoded as latin-1
    by Starlette, which raised ``UnicodeEncodeError`` -- so any paper whose stored
    filename was not ASCII could not be downloaded at all (found 2026-10-04).
    """
    from app.api import downloads as downloads_api

    header = downloads_api.disposition("低功耗\u00a0SRAM 泄漏.pdf")
    assert header.startswith('attachment; filename="')
    assert "filename*=UTF-8''" in header
    assert header.isascii()
    # An ASCII name stays simple, and quotes/spaces never break the header.
    assert downloads_api.disposition("original.pdf") == 'attachment; filename="original.pdf"'
    assert downloads_api.disposition("a b.pdf") == 'attachment; filename="a b.pdf"'


def test_stream_original_uses_the_rfc6266_header(paper_client, session_factory, monkeypatch) -> None:
    paper_id = make_paper(session_factory, chunks=0)
    from app.db.models import PaperFile

    session = session_factory()
    try:
        session.add(
            PaperFile(
                paper_id=paper_id,
                kind="original",
                object_key=f"papers/{paper_id}/original.pdf",
                bucket="paperbox",
                filename="低功耗 SRAM 泄漏.pdf",
                content_type="application/pdf",
                size_bytes=5,
                is_primary=True,
            )
        )
        session.commit()
    finally:
        session.close()

    class _Body(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    monkeypatch.setattr(
        "app.api.downloads.object_storage.open_stream",
        lambda key, bucket=None: _Body(b"%PDF-"),
    )
    monkeypatch.setattr(settings, "mcp_download_secret", "test-secret")
    url, _ = download_signing.build_url(paper_id, "http://testserver")
    response = paper_client.get(url.replace("http://testserver", ""))
    assert response.status_code == 200
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


def test_signed_route_refuses_a_tampered_signature(paper_client, session_factory) -> None:
    paper_id = make_paper(session_factory, chunks=1)
    response = paper_client.get(
        f"/api/downloads/{paper_id}", params={"exp": 9999999999, "sig": "0" * 32}
    )
    assert response.status_code == 403


def test_signed_route_refuses_an_expired_link(paper_client, session_factory) -> None:
    paper_id = make_paper(session_factory, chunks=1)
    expired = int(time.time()) - 5
    response = paper_client.get(
        f"/api/downloads/{paper_id}",
        params={"exp": expired, "sig": download_signing.sign(paper_id, expired)},
    )
    assert response.status_code == 403
    assert "expired" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_fit_chunks_never_silently_drops_the_last_chunk() -> None:
    rows = [
        _chunk_out("a", 0, "x" * 10),
        _chunk_out("b", 1, "y" * 10),
        _chunk_out("c", 2, "z" * 10),
    ]
    views, truncated, next_offset = tools_read._fit_chunks(rows, 25)
    assert [view.chars for view in views] == [10, 10, 5]
    assert truncated is True
    assert next_offset == 3
    assert views[-1].truncated is True

    complete, truncated, next_offset = tools_read._fit_chunks(rows, 30, start_offset=4)
    assert len(complete) == 3
    assert truncated is False
    assert next_offset is None


def _chunk_out(chunk_id: str, index: int, text: str):
    from app.schemas.paper import PaperChunkOut

    return PaperChunkOut(
        chunk_id=chunk_id,
        chunk_index=index,
        page_start=index + 1,
        page_end=index + 1,
        section=f"s{index}",
        text=text,
        char_count=len(text),
    )


def test_chunk_view_marks_the_budget_cut() -> None:
    view = ChunkView(text="abc", chunk_id="c", chunk_index=0, chars=3)
    assert view.truncated is False and view.primary is False
    # Sanity: the models stay JSON-able (structuredContent has to serialise).
    assert json.loads(view.model_dump_json())["chars"] == 3


def test_link_ttl_is_clamped_to_a_sane_window(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mcp_download_ttl_seconds", 999999)
    assert download_signing.ttl_seconds() == download_signing.MAX_TTL_SECONDS
    monkeypatch.setattr(settings, "mcp_download_ttl_seconds", 1)
    assert download_signing.ttl_seconds() == download_signing.MIN_TTL_SECONDS


def test_build_url_expiry_matches_the_ttl() -> None:
    url, expires_at = download_signing.build_url("p1", "http://host:8077", ttl=60)
    assert "http://host:8077/api/downloads/p1?" in url
    delta = expires_at - expires_at.now(expires_at.tzinfo)
    assert timedelta(seconds=55) < delta <= timedelta(seconds=60)
