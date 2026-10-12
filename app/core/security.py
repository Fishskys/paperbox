"""API key authentication for the paperbox REST API's **write** surface.

The API is called by the Hermes main agent over HTTP using a bearer token:

    Authorization: Bearer pb_<prefix>_<random>

Resolution lives in :mod:`app.services.api_key_service` (the same source the MCP
middleware uses — one credential store, two surfaces). This module owns the HTTP
wiring: extracting the credential from the request and enforcing role tiers.

**Who needs a credential (plan D2/D8).** There are four levels: ``public`` and the
three key tiers ``read < write < admin``. Reads carry :data:`require_read` and let a
caller without a credential through only while ``AUTH_READ_ACCESS=public`` (the
default): there the credential is not consulted at all, so a missing **or invalid**
one still gets 200 (the owner's B2 ruling of 2026-10-12) and the caller is served as
the anonymous *read* identity. With ``AUTH_READ_ACCESS=key`` the read door answers
like the write door: 401 without a credential, 403 for one that matches nothing.

Every endpoint that changes state needs ``write`` — ingestion, retry, single-paper
reindex, metadata mutations and **deletion**; only the bulk/whole-library reindex
needs ``admin``. The write and admin tiers do **not** depend on the read policy:
only the read column of the matrix moves when that knob moves, so a gate compares
``is_anonymous`` before the tier (D4).

``AUTH_ENABLED=false`` short-circuits everything: every caller gets the anonymous
admin identity and the log prefix ``anonymous``, and the read-door value has nothing
to act on (plan D7: no warning for that combination either).
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.services import api_key_service
from app.services.api_key_service import AuthIdentity, ROLE_ADMIN, ROLE_READ, ROLE_WRITE

API_KEY_HEADER = "X-API-Key"
BEARER_PREFIX = "bearer "


def extract_api_key(request: Request) -> str | None:
    """Pull the API key from the Authorization header, falling back to X-API-Key."""
    authorization = request.headers.get("Authorization")
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer" and credentials.strip():
            return credentials.strip()
        return None

    explicit = request.headers.get(API_KEY_HEADER)
    if explicit and explicit.strip():
        return explicit.strip()
    return None


def _resolve_identity(request: Request) -> tuple[AuthIdentity | None, bool]:
    """``(identity | None, token_present)`` for the current request.

    The normal path reads what :class:`app.main.AuthContextMiddleware` already
    resolved (one DB probe per request, log binding done in async context).
    The inline fallback covers callers that mounted routers without the
    middleware (small test apps): resolution runs here instead.
    """
    state = request.scope.get("state") or {}
    if "auth_identity" in state:
        return state["auth_identity"], bool(state.get("auth_token_present"))
    token = extract_api_key(request)
    if not settings.auth_enabled:
        return api_key_service.anonymous(), bool(token)
    if not token:
        return _identity_without_token(), False
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        return api_key_service.authenticate(session, token), True
    finally:
        session.close()


def _identity_without_token() -> AuthIdentity | None:
    """Who a credential-less caller is: the anonymous reader, or nobody.

    With the read door open the caller is a legitimate one and owns a name, which
    is what makes the log say ``anonymous`` (and what a write gate then answers 401
    to). With the door closed there is no identity to hand out.
    """
    return api_key_service.anonymous_reader() if settings.reads_are_public else None


def require_role(minimum: str):
    """Build a FastAPI dependency enforcing ``minimum`` (tiers are monotonic).

    Only the endpoints that *declare* the dependency are gated. Once gated
    (``AUTH_ENABLED=true``): 401 for a missing credential, 403 for one that matches
    nothing, and 403 with a distinct detail for a matched key whose tier is too low
    (plan 2026-10-05 D9) — a client can tell "rotate your key" from "ask for a
    stronger key".

    The read tier is the one gate an *open door* satisfies without a credential
    (plan D4 judgement 1-2): the anonymous reader passes it, and a credential that
    matched nothing is not consulted there at all. On the write and admin tiers the
    anonymous identity is answered 401 rather than 403 — it is the caller's move to
    present a key, and the status code stays identical whichever way the read
    policy points (plan D2 invariant).
    """
    minimum_name = minimum

    def dependency(request: Request) -> AuthIdentity:
        if not settings.auth_enabled:
            return api_key_service.anonymous()
        identity, token_present = _resolve_identity(request)
        if identity is None:
            if minimum_name == ROLE_READ and settings.reads_are_public:
                return api_key_service.anonymous_reader()
            if not token_present:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Missing API key",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if identity.is_anonymous:
            if api_key_service.role_at_least(identity.role, minimum_name):
                return identity
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not api_key_service.role_at_least(identity.role, minimum_name):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"insufficient role: this key is {identity.role!r}, "
                    f"this endpoint needs {minimum_name!r} or higher"
                ),
                headers={"WWW-Authenticate": "Bearer"},
            )
        return identity

    return dependency


#: Every read endpoint — the one gate an open read door satisfies anonymously.
require_read = require_role(ROLE_READ)
#: Every endpoint that changes state: ingestion, retry, single-paper reindex,
#: metadata mutations and deletion (plan D8).
require_write = require_role(ROLE_WRITE)
#: Bulk / whole-library reindex only (plan D8).
require_admin = require_role(ROLE_ADMIN)

__all__ = [
    "API_KEY_HEADER",
    "extract_api_key",
    "require_admin",
    "require_read",
    "require_role",
    "require_write",
    "ROLE_ADMIN",
    "ROLE_READ",
    "ROLE_WRITE",
]
