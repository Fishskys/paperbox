"""Distributable API keys: the single authentication source for REST and MCP.

Plan: ``.hermes/plans/2026-10-05_145619-api-auth-keys-roles.md``. One credential
shape — ``Authorization: Bearer pb_<prefix>_<32hex>`` — resolves to an
:class:`AuthIdentity` through one function, :func:`authenticate`, that both the
REST dependency (:mod:`app.core.security`) and the MCP middleware
(:mod:`app.mcp.auth`) call. That is what makes requirement 4 ("MCP and HTTP
share keys") true by construction instead of by convention.

Storage rules (contract, plan §5–§6):

* the database stores **only** ``sha256(full_key)`` — the full key exists once,
  printed by ``scripts/manage_keys.py create``;
* ``prefix`` is the public half and the log-attribution key, hence unique;
* ``role`` tiers are monotonic: ``read < write < admin``;
* keys derived from the environment (``PAPER_API_KEY`` / ``PAPER_API_KEYS``) are
  bootstrap **admins**: they are matched before the database (so a deployment
  without reachable DB still authenticates its operator) and re-synced into the
  table on startup, where they cannot be revoked while the env references them.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import ApiKey, new_uuid

logger = get_logger(__name__)

#: Monotonic permission tiers (plan §4.1): ``read`` can call everything that
#: does not change state, ``write`` adds ingestion/metadata/reindex, ``admin``
#: adds deletion (and, later, key management over HTTP).
ROLE_READ = "read"
ROLE_WRITE = "write"
ROLE_ADMIN = "admin"
ROLES: frozenset[str] = frozenset({ROLE_READ, ROLE_WRITE, ROLE_ADMIN})
_ROLE_ORDER: dict[str, int] = {ROLE_READ: 0, ROLE_WRITE: 1, ROLE_ADMIN: 2}

#: Where a matched identity came from (the audit line keeps this granular).
SOURCE_SHARED = "PAPER_API_KEY"
SOURCE_KEYS = "PAPER_API_KEYS"
SOURCE_DB = "db"
SOURCE_ANONYMOUS = "anonymous"

#: Agent name for the shared ``PAPER_API_KEY`` (same value the MCP contract uses).
SHARED_AGENT = "default"

ANONYMOUS_NAME = "anonymous"
ANONYMOUS_PREFIX = "anonymous"

#: The user-chosen, log-visible half of a key. Lowercase keeps log lines greppable.
PREFIX_PATTERN = re.compile(r"^[a-z0-9_-]{2,16}$")

#: Literal default that must never become a live credential (startup guard).
DEFAULT_KEY = "change-me"


@dataclass(frozen=True)
class AuthIdentity:
    """Who is calling, as far as the process can tell."""

    name: str
    prefix: str
    role: str
    source: str

    @property
    def is_shared(self) -> bool:
        """True when the caller used the shared ``PAPER_API_KEY``."""
        return self.source == SOURCE_SHARED


def anonymous() -> AuthIdentity:
    """Identity used when ``AUTH_ENABLED=false``: full access, no attribution."""
    return AuthIdentity(
        name=ANONYMOUS_NAME, prefix=ANONYMOUS_PREFIX, role=ROLE_ADMIN, source=SOURCE_ANONYMOUS
    )


def role_at_least(role: str, minimum: str) -> bool:
    """Whether ``role`` satisfies the ``minimum`` tier (monotonic order)."""
    return _ROLE_ORDER.get(role, -1) >= _ROLE_ORDER.get(minimum, len(_ROLE_ORDER))


def hash_key(full_key: str) -> str:
    """``sha256`` of the full key — the only form ever persisted."""
    return hashlib.sha256(full_key.encode()).hexdigest()


def generate_full_key(prefix: str) -> str:
    """``pb_<prefix>_<32hex>``: 128 random bits, shown exactly once at creation."""
    return f"pb_{prefix}_{secrets.token_hex(16)}"


def derive_prefix(name: str) -> str:
    """Deterministic prefix for an environment key name (plan §6).

    ``PAPER_API_KEYS`` names become ``prefix`` values; sanitize rather than
    reject so a startup with an awkward name still yields its bootstrap admin.
    """
    cleaned = re.sub(r"[^a-z0-9_-]", "-", name.strip().lower())
    cleaned = cleaned.strip("-") or "key"
    return cleaned[:16] if len(cleaned) >= 2 else f"{cleaned}-key"[:16]


def _env_identities() -> dict[str, AuthIdentity]:
    """``sha256(full_key) -> identity`` for every environment credential.

    Rebuilt on every call on purpose (no cache): tests monkeypatch
    ``settings.paper_api_key`` between calls, and a stale index would silently
    authenticate with a key the operator already rotated. The dict is tiny
    (one entry per configured key).
    """
    index: dict[str, AuthIdentity] = {}
    shared = (settings.paper_api_key or "").strip()
    # The well-known default is never a credential: with auth on it is refused at
    # startup, with auth off every caller is anonymous anyway.
    if shared and shared != DEFAULT_KEY:
        index[hash_key(shared)] = AuthIdentity(
            name=SHARED_AGENT,
            prefix=derive_prefix(SHARED_AGENT),
            role=ROLE_ADMIN,
            source=SOURCE_SHARED,
        )
    for name, key in settings.agent_keys.items():
        index[hash_key(key)] = AuthIdentity(
            name=name,
            prefix=derive_prefix(name),
            role=ROLE_ADMIN,
            source=SOURCE_KEYS,
        )
    return index


def authenticate_env(token: str | None) -> AuthIdentity | None:
    """Match against environment credentials only (no database access)."""
    if not token:
        return None
    return _env_identities().get(hash_key(token))


def authenticate(session: Session, token: str | None) -> AuthIdentity | None:
    """Resolve one bearer token to an identity, or ``None`` when nothing matches.

    Environment keys win over database rows: they are the operator's recovery
    path, so a same-valued database key can never lock the operator out. The
    digest lookup is an equality probe on a high-entropy hash — there is no
    timing side channel worth constant-time paranoia beyond the final
    ``compare_digest`` between the fetched row and the presented digest.
    """
    if not token:
        return None
    digest = hash_key(token)
    env_identity = _env_identities().get(digest)
    if env_identity is not None:
        return env_identity
    row = (
        session.execute(
            select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.revoked_at.is_(None))
        )
        .scalars()
        .first()
    )
    if row is None or not hmac.compare_digest(row.key_hash, digest):
        return None
    return AuthIdentity(
        name=row.name, prefix=row.prefix, role=row.role, source=SOURCE_DB
    )


# --------------------------------------------------------------------------- #
# startup bootstrap (plan §7.2)
# --------------------------------------------------------------------------- #
def sync_env_bootstrap(session: Session) -> list[str]:
    """Upsert environment keys as admin rows; prune env rows the env dropped.

    The environment is the source of truth for ``source='env'`` rows: a name no
    longer present in ``PAPER_API_KEYS`` must not survive as a stale admin.
    Returns the prefixes written (for the startup log).
    """
    touched: list[str] = []
    desired = {
        identity.name: (identity.prefix, digest)
        for digest, identity in _env_identities().items()
    }
    existing = {
        row.name: row
        for row in session.execute(select(ApiKey).where(ApiKey.source == "env"))
        .scalars()
        .all()
    }
    for name, (prefix, digest) in desired.items():
        row = existing.get(name)
        if row is None:
            row = ApiKey(
                id=new_uuid(),
                name=name,
                prefix=_free_prefix(session, prefix),
                key_hash=digest,
                role=ROLE_ADMIN,
                source="env",
                note="derived from the environment at startup",
            )
            session.add(row)
        elif not hmac.compare_digest(row.key_hash, digest):
            row.key_hash = digest
        touched.append(row.prefix)
    for name, row in existing.items():
        if name not in desired:
            session.delete(row)
    session.flush()
    return touched


def _free_prefix(session: Session, base: str) -> str:
    """First unused variant of ``base`` (``base``, ``base-2``, ``base-3``, ...).

    Only needed when a database key already claims the derived prefix; the env
    path keeps authenticating with the unsuffixed identity either way.
    """
    candidate = base
    counter = 2
    while True:
        clash = session.execute(
            select(ApiKey.id).where(ApiKey.prefix == candidate)
        ).first()
        if clash is None:
            return candidate
        candidate = f"{base}-{counter}"[:16]
        counter += 1


def startup_bootstrap() -> None:
    """Sync env keys into the database at startup (never blocks a closed API).

    With ``AUTH_ENABLED=true`` a failure here is fatal — the operator would face
    a service that cannot authenticate anyone. With auth off the keys are
    decorative, so the failure is only a warning.
    """
    from app.db.session import SessionLocal

    try:
        session = SessionLocal()
        try:
            prefixes = sync_env_bootstrap(session)
            session.commit()
        finally:
            session.close()
    except SQLAlchemyError as exc:
        if settings.auth_enabled:
            raise RuntimeError(
                "AUTH_ENABLED=true but the api_keys bootstrap failed "
                f"({type(exc).__name__}: {exc}); run `alembic upgrade head` first"
            ) from exc
        logger.warning(
            "api key bootstrap skipped: database unavailable (%s)", type(exc).__name__
        )
        return
    if prefixes:
        logger.info(
            "environment keys synced as bootstrap admins",
            extra={"extra_fields": {"prefixes": prefixes}},
        )


def live_key_count(session: Session) -> int:
    """Number of usable keys (env rows included). Guard for ``AUTH_ENABLED=true``."""
    return int(
        session.execute(
            select(func.count()).select_from(ApiKey).where(ApiKey.revoked_at.is_(None))
        ).scalar_one()
    )


# --------------------------------------------------------------------------- #
# key management (scripts/manage_keys.py; HTTP admin API deliberately absent)
# --------------------------------------------------------------------------- #
class KeyManagementError(ValueError):
    """A create/revoke request the store refuses (bad input or constraint)."""


def create_key(
    session: Session,
    *,
    name: str,
    prefix: str,
    role: str,
    note: str | None = None,
) -> tuple[ApiKey, str]:
    """Create one database key; returns ``(row, full_key)`` — print it once.

    Raises :class:`KeyManagementError` for unusable input and lets
    :class:`IntegrityError` surface for prefix/name clashes (the CLI turns them
    into a friendly message).
    """
    name = (name or "").strip()
    prefix = (prefix or "").strip()
    if not name or len(name) > 64:
        raise KeyManagementError("name must be 1..64 characters")
    if not PREFIX_PATTERN.fullmatch(prefix):
        raise KeyManagementError(
            "prefix must match [a-z0-9_-]{2,16} (it appears in every log line)"
        )
    if role not in ROLES:
        raise KeyManagementError(f"role must be one of {sorted(ROLES)}")
    full_key = generate_full_key(prefix)
    row = ApiKey(
        id=new_uuid(),
        name=name,
        prefix=prefix,
        key_hash=hash_key(full_key),
        role=role,
        source="db",
        note=(note or None),
    )
    # The constraint clash must not sink the caller's transaction (a bare
    # session.rollback() would drop every uncommitted change before this call).
    # Both the add and the flush live inside the SAVEPOINT: an object added
    # outside it stays attached to the root transaction, and the savepoint
    # rollback would leave the root poisoned (PendingRollbackError) instead of
    # just discarding the rejected row.
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        raise KeyManagementError(
            "a key with the same name or prefix already exists"
        ) from exc
    return row, full_key


def get_by_prefix(session: Session, prefix: str) -> ApiKey | None:
    """Fetch one key row by its (unique, log-visible) prefix."""
    return (
        session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
        .scalars()
        .first()
    )


def revoke_key(session: Session, prefix: str) -> ApiKey:
    """Revoke one key; environment bootstrap rows refuse while env references them."""
    row = get_by_prefix(session, prefix)
    if row is None:
        raise KeyManagementError(f"no key with prefix {prefix!r}")
    if row.source == "env":
        raise KeyManagementError(
            f"key {prefix!r} comes from the environment and cannot be revoked here; "
            "remove it from PAPER_API_KEY / PAPER_API_KEYS and restart"
        )
    row.revoked_at = datetime.now(timezone.utc)
    session.flush()
    return row


def list_keys(session: Session) -> list[ApiKey]:
    """All keys, oldest first (display only — never exposes key material)."""
    return list(
        session.execute(select(ApiKey).order_by(ApiKey.created_at, ApiKey.id))
        .scalars()
        .all()
    )


__all__ = [
    "ANONYMOUS_NAME",
    "ANONYMOUS_PREFIX",
    "DEFAULT_KEY",
    "PREFIX_PATTERN",
    "ROLE_ADMIN",
    "ROLE_READ",
    "ROLE_WRITE",
    "ROLES",
    "SOURCE_ANONYMOUS",
    "SOURCE_DB",
    "SOURCE_KEYS",
    "SOURCE_SHARED",
    "ApiKey",
    "AuthIdentity",
    "KeyManagementError",
    "SHARED_AGENT",
    "anonymous",
    "authenticate",
    "authenticate_env",
    "create_key",
    "derive_prefix",
    "generate_full_key",
    "get_by_prefix",
    "hash_key",
    "list_keys",
    "live_key_count",
    "revoke_key",
    "role_at_least",
    "startup_bootstrap",
    "sync_env_bootstrap",
]
