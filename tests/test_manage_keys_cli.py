"""The key-management CLI, driven end to end against the in-memory store.

Two things are worth pinning here rather than at the service layer:

* an invalidating action (replacing a prefix, revoking a key) asks first and, when
  there is nobody to ask, refuses instead of guessing — a script that pipes input
  must not rotate a credential by accident;
* the full key is printed **once**: the CLI's only job an operator cannot redo.
"""

from __future__ import annotations

import re
import sys

import pytest

from app.core.config import settings
from app.services import api_key_service as keys
from scripts import manage_keys

KEY_IN_OUTPUT = re.compile(r"(pb_[a-z0-9_-]+_[0-9a-f]{32})")


@pytest.fixture()
def cli(monkeypatch, session_factory):
    """Point the CLI at the in-memory store and clear the env bootstrap keys."""
    monkeypatch.setattr(manage_keys, "SessionLocal", session_factory)
    monkeypatch.setattr(settings, "paper_api_key", "")
    monkeypatch.setattr(settings, "paper_api_keys", "")
    return manage_keys


def run_cli(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["manage_keys.py", *argv])
    return manage_keys.main()


def printed_key(capsys) -> str:
    match = KEY_IN_OUTPUT.search(capsys.readouterr().out)
    assert match is not None, "the CLI must print the full key exactly once"
    return match.group(1)


def authenticates(session_factory, full_key: str) -> bool:
    session = session_factory()
    try:
        return keys.authenticate(session, full_key) is not None
    finally:
        session.close()


def test_create_prints_the_key_once_and_lists_it(monkeypatch, cli, session_factory, capsys) -> None:
    assert run_cli(monkeypatch, "create", "--prefix", "alice-hermes", "--role", "read") == 0
    full_key = printed_key(capsys)
    assert authenticates(session_factory, full_key)

    assert run_cli(monkeypatch, "list") == 0
    out = capsys.readouterr().out
    assert "alice-hermes" in out and "read" in out
    assert full_key not in out  # list never carries key material


def test_a_taken_prefix_asks_before_replacing(
    monkeypatch, cli, session_factory, capsys
) -> None:
    """Confirming replaces the row: the old secret dies, the new one works."""
    assert run_cli(monkeypatch, "create", "--prefix", "hermes", "--role", "read") == 0
    old_key = printed_key(capsys)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert run_cli(monkeypatch, "create", "--prefix", "hermes", "--role", "write") == 0
    captured = capsys.readouterr()
    new_key = KEY_IN_OUTPUT.search(captured.out).group(1)

    assert new_key != old_key
    assert authenticates(session_factory, new_key)
    assert not authenticates(session_factory, old_key)

    session = session_factory()
    try:
        rows = keys.list_keys(session)
        assert [row.prefix for row in rows] == ["hermes"]  # one row, not two
        assert rows[0].role == "write"
    finally:
        session.close()


def test_declining_the_prompt_changes_nothing(
    monkeypatch, cli, session_factory, capsys
) -> None:
    assert run_cli(monkeypatch, "create", "--prefix", "hermes", "--role", "read") == 0
    old_key = printed_key(capsys)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert run_cli(monkeypatch, "create", "--prefix", "hermes", "--role", "admin") == 1
    printed_key_present = KEY_IN_OUTPUT.search(capsys.readouterr().out)
    assert printed_key_present is None  # no new key was issued
    assert authenticates(session_factory, old_key)


def test_without_a_terminal_the_command_refuses_instead_of_guessing(
    monkeypatch, cli, session_factory, capsys
) -> None:
    assert run_cli(monkeypatch, "create", "--prefix", "hermes", "--role", "read") == 0
    old_key = printed_key(capsys)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert run_cli(monkeypatch, "create", "--prefix", "hermes", "--role", "admin") == 1
    assert capsys.readouterr().err.count("--yes") == 1
    assert authenticates(session_factory, old_key)

    # ...and --yes is exactly how a non-interactive caller says "replace it"
    assert (
        run_cli(monkeypatch, "create", "--prefix", "hermes", "--role", "admin", "--yes") == 0
    )
    new_key = printed_key(capsys)
    assert authenticates(session_factory, new_key)
    assert not authenticates(session_factory, old_key)


def test_an_environment_row_is_never_replaced(
    monkeypatch, cli, session_factory, capsys
) -> None:
    """Deleting it would be undone on the next startup — the env owns that row."""
    monkeypatch.setattr(settings, "paper_api_key", "env-secret")
    session = session_factory()
    try:
        keys.sync_env_bootstrap(session)
        session.commit()
    finally:
        session.close()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert run_cli(monkeypatch, "create", "--prefix", "default", "--role", "read") == 1
    assert (
        run_cli(monkeypatch, "create", "--prefix", "default", "--role", "read", "--yes")
        == 1
    )
    err = capsys.readouterr().err
    assert "PAPER_API_KEYS" in err
    assert authenticates(session_factory, "env-secret")


def test_revoke_deletes_the_key_once_confirmed(
    monkeypatch, cli, session_factory, capsys
) -> None:
    assert run_cli(monkeypatch, "create", "--prefix", "hermes", "--role", "read") == 0
    full_key = printed_key(capsys)

    assert run_cli(monkeypatch, "revoke", "hermes", "--yes") == 0
    assert "deleted: hermes" in capsys.readouterr().out
    assert not authenticates(session_factory, full_key)

    assert run_cli(monkeypatch, "list") == 0
    assert "hermes" not in capsys.readouterr().out
    # and the prefix is free again, so re-issuing needs no hand-written SQL
    assert run_cli(monkeypatch, "create", "--prefix", "hermes", "--role", "read") == 0


def test_revoke_without_confirmation_changes_nothing(
    monkeypatch, cli, session_factory, capsys
) -> None:
    assert run_cli(monkeypatch, "create", "--prefix", "hermes", "--role", "read") == 0
    full_key = printed_key(capsys)

    assert run_cli(monkeypatch, "revoke", "hermes") == 1
    assert "--yes" in capsys.readouterr().err
    assert authenticates(session_factory, full_key)


def test_revoking_an_unknown_prefix_is_refused(monkeypatch, cli, capsys) -> None:
    assert run_cli(monkeypatch, "revoke", "ghost", "--yes") == 1
    assert "no key with prefix" in capsys.readouterr().err


def test_an_environment_row_refuses_revocation(
    monkeypatch, cli, session_factory, capsys
) -> None:
    monkeypatch.setattr(settings, "paper_api_key", "env-secret")
    session = session_factory()
    try:
        keys.sync_env_bootstrap(session)
        session.commit()
    finally:
        session.close()

    assert run_cli(monkeypatch, "revoke", "default", "--yes") == 1
    assert "PAPER_API_KEYS" in capsys.readouterr().err
    assert authenticates(session_factory, "env-secret")


def test_bad_input_is_refused_without_a_prompt(
    monkeypatch, cli, session_factory, capsys
) -> None:
    assert run_cli(monkeypatch, "create", "--prefix", "UPPER", "--role", "read") == 1
    assert "refused:" in capsys.readouterr().err
    # an unknown tier never reaches the store: argparse rejects it first
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "create", "--prefix", "fine", "--role", "root")
