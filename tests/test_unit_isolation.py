"""The unit entrypoint must stop libpq before it can open a real connection."""

import socket
import os

import psycopg
import pytest
from sqlalchemy import create_engine, text

from scripts.test_unit import isolated_io


def test_external_connections_are_blocked_before_the_database_driver(monkeypatch):
    def unexpected_connect(*args, **kwargs):
        pytest.fail("An external database connection reached the driver")

    monkeypatch.setattr(psycopg, "connect", unexpected_connect)
    engine = create_engine("postgresql+psycopg://test-only@database.invalid/test")
    try:
        with isolated_io():
            with pytest.raises(RuntimeError, match="external databases"):
                engine.connect()
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                with pytest.raises(OSError, match="cannot open TCP"):
                    connection.connect(("127.0.0.1", 9))
    finally:
        engine.dispose()


def test_sqlite_remains_available_to_unit_tests():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with isolated_io(), engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()


def test_file_logging_is_disabled_before_configuration_import_and_restored(monkeypatch):
    from app.core.config import Settings
    monkeypatch.setenv("LOG_FILE", "logs/app/production-destination.log")
    with isolated_io():
        assert Settings(_env_file=None).log_file == ""
    assert os.environ["LOG_FILE"] == "logs/app/production-destination.log"