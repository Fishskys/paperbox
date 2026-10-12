"""The read-access policy knob and the identities behind it (plan D1/D3/D7).

Two things are pinned here:

* ``AUTH_READ_ACCESS`` is a **single enum** (``public`` | ``key``): an unknown value
  refuses to start, while the combination that simply has no effect
  (``AUTH_ENABLED=false`` together with ``key``) stays **silent** — a legitimate
  configuration choice, not an error (plan D7, owner's ruling of 2026-10-12).
* The two virtual identities differ only by tier. ``is_anonymous`` is what a gate
  needs to tell "presented nothing" from "presented a read key"; with the master
  switch off the identity stays the anonymous **admin**, the behaviour every
  existing deployment depends on.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services import api_key_service


def _settings(**overrides) -> Settings:
    """Settings built from the overrides alone (never the developer's ``.env``).

    ``PAPER_API_KEY`` is blanked because these cases are about the read door, not
    about the bootstrap credential: the field default ``change-me`` together with
    ``AUTH_ENABLED=true`` is refused at startup on purpose.
    """
    overrides.setdefault("paper_api_key", "")
    return Settings(_env_file=None, **overrides)


def test_the_read_door_defaults_to_public() -> None:
    """G4: an unconfigured deployment behaves exactly like 2026-10-12."""
    settings = _settings(auth_enabled=True)
    assert settings.auth_read_access == "public"
    assert settings.reads_are_public is True


def test_the_read_door_accepts_the_key_tier() -> None:
    settings = _settings(auth_enabled=True, auth_read_access="key")
    assert settings.auth_read_access == "key"
    assert settings.reads_are_public is False


def test_the_read_door_value_is_normalized() -> None:
    """``.env`` files grow stray case and spaces; the enum is case-insensitive."""
    assert _settings(auth_read_access="  KEY ").auth_read_access == "key"


def test_an_unknown_read_door_refuses_to_start() -> None:
    """Same fail-fast shape as ``PARSER_BACKEND`` / ``CHUNK_MODE`` (plan D7)."""
    with pytest.raises(ValidationError) as excinfo:
        _settings(auth_read_access="open")
    assert "AUTH_READ_ACCESS" in str(excinfo.value)


@pytest.mark.parametrize("policy", ["public", "key"])
def test_a_read_door_without_a_master_switch_is_silent(caplog, policy: str) -> None:
    """D7: no startup WARNING, no startup error, for a combination without effect.

    The policy has nothing to act on while ``AUTH_ENABLED=false``; the information
    that it is inert lives in the manual, not in the log.
    """
    caplog.set_level(logging.DEBUG)
    settings = _settings(auth_enabled=False, auth_read_access=policy)
    assert settings.auth_enabled is False
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_the_master_switch_identity_is_the_anonymous_admin() -> None:
    """``AUTH_ENABLED=false`` keeps resolving every caller to a full-access identity."""
    identity = api_key_service.anonymous()
    assert identity == api_key_service.AuthIdentity(
        prefix=api_key_service.ANONYMOUS_PREFIX,
        role=api_key_service.ROLE_ADMIN,
        source=api_key_service.SOURCE_ANONYMOUS,
        is_anonymous=True,
    )
    assert api_key_service.role_at_least(identity.role, api_key_service.ROLE_ADMIN)


def test_the_open_read_identity_differs_only_by_tier() -> None:
    """One shape, two tiers: the attribution stays ``anonymous`` either way."""
    admin = api_key_service.anonymous()
    reader = api_key_service.anonymous_reader()
    assert (reader.prefix, reader.source) == (admin.prefix, admin.source)
    assert reader.role == api_key_service.ROLE_READ
    assert reader.is_anonymous is True


def test_the_open_read_identity_only_satisfies_read() -> None:
    """It must never reach a write or admin gate (plan D2 invariant)."""
    reader = api_key_service.anonymous_reader()
    assert api_key_service.role_at_least(reader.role, api_key_service.ROLE_READ)
    assert not api_key_service.role_at_least(reader.role, api_key_service.ROLE_WRITE)
    assert not api_key_service.role_at_least(reader.role, api_key_service.ROLE_ADMIN)


def test_a_resolved_key_is_never_anonymous(session_factory) -> None:
    """``is_anonymous`` must mean exactly "the credential was not trusted" (D4)."""
    session = session_factory()
    try:
        _, full_key = api_key_service.create_key(
            session, prefix="reader", role=api_key_service.ROLE_READ
        )
        session.commit()
        identity = api_key_service.authenticate(session, full_key)
    finally:
        session.close()
    assert identity is not None
    assert identity.is_anonymous is False
