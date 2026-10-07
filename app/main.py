"""FastAPI application for paperbox.

Wiring only: the app object, the request-id middleware and the routers that
already exist. Endpoints are added phase by phase (see ``docs/architecture/MVP-SPEC.md``).

Run locally with::

    .venv\\Scripts\\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8077
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import __version__
from app.api import ingestion as ingestion_api
from app.api import jobs as jobs_api
from app.api import downloads as downloads_api
from app.api import health as health_api
from app.api import consistency as consistency_api
from app.api import metadata as metadata_api
from app.api import papers as papers_api
from app.api import search as search_api
from app.api import search_logs as search_logs_api
from app.core.config import settings
from app.core.logging import (
    REQUEST_ID_HEADER,
    bind_key_prefix,
    bind_request_id,
    configure_logging,
    get_logger,
)
from app.db.session import SessionLocal
from app.services import api_key_service
from app.services.api_key_service import AuthIdentity
from app.workers import housekeeping
from mcp.server import MCPServer

from app.mcp.auth import McpAuthMiddleware, warn_about_shared_keys
from app.mcp.server import McpMountPathMiddleware
from app.mcp.server import build_server, build_streamable_http_app
from app.core.security import extract_api_key
from app.workers import queue as job_queue

logger = get_logger(__name__)

#: Hosts where an unauthenticated API is still "local" enough not to warn about.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging(settings.log_level)
    logger.info("paperbox %s starting (env=%s)", __version__, settings.app_env)
    # Auth bootstrap runs before the queue starts: with AUTH_ENABLED=true a
    # failure must stop the process while no worker thread exists yet.
    api_key_service.startup_bootstrap()
    if settings.auth_enabled:
        session = SessionLocal()
        try:
            usable_keys = api_key_service.live_key_count(session)
        finally:
            session.close()
        if usable_keys == 0:
            raise RuntimeError(
                "AUTH_ENABLED=true but no API key exists: the environment "
                "bootstrap found none and the api_keys table is empty. Create "
                "one with scripts/manage_keys.py create"
            )
    else:
        if settings.paper_api_host not in _LOOPBACK_HOSTS:
            logger.warning(
                "auth is disabled and the API listens on %s — every /api and /mcp "
                "request is served as an anonymous admin",
                settings.paper_api_host,
            )
        if not (settings.mcp_download_secret or settings.paper_api_key):
            logger.warning(
                "no download-signing secret (set MCP_DOWNLOAD_SECRET or "
                "PAPER_API_KEY): signed download links will fail at creation"
            )
    # The ingestion queue owns every pipeline run: start the workers, then
    # reconcile whatever a previous process left behind (re-queue jobs that never
    # started, fail the ones that were mid-pipeline with INTERRUPTED).
    job_queue.start()
    job_queue.recover()
    # Housekeeping runs once right after recovery (it cleans the debris of the
    # process that died) and then on its own interval.
    housekeeping.start()
    if mcp_server is not None:
        # The MCP session manager has to be entered by the *host* app: a mounted
        # sub-application's own lifespan never runs, and without this line the
        # endpoint resolves but the first request fails (contract doc, section 2).
        async with mcp_server.session_manager.run():
            yield
    else:
        yield
    await housekeeping.stop()
    await job_queue.stop()
    logger.info("paperbox stopping")


app = FastAPI(
    title="paperbox",
    version=__version__,
    description=(
        "REST API of the paperbox paper knowledge service. Called by the "
        "Hermes main agent; bearer token auth on every /api route."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Propagate ``X-Request-ID`` into logs and echo it back to the caller."""
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
    bind_request_id(request_id)
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


# MCP auth runs outermost: an unauthenticated caller is refused before anything else
# (and before the transport reveals which Host names this server accepts).
app.add_middleware(McpAuthMiddleware)
# Serving /mcp without a trailing slash is in-process, not a 307 (see the class).
app.add_middleware(McpMountPathMiddleware)

#: Request-body ceilings (review 2026-10-05, P1-16). Code constants on purpose:
#: they guard memory, they are not deployment knobs. ``MAX_TOTAL_BODY_BYTES``
#: must stay above ``INGEST_MAX_REQUEST_MB`` (200 MB) — the business layer keeps
#: its stricter multipart rule and answers 413 first; this ceiling only exists
#: so no request can promise the process unbounded buffering (single worker:
#: the whole service, queue included, dies with the memory).
MAX_JSON_BODY_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BODY_BYTES = 256 * 1024 * 1024


class BodyLimitMiddleware:
    """Cap request bodies before anything reads them (review 2026-10-05, P1-16).

    Two body shapes, two mechanisms:

    * **Declared length** — a ``Content-Length`` over the ceiling is refused up
      front and no body is read at all (JSON 8 MB, everything else 256 MB).
    * **JSON** — the body is buffered *up to the ceiling* and replayed to the
      app, so neither a chunked body nor one that lies about its declared length
      can make ``request.json()`` buffer unbounded bytes. Over the ceiling: 413,
      and no route ever runs. The buffer is bounded by the ceiling by
      construction, which is the whole point.

    Multipart without a declared length still streams through: the upload path
    counts bytes per file and per request while streaming (that is where its own
    413/422 rules live), and buffering up to 256 MB here would be exactly the
    memory spike this class exists to prevent.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        is_json = content_type == "application/json"
        limit = MAX_JSON_BODY_BYTES if is_json else MAX_TOTAL_BODY_BYTES

        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await _answer_too_large(scope, receive, send)
            return

        if not is_json or scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body += message.get("body", b"")
            if len(body) > MAX_JSON_BODY_BYTES:
                # The ceiling is enforced on bytes that actually arrived, so a
                # missing or understated Content-Length cannot get past it.
                await _answer_too_large(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _answer_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    """Answer 413 without giving the body to any route."""
    from starlette.responses import JSONResponse

    response = JSONResponse({"detail": "request body too large"}, status_code=413)
    await response(scope, receive, send)


app.add_middleware(BodyLimitMiddleware)


class AuthContextMiddleware:
    """Resolve the caller's key once per ``/api`` request; publish it twice.

    Pure ASGI and *silent* — it never rejects (the role dependencies do that).
    It exists because (a) the log prefix must be bound in async context: a
    threadpool dependency cannot propagate a contextvar back to the request's
    logging context, and (b) the database probe must happen exactly once, not
    once per dependency. ``/mcp`` has its own middleware with the same rules
    (:mod:`app.mcp.auth`); ``AUTH_ENABLED=false`` resolves everyone to the
    anonymous admin identity without touching the database.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/api"):
            await self.app(scope, receive, send)
            return
        token = extract_api_key(Request(scope))
        identity: AuthIdentity | None = None
        if not settings.auth_enabled:
            identity = api_key_service.anonymous()
        elif token:
            # The key lookup is a blocking DB probe on the event loop; push it
            # to a worker thread (review 2026-10-05, P2-10 family).
            session = SessionLocal()
            try:
                identity = await asyncio.to_thread(
                    api_key_service.authenticate, session, token
                )
            finally:
                session.close()
        state = scope.setdefault("state", {})
        state["auth_identity"] = identity
        state["auth_token_present"] = bool(token)
        if identity is not None:
            bind_key_prefix(identity.prefix)
        await self.app(scope, receive, send)


# Outermost: every /api request gets its identity resolved (and its log prefix
# bound) before any dependency or handler runs.
app.add_middleware(AuthContextMiddleware)

app.include_router(health_api.router)
app.include_router(consistency_api.router)
app.include_router(ingestion_api.router)
app.include_router(jobs_api.router)
app.include_router(papers_api.router)
app.include_router(metadata_api.router)
app.include_router(search_api.router)
app.include_router(search_logs_api.router)
# Signed downloads carry their credential in the URL (no API key dependency).
app.include_router(downloads_api.router)

# --- MCP agent interface (contract: docs/architecture/11-mcp-agent-interface.md) ---
# Built only when MCP_ENABLED=true; an empty MCP_ALLOWED_HOSTS is a startup error
# (settings validator), never a silent fallback to the SDK's localhost-only default.
mcp_server: MCPServer | None = None
if settings.mcp_enabled:
    warn_about_shared_keys()
    mcp_server = build_server()
    app.mount("/mcp", build_streamable_http_app(mcp_server))


@app.get("/", include_in_schema=False)
async def root() -> dict:
    """Tiny landing payload so a bare GET / is not a 404."""
    return {"name": "paperbox", "version": __version__, "docs": "/docs"}


__all__ = ["app"]
