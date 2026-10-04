"""Streamable HTTP MCP server, served by the paperbox app itself.

Contract: ``docs/architecture/11-mcp-agent-interface.md`` section 2. The parts that
bite if they are left to defaults:

* **Transport security is explicit.** ``streamable_http_app()`` arms DNS-rebinding
  protection with a *localhost* allowlist and answers every other ``Host`` with a
  bare-text ``421`` -- invisible from the client side, which only sees a generic
  transport error. So the allowlist is required, not optional; an empty one is a
  configuration error, never a silent fallback.
* **Stateless HTTP.** One transport per request, no session bookkeeping: the tool
  layer resolves the caller from the request itself, so several workers or a restart
  cannot strand a session.
* **The session manager belongs to the host app.** A mounted sub-application's own
  lifespan never runs, so :func:`app.main.lifespan` has to enter
  ``session_manager.run()``; without it the server starts, the route resolves, and
  the first request fails.
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from app import __version__
from app.core.config import settings
from app.core.logging import get_logger
from app.mcp import tools_read

logger = get_logger(__name__)

#: Instructions handed to the client at ``initialize`` (agent-facing prompt).
INSTRUCTIONS = (
    "paperbox is a paper knowledge service: search a corpus of PDFs in natural "
    "language, read the parts that matched (each hit carries page and section, so "
    "citations are exact), and -- when the server operator enabled it -- import new "
    "papers. Start with paper_search; use paper_get_context to read around a hit, "
    "paper_get_chunks to page through a whole paper, and paper_get_file only when "
    "the original PDF itself matters."
)


def build_server() -> MCPServer:
    """Create the MCP server and register the tools this build exposes."""
    server = MCPServer(
        name="paperbox",
        title="paperbox paper knowledge service",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    tools_read.register(server)
    return server


def transport_security() -> TransportSecuritySettings:
    """Explicit Host allowlist for the Streamable HTTP transport.

    Raises instead of falling back to the SDK default: that default only accepts
    ``127.0.0.1``/``localhost``, which looks like a broken deployment to every
    agent on the LAN (see the contract doc, section 2 and R10).
    """
    allowed = settings.mcp_allowed_host_list
    if not allowed:
        raise RuntimeError(
            "MCP_ALLOWED_HOSTS is empty: list every host agents use (the LAN IP and "
            "the hostname, each as 'host' and 'host:*'). The SDK default allows "
            "localhost only and answers anything else with 421."
        )
    return TransportSecuritySettings(allowed_hosts=allowed, allowed_origins=[])


def build_streamable_http_app(server: MCPServer | None = None) -> Starlette:
    """Build the ASGI app for ``/mcp`` (mount it under that path)."""
    server = server or build_server()
    security = transport_security()
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=security,
    )
    # Logged on purpose: "which hosts do we actually answer?" is the first question
    # when an agent gets a 421.
    logger.info(
        "mcp endpoint ready",
        extra={
            "extra_fields": {
                "path": "/mcp",
                "stateless_http": True,
                "json_response": True,
                "allowed_hosts": security.allowed_hosts,
                "write_enabled": settings.mcp_write_enabled,
                "toolset": settings.mcp_toolset,
            }
        },
    )
    return app
