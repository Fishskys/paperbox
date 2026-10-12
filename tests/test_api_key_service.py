"""Unit tests for the API key store (plan: 2026-10-05_145619-api-auth-keys-roles section 5-6).

Pure service layer over the in-memory SQLite schema — no HTTP, no real database.
The store is the single authentication source for REST and MCP, so these tests
pin the properties both surfaces rely on: hash-only storage, monotonic tiers,
env bootstrap precedence, and revocation taking effect on the next lookup.
"""

from __future__ import annotations

import re

import pytest

from app.core.config import settings
from app.services import api_key_service as keys


# --------------------------------------------------------------------------- #
# tiers and identities
# --------------------------------------------------------------------------- #
def test_role_tiers_are_monotonic() -> None:
    assert keys.role_at_least("read", "read")
    assert keys.role_at_least("write", "read")
    assert keys.role_at_least("admin", "read")
    assert keys.role_at_least("admin", "write")
    assert not keys.role_at_least("read", "write")
    assert not keys.role_at_least("write", "admin")
    assert not keys.role_at_least("unknown", "read")


def test_derive_prefix_sanitizes_a_label_for_logs() -> None:
    assert keys.derive_prefix("Hermes Main") == "hermes-main"
    assert keys.derive_prefix("codex!") == "codex"
    assert keys.derive_prefix("a") == "a-key"
    # 32 characters of room: ``owner-agent`` schemes fit, longer ones are truncated
    assert len(keys.derive_prefix("x" * 40)) == 32
    assert keys.derive_prefix("alice-hermes-laptop") == "alice-hermes-laptop"


def test_anonymous_identity_is_a_full_admin() -> None:
    identity = keys.anonymous()
    assert identity.role == keys.ROLE_ADMIN
    assert identity.prefix == "anonymous"
    assert identity.is_anonymous


def test_an_identity_has_no_second_name() -> None:
    """A credential is identified by its prefix alone (2026-10-12 decision)."""
    assert not hasattr(keys.AuthIdentity(prefix="x", role="read", source="db"), "name")


# --------------------------------------------------------------------------- #
# environment bootstrap keys
# --------------------------------------------------------------------------- #
def test_env_keys_authenticate_without_a_database(monkeypatch) -> None:
    monkeypatch.setattr(settings, "paper_api_key", "shared-secret")
    monkeypatch.setattr(settings, "paper_api_keys", "hermes:hermes-secret")

    shared = keys.authenticate_env("shared-secret")
    assert shared is not None and shared.prefix == "default"
    assert shared.is_shared and shared.role == keys.ROLE_ADMIN

    named = keys.authenticate_env("hermes-secret")
    assert named is not None and named.prefix == "hermes"
    assert not named.is_shared
    assert named.source == keys.SOURCE_KEYS

    assert keys.authenticate_env("nope") is None
    assert keys.authenticate_env(None) is None
    assert keys.authenticate_env("") is None


def test_the_well_known_default_is_never_a_credential(monkeypatch) -> None:
    monkeypatch.setattr(settings, "paper_api_key", "change-me")
    monkeypatch.setattr(settings, "paper_api_keys", "")
    assert keys.authenticate_env("change-me") is None


def test_a_named_key_wins_over_the_shared_value(monkeypatch) -> None:
    monkeypatch.setattr(settings, "paper_api_key", "dup")
    monkeypatch.setattr(settings, "paper_api_keys", "hermes:dup")
    identity = keys.authenticate_env("dup")
    assert identity is not None and identity.prefix == "hermes"


# --------------------------------------------------------------------------- #
# database keys
# --------------------------------------------------------------------------- #
def test_create_key_stores_only_the_hash(session_factory) -> None:
    session = session_factory()
    try:
        row, full_key = keys.create_key(
            session, prefix="ci", role="read", note="for the CI agent"
        )
        session.commit()
        assert re.fullmatch(r"pb_ci_[0-9a-f]{32}", full_key)
        assert row.key_hash == keys.hash_key(full_key)
        assert full_key not in str(row.key_hash)
        assert row.role == "read" and row.source == "db"

        identity = keys.authenticate(session, full_key)
        assert identity is not None
        assert (identity.prefix, identity.role) == ("ci", "read")
    finally:
        session.close()


def test_create_key_rejects_bad_input(session_factory) -> None:
    session = session_factory()
    try:
        with pytest.raises(keys.KeyManagementError):
            keys.create_key(session, prefix="UPPER", role="read")
        with pytest.raises(keys.KeyManagementError):
            keys.create_key(session, prefix="ab", role="root")
        with pytest.raises(keys.KeyManagementError):
            keys.create_key(session, prefix="", role="read")
        # 32 characters is the ceiling (alice-hermes-laptop-long fits, this does not)
        with pytest.raises(keys.KeyManagementError):
            keys.create_key(session, prefix="x" * 33, role="read")
    finally:
        session.close()


def test_a_long_prefix_is_accepted(session_factory) -> None:
    """The old 16-character ceiling refused ``owner-agent`` schemes outright."""
    session = session_factory()
    try:
        row, full_key = keys.create_key(
            session, prefix="alice-hermes-laptop", role="read"
        )
        session.commit()
        assert row.prefix == "alice-hermes-laptop"
        assert re.fullmatch(r"pb_alice-hermes-laptop_[0-9a-f]{32}", full_key)
    finally:
        session.close()


def test_the_prefix_is_unique(session_factory) -> None:
    session = session_factory()
    try:
        keys.create_key(session, prefix="alpha", role="read")
        with pytest.raises(keys.KeyManagementError):
            keys.create_key(session, prefix="alpha", role="write")
        # a different prefix is of course fine
        keys.create_key(session, prefix="beta", role="read")
        session.commit()
        assert {row.prefix for row in keys.list_keys(session)} == {"alpha", "beta"}
    finally:
        session.close()


def test_revocation_takes_effect_on_the_next_lookup(session_factory) -> None:
    session = session_factory()
    try:
        _, full_key = keys.create_key(session, prefix="tmp", role="write")
        session.commit()
        assert keys.authenticate(session, full_key) is not None
        row = keys.revoke_key(session, "tmp")
        assert row.revoked_at is not None
        assert keys.authenticate(session, full_key) is None
        # idempotence is not promised for unknown prefixes
        with pytest.raises(keys.KeyManagementError):
            keys.revoke_key(session, "missing")
    finally:
        session.close()


def test_environment_rows_refuse_revocation(monkeypatch, session_factory) -> None:
    monkeypatch.setattr(settings, "paper_api_key", "env-secret")
    session = session_factory()
    try:
        keys.sync_env_bootstrap(session)
        session.commit()
        with pytest.raises(keys.KeyManagementError):
            keys.revoke_key(session, "default")
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# startup bootstrap: the environment is the source of truth for env rows
# --------------------------------------------------------------------------- #
def test_bootstrap_upserts_admins_and_prunes_stale_rows(monkeypatch, session_factory) -> None:
    monkeypatch.setattr(settings, "paper_api_key", "shared-secret")
    monkeypatch.setattr(settings, "paper_api_keys", "hermes:hermes-secret")
    session = session_factory()
    try:
        keys.sync_env_bootstrap(session)
        session.commit()

        rows = {row.prefix: row for row in keys.list_keys(session)}
        assert set(rows) == {"default", "hermes"}
        assert all(row.role == keys.ROLE_ADMIN for row in rows.values())
        assert all(row.source == "env" for row in rows.values())
        assert rows["default"].key_hash == keys.hash_key("shared-secret")

        # rotating the env key updates the hash in place
        monkeypatch.setattr(settings, "paper_api_key", "rotated-secret")
        keys.sync_env_bootstrap(session)
        session.commit()
        rows = {row.prefix: row for row in keys.list_keys(session)}
        assert rows["default"].key_hash == keys.hash_key("rotated-secret")

        # dropping an entry from the env prunes its row (no stale admins)
        monkeypatch.setattr(settings, "paper_api_keys", "")
        keys.sync_env_bootstrap(session)
        session.commit()
        assert {row.prefix for row in keys.list_keys(session)} == {"default"}

        # and database rows are never touched by the sync
        keys.create_key(session, prefix="human", role="read")
        session.commit()
        keys.sync_env_bootstrap(session)
        session.commit()
        assert {row.prefix for row in keys.list_keys(session)} == {"default", "human"}
    finally:
        session.close()


def test_live_key_count_ignores_revoked_rows(monkeypatch, session_factory) -> None:
    monkeypatch.setattr(settings, "paper_api_key", "")
    monkeypatch.setattr(settings, "paper_api_keys", "")
    session = session_factory()
    try:
        assert keys.live_key_count(session) == 0
        _, first = keys.create_key(session, prefix="aaa", role="read")
        keys.create_key(session, prefix="bbb", role="read")
        session.commit()
        assert keys.live_key_count(session) == 2
        keys.revoke_key(session, "aaa")
        session.commit()
        assert keys.live_key_count(session) == 1
        assert keys.authenticate(session, first) is None
    finally:
        session.close()


def test_env_keys_win_over_a_same_valued_database_key(monkeypatch, session_factory) -> None:
    """The env key is the recovery path: it must never be lockable from the DB."""
    monkeypatch.setattr(settings, "paper_api_key", "dup-secret")
    session = session_factory()
    try:
        # simulate a db row carrying the same hash at a lower tier
        keys.create_key(session, prefix="shadow", role="read")
        row = keys.get_by_prefix(session, "shadow")
        row.key_hash = keys.hash_key("dup-secret")
        session.commit()
        identity = keys.authenticate(session, "dup-secret")
        assert identity is not None
        assert identity.role == keys.ROLE_ADMIN
        assert identity.source == keys.SOURCE_SHARED
    finally:
        session.close()
