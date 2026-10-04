"""FastAPI application for paperbox.

Wiring only: the app object, the request-id middleware and the routers that
already exist. Endpoints are added phase by phase (see ``docs/architecture/MVP-SPEC.md``).

Run locally with::

    .venv\\Scripts\\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8077
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

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
    bind_request_id,
    configure_logging,
    get_logger,
)
from app.workers import housekeeping
from mcp.server import MCPServer

from app.mcp.server import build_server, build_streamable_http_app
from app.workers import queue as job_queue

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging(settings.log_level)
    logger.info("paperbox %s starting (env=%s)", __version__, settings.app_env)
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
    mcp_server = build_server()
    app.mount("/mcp", build_streamable_http_app(mcp_server))


@app.get("/", include_in_schema=False)
async def root() -> dict:
    """Tiny landing payload so a bare GET / is not a 404."""
    return {"name": "paperbox", "version": __version__, "docs": "/docs"}


__all__ = ["app"]
