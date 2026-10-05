"""Structured logging with request_id propagation.

Log records are emitted as a single line:

    2026-09-11T12:00:00+0800 INFO     paperbox.request [req=abc123] message

The request id lives in a :class:`contextvars.ContextVar` so that every module
can pick it up without threading it through call signatures. FastAPI middleware
(bound in a later phase) sets it per request via :func:`bind_request_id`.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from app.core.config import settings

REQUEST_ID_HEADER = "X-Request-ID"

_request_id_var: ContextVar[str | None] = ContextVar("paperbox_request_id", default=None)
#: Readable prefix of the credential serving the current request (plan:
#: 2026-10-05_145619-api-auth-keys-roles §7.6). Never the full key — the prefix
#: is the attribution key: ``anonymous`` when auth is off, ``-`` outside a request.
_key_prefix_var: ContextVar[str | None] = ContextVar("paperbox_key_prefix", default=None)

_CONFIGURED = False


def get_request_id() -> str | None:
    """Current request id, or ``None`` outside of a request context."""
    return _request_id_var.get()


def bind_request_id(request_id: str | None) -> None:
    """Attach ``request_id`` to the current context (no-op when empty)."""
    if request_id:
        _request_id_var.set(request_id)


def bind_key_prefix(prefix: str | None) -> object | None:
    """Attach the caller's key prefix to the current context.

    Returns the ``ContextVar`` token so the caller can ``reset`` it in a
    ``finally`` (mirrors ``current_agent.set``/``reset`` in the MCP middleware).
    """
    if not prefix:
        return None
    return _key_prefix_var.set(prefix)


def get_key_prefix() -> str | None:
    """Current caller's key prefix, or ``None`` outside of a request context."""
    return _key_prefix_var.get()


class RequestIdFilter(logging.Filter):
    """Ensure every record exposes ``record.request_id`` and ``record.key_prefix``."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = get_request_id() or "-"
        if not getattr(record, "key_prefix", None):
            record.key_prefix = get_key_prefix() or "-"
        return True


class TextFormatter(logging.Formatter):
    """Human-friendly single-line formatter."""

    default_msec_format = "%s.%03d"

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = getattr(record, "request_id", None) or get_request_id() or "-"
        record.key_prefix = getattr(record, "key_prefix", None) or get_key_prefix() or "-"
        record.levelname = f"{record.levelname:<8}"
        base = super().format(record)
        extras = getattr(record, "extra_fields", None)
        if extras:
            base = f"{base} {json.dumps(extras, ensure_ascii=False, default=str)}"
        if record.exc_info and record.exc_text:
            base = f"{base}\n{record.exc_text}"
        return base


class JsonFormatter(logging.Formatter):
    """Machine-readable formatter (handy when logs are shipped somewhere)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.strip(),
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or get_request_id(),
            "key_prefix": getattr(record, "key_prefix", None) or get_key_prefix() or "-",
            "module": record.module,
            "line": record.lineno,
        }
        extras = getattr(record, "extra_fields", None)
        if extras:
            payload.update(extras)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str | None = None, *, json_output: bool = False) -> None:
    """Configure the root logger once per process."""
    global _CONFIGURED

    resolved_level = (level or settings.log_level or "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            TextFormatter(
                fmt="%(asctime)s %(levelname)s %(name)s [req=%(request_id)s] [key=%(key_prefix)s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)

    # uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(resolved_level)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, configuring logging on first use."""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
