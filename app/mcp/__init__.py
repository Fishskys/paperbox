"""MCP agent interface (contract: ``docs/architecture/11-mcp-agent-interface.md``).

The MCP endpoint is a thin shell over ``app/services/*``: it shares the app
process, settings, auth and logging with the REST API, and it never re-implements
retrieval, aggregation or metadata rules. See the contract doc section 7 for the
invariants a change here has to keep.
"""

from __future__ import annotations

from app.mcp.server import build_server, build_streamable_http_app

__all__ = ["build_server", "build_streamable_http_app"]
