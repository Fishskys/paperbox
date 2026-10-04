"""Error codes of the MCP contract and how a failure reaches the caller.

``code`` values deliberately reuse vocabulary the project already has: the 14
ingestion ``error_code`` values (:mod:`app.core.errors`), the parsing degradation
codes and HTTP semantics. This module adds exactly two new ones -- ``WRITE_DISABLED``
(tool switched off) and ``SSRF_BLOCKED`` (inbound URL refused by the safety gate) --
so an agent can branch on a code it may already know from the REST API.

How a failure is delivered (contract doc section 3.3): the SDK turns a raised
:class:`mcp.server.mcpserver.exceptions.ToolError` into ``isError: true`` with the
exception's text as the only content, and logs it at INFO without a traceback. Any
*other* exception counts as a crash -- the caller then sees only
``Error executing tool <name>`` and the server logs a traceback at ERROR. So
:class:`ToolFailure` subclasses that ``ToolError`` and its ``__str__`` is the JSON of
the contract's error object, which is what lands in ``content[0].text`` (prefixed by
the SDK with ``Error executing tool <name>: ``).
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from app.core.errors import FAILURE_CODES
from app.mcp.models import ErrorInfo

#: Transport/argument level codes (the REST API maps these onto HTTP statuses).
NOT_FOUND = "NOT_FOUND"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
UNAUTHORIZED = "UNAUTHORIZED"
FORBIDDEN = "FORBIDDEN"
CONFLICT = "CONFLICT"
SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
TIMEOUT = "TIMEOUT"
INTERNAL = "INTERNAL"
#: Tool is switched off by configuration (hint names the environment variable).
WRITE_DISABLED = "WRITE_DISABLED"
#: Inbound URL refused by the SSRF gate (added in T-A11).
SSRF_BLOCKED = "SSRF_BLOCKED"

_TRANSPORT_CODES = (
    NOT_FOUND,
    INVALID_ARGUMENT,
    UNAUTHORIZED,
    FORBIDDEN,
    CONFLICT,
    SERVICE_UNAVAILABLE,
    TIMEOUT,
    INTERNAL,
    WRITE_DISABLED,
    SSRF_BLOCKED,
)

#: Every code a tool may return: the ingestion set plus the transport set above.
CODES: frozenset[str] = frozenset(FAILURE_CODES) | frozenset(_TRANSPORT_CODES)


class ToolFailure(ToolError):
    """An anticipated tool failure, in contract shape.

    Raising this (instead of letting a raw exception escape) is what tells the SDK
    "this was expected": the caller gets ``isError: true``, a code it can branch on,
    whether retrying is worth it, and -- when it is -- how long to wait.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
        hint: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        if code not in CODES:
            raise ValueError(f"unknown MCP error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        self.hint = hint
        #: Extra context for the audit log (never echoed to the caller wholesale).
        self.context = context

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            retry_after=self.retry_after,
            hint=self.hint,
        )

    def to_dict(self) -> dict[str, Any]:
        """The ``{"error": {...}}`` object as the contract defines it."""
        return {"error": self.to_info().model_dump(exclude_none=True)}

    def __str__(self) -> str:
        # This string is what the caller receives (the SDK prefixes it with the tool
        # name), so it has to stay parseable JSON.
        return json.dumps(self.to_dict(), ensure_ascii=False)


def write_disabled(variable: str, tool: str) -> ToolFailure:
    """The tool exists in this build but is switched off by configuration."""
    return ToolFailure(
        code=WRITE_DISABLED,
        message=f"{tool} is disabled on this server",
        hint=f"ask the operator to set {variable}=true (and MCP_WRITE_ENABLED=true)",
    )


def not_found(what: str, value: str) -> ToolFailure:
    return ToolFailure(code=NOT_FOUND, message=f"{what} {value!r} does not exist")


def invalid_argument(message: str, hint: str | None = None) -> ToolFailure:
    return ToolFailure(code=INVALID_ARGUMENT, message=message, hint=hint)


def internal(exc: BaseException) -> ToolFailure:
    """Last-resort mapping: never let a raw traceback reach the agent."""
    return ToolFailure(
        code=INTERNAL,
        message=f"{type(exc).__name__}: {exc}",
        hint="check the paperbox server logs; the request id is in the response headers",
    )
