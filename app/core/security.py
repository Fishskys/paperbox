"""API key authentication for the paperbox REST API.

The API is called by the Hermes main agent over HTTP using a bearer token:

    Authorization: Bearer <PAPER_API_KEY>

FastAPI dependency wiring lives in the API layer (later phase); this module only
owns the credential check so it can be unit-tested in isolation.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from app.core.config import settings

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


def api_key_matches(candidate: str | None, expected: str | None = None) -> bool:
    """Constant-time comparison against the configured key."""
    expected_key = expected if expected is not None else settings.paper_api_key
    if not candidate or not expected_key:
        return False
    return hmac.compare_digest(candidate, expected_key)


def verify_api_key(request: Request) -> str:
    """FastAPI dependency: validate the bearer token and return it."""
    candidate = extract_api_key(request)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not api_key_matches(candidate):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return candidate


def require_api_key(request: Request) -> str:
    """Alias kept for readability at call sites."""
    return verify_api_key(request)
