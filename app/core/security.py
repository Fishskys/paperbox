"""API key authentication for the paperbox REST API's **write** surface.

The API is called by the Hermes main agent over HTTP using a bearer token:

    Authorization: Bearer pb_<prefix>_<random>

Resolution lives in :mod:`app.services.api_key_service` (the same source the MCP
middleware uses — one credential store, two surfaces). This module owns the HTTP
wiring: extracting the credential from the request and enforcing role tiers.

**Who needs a credential (decision of 2026-10-12).** Reads are public: every
endpoint that does not change stored data — retrieval, listing, detail, chunks,
degradations, jobs, consistency, search logs, the signed download — carries no
dependency, so a missing **or invalid** credential is simply not consulted and the
answer is 200. Only the endpoints that write (ingestion, retry, reindex, metadata
mutations) require ``write``, and deletion requires ``admin``. The route table in
``docs/architecture/07-api.md`` marks which is which.

``AUTH_ENABLED=false`` short-circuits the remaining checks: every caller gets the
anonymous admin identity and the log prefix ``anonymous``. The switch is
all-or-nothing for the gates that still exist (plan section 3 D2), and it is *not*
what makes reads public — they are public in both states.
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
        return None, False
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        return api_key_service.authenticate(session, token), True
    finally:
        session.close()


def require_role(minimum: str):
    """Build a FastAPI dependency enforcing ``minimum`` (tiers are monotonic).

    Only the endpoints that *declare* the dependency are gated — the read
    endpoints declare none (module docstring). For a gated endpoint: 401 for a
    missing credential, 403 for an unmatched one, 403 with a distinct detail for a
    matched key whose tier is too low (plan section 3 D9) — a client can tell
    "rotate your key" from "ask for a stronger key".
    """
    minimum_name = minimum

    def dependency(request: Request) -> AuthIdentity:
        if not settings.auth_enabled:
            return api_key_service.anonymous()
        identity, token_present = _resolve_identity(request)
        if identity is None:
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


#: Ingestion, retry, reindex, metadata mutations.
require_write = require_role(ROLE_WRITE)
#: Deletion.
require_admin = require_role(ROLE_ADMIN)

__all__ = [
    "API_KEY_HEADER",
    "extract_api_key",
    "require_admin",
    "require_role",
    "require_write",
    "ROLE_ADMIN",
    "ROLE_READ",
    "ROLE_WRITE",
]
