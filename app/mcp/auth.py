"""Static bearer auth for ``/mcp``, and the agent identity it carries.

The contract (``docs/architecture/11-mcp-agent-interface.md`` section 4) is small
on purpose: one credential path -- ``Authorization: Bearer <key>`` -- resolved
against ``PAPER_API_KEYS`` (name -> key, for per-agent audit) with a fallback to
the shared ``PAPER_API_KEY`` (the caller is then called ``default``). Missing
credential is 401, a credential that matches nothing is 403, and a key in the query
string is **not** supported at all (it would land in proxy logs and shell history).

Why not the SDK's built-in ``AuthSettings`` / ``TokenVerifier``: that machinery
models an OAuth resource server -- ``issuer_url`` and ``resource_server_url`` are
required and it advertises RFC 9728 discovery endpoints, so a client would try a
dynamic OAuth flow that can never succeed against a static key. A constant-time
comparison in front of the mounted endpoint is the honest shape here; it is also
where the identity is known before any tool code runs.

The identity is published to the tool layer through a context variable, which the
request-scoped middleware sets and every ``log_call`` / ``Envelope.meta`` reads
(contract section 4.3: the audit line has to say *which* agent did it).
"""

from __future__ import annotations

import hmac
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Path prefix owned by the MCP endpoint (everything under it is guarded).
MCP_PATH = "/mcp"

#: Sent with a 401 so an MCP client knows a bearer token is what it is missing.
WWW_AUTHENTICATE = 'Bearer realm="paperbox"'

#: Agent name used when the shared key authenticated the caller.
SHARED_AGENT = "default"

#: Where the token came from (kept for the audit line and for tests).
SOURCE_KEYS = "PAPER_API_KEYS"
SOURCE_SHARED = "PAPER_API_KEY"


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """Who is calling, as far as this server can tell."""

    name: str
    source: str

    @property
    def is_shared(self) -> bool:
        return self.source == SOURCE_SHARED


#: The identity of the request being served; ``None`` outside a request.
current_agent: ContextVar[AgentIdentity | None] = ContextVar("mcp_agent", default=None)


def agent_name() -> str:
    """The caller's agent name (``default`` when there is no request context)."""
    identity = current_agent.get()
    return identity.name if identity else SHARED_AGENT


def parse_bearer(header: str | None) -> str | None:
    """Extract the token from an ``Authorization`` header (``None`` if malformed)."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def resolve_agent(token: str | None) -> AgentIdentity | None:
    """Map a bearer token to an agent, or ``None`` when nothing matches.

    Both lookups compare with :func:`hmac.compare_digest`, so a wrong key cannot be
    narrowed down by timing. ``PAPER_API_KEYS`` wins over the shared key, and a key
    that appears in both is flagged once at startup (see :func:`warn_about_shared_keys`).
    """
    if not token:
        return None
    for name, key in settings.agent_keys.items():
        if hmac.compare_digest(key, token):
            return AgentIdentity(name=name, source=SOURCE_KEYS)
    shared = settings.paper_api_key or ""
    if shared and hmac.compare_digest(shared, token):
        return AgentIdentity(name=SHARED_AGENT, source=SOURCE_SHARED)
    return None


def warn_about_shared_keys() -> list[str]:
    """Log (and return) agents whose key is also the shared one.

    Contract section 4.2: when the same key appears in both places the named agent
    wins -- which means the shared key silently stops being "the shared key". That
    is worth one WARNING at startup rather than a confusing audit trail.
    """
    shared = settings.paper_api_key or ""
    clashes = [
        name for name, key in settings.agent_keys.items() if shared and key == shared
    ]
    for name in clashes:
        logger.warning(
            "agent %r uses the same key as PAPER_API_KEY: the named agent wins, the "
            "shared identity is unreachable with that key",
            name,
        )
    return clashes


def authorize(request: Request) -> JSONResponse | None:
    """Return a 401/403 response when the request may not talk to ``/mcp``.

    Ordering note: this runs **before** the transport sees the request, so an
    unauthenticated caller gets 401 even when its ``Host`` is not on the allowlist --
    a client that has not authenticated learns nothing about which host names the
    server accepts. The 421 check still applies to authenticated requests (contract
    section 2), and both cases are covered by tests.
    """
    identity = resolve_agent(parse_bearer(request.headers.get("authorization")))
    if identity is None:
        token = parse_bearer(request.headers.get("authorization"))
        if token is None:
            return JSONResponse(
                {"error": "missing or malformed Authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": WWW_AUTHENTICATE},
            )
        logger.warning("mcp auth refused: unknown bearer token")
        return JSONResponse({"error": "invalid credentials"}, status_code=403)
    request.scope.setdefault("state", {})["mcp_agent"] = identity
    return None


class McpAuthMiddleware:
    """ASGI middleware: authenticate ``/mcp`` and publish the agent identity.

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` because the MCP endpoint
    streams responses (SSE for some methods) and a buffering middleware would break
    that; this one only inspects the request and then gets out of the way.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(MCP_PATH):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        refusal = authorize(request)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        identity = request.scope.get("state", {}).get("mcp_agent")
        reset = current_agent.set(identity) if identity is not None else None
        try:
            await self.app(scope, receive, send)
        finally:
            if reset is not None:
                current_agent.reset(reset)


__all__ = [
    "MCP_PATH",
    "SHARED_AGENT",
    "SOURCE_KEYS",
    "SOURCE_SHARED",
    "WWW_AUTHENTICATE",
    "AgentIdentity",
    "McpAuthMiddleware",
    "agent_name",
    "authorize",
    "current_agent",
    "parse_bearer",
    "resolve_agent",
    "warn_about_shared_keys",
]
