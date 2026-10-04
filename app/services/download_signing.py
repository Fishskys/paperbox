"""Short-lived signed links for the stored original PDF.

``paper_get_file`` must not hand the agent a credential: a bearer key in a URL
ends up in proxy logs, shell history and whatever the agent writes down. So the
download route trusts an **HMAC over (paper_id, expiry)** instead, with a narrow
window, and the URL carries nothing reusable after it expires.

The secret is ``MCP_DOWNLOAD_SECRET`` when set, otherwise the REST API key (which
is already required for the service to be useful). Rotating either invalidates
outstanding links -- that is the intended behaviour, they are meant to be short.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from app.core.config import settings

#: Path of the signed download route (no API key dependency -- the signature is
#: the credential).
DOWNLOAD_PATH = "/api/downloads/{paper_id}"

#: Signature length: 128 bits is far beyond brute force for a 5-minute window.
SIGNATURE_CHARS = 32

#: Shorter than this is a bug (a link that dies before the agent can use it);
#: longer than the ceiling defeats the point of signing.
MIN_TTL_SECONDS = 10
MAX_TTL_SECONDS = 3600


def secret() -> str:
    """The signing key, or raise when the deployment has neither value set."""
    value = (settings.mcp_download_secret or settings.paper_api_key or "").strip()
    if not value:
        raise RuntimeError(
            "no download signing secret: set MCP_DOWNLOAD_SECRET (or PAPER_API_KEY)"
        )
    return value


def _payload(paper_id: str, expires_at: int) -> bytes:
    return f"{paper_id}:{expires_at}".encode()


def sign(paper_id: str, expires_at: int, *, key: str | None = None) -> str:
    """HMAC-SHA256 of ``paper_id:expires_at``, truncated to 32 hex chars."""
    digest = hmac.new(
        (key or secret()).encode(), _payload(paper_id, expires_at), hashlib.sha256
    )
    return digest.hexdigest()[:SIGNATURE_CHARS]


def verify(paper_id: str, expires_at: int, signature: str, *, key: str | None = None) -> bool:
    """Constant-time check of a presented signature (never raises on garbage)."""
    if not signature:
        return False
    try:
        expires = int(expires_at)
    except (TypeError, ValueError):
        return False
    expected = sign(paper_id, expires, key=key)
    return hmac.compare_digest(expected, str(signature))


def ttl_seconds() -> int:
    """Effective link lifetime, clamped to a sane window."""
    configured = int(getattr(settings, "mcp_download_ttl_seconds", 300) or 300)
    return max(MIN_TTL_SECONDS, min(configured, MAX_TTL_SECONDS))


def build_url(paper_id: str, base_url: str, *, ttl: int | None = None) -> tuple[str, datetime]:
    """Return ``(url, expires_at)`` for ``base_url`` (scheme+host, no trailing /).

    ``expires_at`` is returned in UTC so the caller can publish it as-is; the
    query string is built with :func:`urlencode` so nothing is interpolated raw.
    """
    lifetime = ttl if ttl is not None else ttl_seconds()
    lifetime = max(MIN_TTL_SECONDS, min(int(lifetime), MAX_TTL_SECONDS))
    expiry = datetime.now(timezone.utc) + timedelta(seconds=lifetime)
    epoch = int(expiry.timestamp())
    query = urlencode({"exp": epoch, "sig": sign(paper_id, epoch)})
    url = f"{base_url.rstrip('/')}{DOWNLOAD_PATH.format(paper_id=paper_id)}?{query}"
    return url, expiry


__all__ = [
    "DOWNLOAD_PATH",
    "MAX_TTL_SECONDS",
    "MIN_TTL_SECONDS",
    "SIGNATURE_CHARS",
    "build_url",
    "secret",
    "sign",
    "ttl_seconds",
    "verify",
]
