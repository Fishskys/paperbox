"""Structured audit trail for MCP tool calls (no database table).

The contract (``docs/architecture/11-mcp-agent-interface.md`` section 4.3) only
requires one structured log line per call: which agent, which tool, what it was
asked to do, how big the blast radius was, and how it ended. Retrieval calls keep
landing in ``search_queries`` through the service layer; this module covers the MCP
level, where the agent identity lives.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Longest ``query`` fragment kept in the audit line (full text is in the search log).
QUERY_PREVIEW_CHARS = 200


def digest_args(arguments: Any) -> dict[str, Any]:
    """Shrink tool arguments to something safe and useful to log.

    Bodies of papers never reach the log (a query is truncated, the rest of the
    payload is summarised), and keys are not part of the arguments in the first
    place -- they travel in the ``Authorization`` header.
    """
    if not isinstance(arguments, dict):
        return {}
    digest: dict[str, Any] = {}
    for name, value in arguments.items():
        if name == "query" and isinstance(value, str):
            digest[name] = value[:QUERY_PREVIEW_CHARS]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            digest[name] = value
        elif isinstance(value, list):
            digest[name] = f"list[{len(value)}]"
        elif isinstance(value, dict):
            digest[name] = f"object({len(value)} keys)"
        else:
            digest[name] = type(value).__name__
    return digest


def log_call(
    *,
    tool: str,
    agent: str,
    arguments: Any,
    outcome: str,
    code: str | None = None,
    affected: dict[str, Any] | None = None,
    took_ms: int = 0,
) -> None:
    """Write one ``mcp_call`` line (the only place agent identity is recorded).

    ``key_prefix`` comes from the request's identity (``app.mcp.auth``), not from
    the arguments: it answers "which credential did this" without ever carrying
    key material (plan: 2026-10-05_145619-api-auth-keys-roles §7.6).
    """
    from app.mcp import auth

    identity = auth.current_identity()
    logger.info(
        "mcp call",
        extra={
            "extra_fields": {
                "event": "mcp_call",
                "agent": agent,
                "key_prefix": identity.prefix if identity else "-",
                "tool": tool,
                "args_digest": digest_args(arguments),
                "outcome": outcome,
                "code": code,
                "affected": affected or {},
                "took_ms": took_ms,
                "transport": "http",
            }
        },
    )
