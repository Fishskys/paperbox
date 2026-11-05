#!/usr/bin/env python3
"""Run unit tests with TCP and external SQL connections blocked before app import."""

from contextlib import contextmanager
from pathlib import Path
import os
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@contextmanager
def isolated_io():
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    previous_log_file = os.environ.get("LOG_FILE")
    os.environ["LOG_FILE"] = ""

    def block(function):
        def guarded(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                raise OSError("Unit tests cannot open TCP connections; use a stub or scripts/ acceptance")
            return function(sock, address)
        return guarded

    socket.socket.connect = block(connect)
    socket.socket.connect_ex = block(connect_ex)

    def block_database(dialect, connection_record, args, kwargs):
        if dialect.name != "sqlite":
            raise RuntimeError("Unit tests cannot connect to external databases; use SQLite or a stub")

    # libpq connects below Python's socket API, so guard before driver dispatch.
    event.listen(Engine, "do_connect", block_database)
    try:
        yield
    finally:
        event.remove(Engine, "do_connect", block_database)
        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        if previous_log_file is None:
            os.environ.pop("LOG_FILE", None)
        else:
            os.environ["LOG_FILE"] = previous_log_file


def main() -> int:
    import pytest

    with isolated_io():
        return pytest.main(sys.argv[1:] or ["tests", "--tb=short"])


if __name__ == "__main__":
    raise SystemExit(main())