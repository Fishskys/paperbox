"""Static bearer auth for ``/mcp``, and the agent identity it carries.

The contract (``docs/architecture/11-mcp-agent-interface.md`` section 4) is small
on purpose: one credential path -- ``Authorization: Bearer <key>`` -- resolved by
:mod:`app.services.api_key_service`, the same source the REST dependency uses
(MCP and HTTP share keys by construction). Missing credential is 401, a
credential that matches nothing is 403, and a key in the query string is **not**
supported at all (it would land in proxy logs and shell history).

``AUTH_ENABLED`` is the all-on/all-off master switch (plan §3 D1): off, this
middleware never checks a credential and serves everyone as the anonymous admin
identity; on, every call must present a key. ``MCP_ENABLED`` only decides
whether ``/mcp`` exists.

Why not the SDK's built-in ``AuthSettings`` / ``TokenVerifier``: that machinery
models an OAuth resource server -- ``issuer_url`` and ``resource_server_url`` are
required and it advertises RFC 9728 discovery endpoints, so a client would try a
dynamic OAuth flow that can never succeed against a static key. A constant-time
comparison in front of the mounted endpoint is the honest shape here; it is also
where the identity is known before any tool code runs.

The identity is published to the tool layer through a context variable, which the
request-scoped middleware sets and every ``log_call`` / ``Envelope.meta`` reads
(contract section 4.3: the audit line has to say *which* agent did it) -- plus
the key prefix, so a log line answers "which credential" without ever carrying
the key itself.
"""

from __future__ import annotations

from contextvars import ContextVar

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.core.logging import bind_key_prefix, get_logger
from app.db.session import SessionLocal
from app.services import api_key_service
from app.services.api_key_service import AuthIdentity

logger = get_logger(__name__)

#: Path prefix owned by the MCP endpoint (everything under it is guarded).
MCP_PATH = "/mcp"

#: Sent with a 401 so an MCP client knows a bearer token is what it is missing.
WWW_AUTHENTICATE = 'Bearer realm="paperbox"'

#: Agent name used when the shared key authenticated the caller.
SHARED_AGENT = api_key_service.SHARED_AGENT

#: Where the token came from (kept for the audit line and for tests).
SOURCE_KEYS = api_key_service.SOURCE_KEYS
SOURCE_SHARED = api_key_service.SOURCE_SHARED

#: Backwards-compatible name: the identity type lives with the key store now.
AgentIdentity = AuthIdentity


#: The identity of the request being served; ``None`` outside a request.
current_agent: ContextVar[AgentIdentity | None] = ContextVar("mcp_agent", default=None)


def agent_name() -> str:
    """The caller's agent name (``default`` when there is no request context)."""
    identity = current_agent.get()
    return identity.name if identity else SHARED_AGENT


def current_identity() -> AgentIdentity | None:
    """The full identity of the served request (role checks read this)."""
    return current_agent.get()


def parse_bearer(header: str | None) -> str | None:
    """Extract the token from an ``Authorization`` header (``None`` if malformed)."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def resolve_agent(token: str | None, session=None) -> AgentIdentity | None:
    """Map a bearer token to an agent, or ``None`` when nothing matches.

    Thin wrapper over :func:`api_key_service.authenticate` (environment keys
    first, then the ``api_keys`` table). ``session=None`` resolves environment
    credentials only -- enough for unit tests and for the shared-key clash
    probe; the middleware always passes a real session so database keys work.
    """
    if session is not None:
        return api_key_service.authenticate(session, token)
    return api_key_service.authenticate_env(token)


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

    ``AUTH_ENABLED=false`` never reaches here: the middleware assigns the
    anonymous identity directly (plan §3 D1 -- the switch is all-on/all-off).
    """
    token = parse_bearer(request.headers.get("authorization"))
    if token is None:
        return JSONResponse(
            {"error": "missing or malformed Authorization header"},
            status_code=401,
            headers={"WWW-Authenticate": WWW_AUTHENTICATE},
        )
    session = SessionLocal()
    try:
        identity = api_key_service.authenticate(session, token)
    finally:
        session.close()
    if identity is None:
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
        if settings.auth_enabled:
            refusal = authorize(request)
            if refusal is not None:
                await refusal(scope, receive, send)
                return
            identity = request.scope.get("state", {}).get("mcp_agent")
        else:
            identity = api_key_service.anonymous()
        reset = current_agent.set(identity)
        bind_key_prefix(identity.prefix)
        try:
            await self.app(scope, receive, send)
        finally:
            current_agent.reset(reset)


__all__ = [
    "AgentIdentity",
    "MCP_PATH",
    "McpAuthMiddleware",
    "SHARED_AGENT",
    "SOURCE_KEYS",
    "SOURCE_SHARED",
    "WWW_AUTHENTICATE",
    "agent_name",
    "authorize",
    "current_agent",
    "current_identity",
    "parse_bearer",
    "resolve_agent",
    "warn_about_shared_keys",
]
