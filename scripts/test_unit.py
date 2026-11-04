#!/usr/bin/env python3
"""Run isolated unit tests with TCP connections blocked before application import."""

from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    import pytest

    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def block(function):
        def guarded(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                raise OSError("Unit tests cannot open TCP connections; use a stub or scripts/ acceptance")
            return function(sock, address)
        return guarded

    socket.socket.connect = block(connect)
    socket.socket.connect_ex = block(connect_ex)
    try:
        return pytest.main(sys.argv[1:] or ["tests", "--tb=short"])
    finally:
        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex


if __name__ == "__main__":
    raise SystemExit(main())