"""Static bearer auth for ``/mcp``, and the agent identity it carries.

The contract (``docs/architecture/11-mcp-agent-interface.md`` section 4) resolves
one credential path -- ``Authorization: Bearer pb_<prefix>_<secret>`` -- through
:mod:`app.services.api_key_service`, the same source the REST dependency uses (MCP
and HTTP share keys by construction). A key in the query string is **not**
supported at all (it would land in proxy logs and shell history).

**Anonymous is a caller (decision of 2026-10-12).** The transport no longer
refuses requests: it resolves whatever credential is present (an unknown one is
logged and treated as absent) and publishes the identity, possibly ``None``. What
an anonymous caller may *do* is decided per tool, where the tool name is finally
known: the six read tools answer without a credential, and the four write tools
require a key whose tier reaches ``write``/``admin`` (``tools_write.require_role``,
which fails on ``None``). This mirrors REST, where reads carry no gate and writes
do. The transport cannot make that distinction -- it never sees the tool name --
so a write tool called anonymously fails as ``ToolFailure(FORBIDDEN)`` rather than
as an HTTP 401/403.

**The read door (plan D5).** ``AUTH_READ_ACCESS`` decides who the six reading tools
answer: ``public`` (default) resolves a caller without a credential to the anonymous
*read* identity, ``key`` leaves it unresolved and ``tools_read.require_read`` then
fails the call with ``FORBIDDEN``. ``tools/list`` is built once at startup, so the
failure -- not a per-request unregistration -- is what tells the caller the door
closed. The write tools never depended on this value: they need ``write`` or higher
either way (deletion included, plan D8).

``AUTH_ENABLED`` remains the master switch (plan section 3 D1): off, this
middleware resolves everyone to the anonymous **admin** identity (so the write
tools' tier check passes and only the ``MCP_ALLOW_*`` switches remain); on, a
credential is required for writes and optional for reads. ``MCP_ENABLED`` only
decides whether ``/mcp`` exists.

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
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings
from app.core.logging import bind_key_prefix, get_logger
from app.db.session import SessionLocal
from app.services import api_key_service
from app.services.api_key_service import AuthIdentity

logger = get_logger(__name__)

#: Path prefix owned by the MCP endpoint (its identity gets resolved per request).
MCP_PATH = "/mcp"

#: Agent name used when the shared key authenticated the caller.
SHARED_AGENT = api_key_service.SHARED_AGENT

#: Agent name for a request that presented no usable credential (open reads).
ANONYMOUS_NAME = api_key_service.ANONYMOUS_NAME

#: Where the token came from (kept for the audit line and for tests).
SOURCE_KEYS = api_key_service.SOURCE_KEYS
SOURCE_SHARED = api_key_service.SOURCE_SHARED

#: Backwards-compatible name: the identity type lives with the key store now.
AgentIdentity = AuthIdentity


#: The identity of the request being served; ``None`` outside a request.
current_agent: ContextVar[AgentIdentity | None] = ContextVar("mcp_agent", default=None)


def agent_name() -> str:
    """The caller's agent name (``anonymous`` when no identity was resolved).

    With the read tools open, "no identity" is a normal state, and the audit line
    has to say so instead of implying a named agent called.
    """
    identity = current_agent.get()
    return identity.name if identity else ANONYMOUS_NAME


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


def resolve_identity(request: Request) -> AgentIdentity | None:
    """Resolve the request's credential, or ``None`` when there is none to trust.

    Never refuses: a missing header and an unknown token both resolve to ``None``,
    which is a legitimate caller here -- reads are open (decision of 2026-10-12).
    An unknown token is worth a WARNING, because it usually means a rotated or
    mistyped key that will start failing on the write tools.

    The resolved identity lands in ``request.scope["state"]["mcp_agent"]`` so the
    middleware can publish it; ``None`` leaves that key unset, which callers read
    as "anonymous" (``auth.current_identity()``).
    """
    token = parse_bearer(request.headers.get("authorization"))
    if token is None:
        return None
    session = SessionLocal()
    try:
        identity = api_key_service.authenticate(session, token)
    finally:
        session.close()
    if identity is None:
        logger.warning(
            "mcp auth: unknown bearer token, treating the call as anonymous"
        )
        return None
    request.scope.setdefault("state", {})["mcp_agent"] = identity
    return identity


class McpAuthMiddleware:
    """ASGI middleware: resolve the ``/mcp`` caller's identity and publish it.

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` because the MCP endpoint
    streams responses (SSE for some methods) and a buffering middleware would break
    that; this one only inspects the request and then gets out of the way.

    It does **not** reject anything: the tools decide (``tools_write.require_role``
    for writes, nothing for reads). The transport never sees the tool name, so it is
    the wrong layer for that decision -- a refusal here would have to gate the read
    tools too.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(MCP_PATH):
            await self.app(scope, receive, send)
            return
        if settings.auth_enabled:
            identity = resolve_identity(Request(scope, receive=receive))
            if identity is None and settings.reads_are_public:
                # Open read door (plan D2/D5): a caller without a trusted credential
                # is a legitimate reader and owns a name, so the audit line says
                # "anonymous" instead of nothing. The write tools' tier check still
                # refuses it.
                identity = api_key_service.anonymous_reader()
        else:
            identity = api_key_service.anonymous()
        reset = current_agent.set(identity)
        if identity is not None:
            bind_key_prefix(identity.prefix)
        try:
            await self.app(scope, receive, send)
        finally:
            current_agent.reset(reset)


__all__ = [
    "AgentIdentity",
    "ANONYMOUS_NAME",
    "MCP_PATH",
    "McpAuthMiddleware",
    "SHARED_AGENT",
    "SOURCE_KEYS",
    "SOURCE_SHARED",
    "agent_name",
    "current_agent",
    "current_identity",
    "parse_bearer",
    "resolve_agent",
    "resolve_identity",
    "warn_about_shared_keys",
]
