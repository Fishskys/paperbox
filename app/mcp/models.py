"""Typed contract of the MCP tools.

Every tool result carries :class:`Envelope` -- ``data`` plus ``meta``, ``warnings``
and ``citations`` -- and every failure carries :class:`ErrorEnvelope`
(``docs/architecture/11-mcp-agent-interface.md`` section 3). The MCP ``tools/list``
schema is generated from these models, so nothing here may degrade into a bare
``object``: a caller has to be able to see the shape without guessing.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Citation(BaseModel):
    """A verifiable pointer into a paper.

    ``page`` and ``section`` come from the index (they are what lets an agent write
    "this is in section III-B on page 7"); ``quote`` is a short verbatim snippet and
    is never used for ranking.
    """

    model_config = ConfigDict(extra="forbid")

    paper_id: str
    #: Paper title, so a citation is readable without a second call.
    title: str = ""
    page: int | None = None
    section: str | None = None
    chunk_id: str
    #: Verbatim snippet (callers may shrink it; never a rewrite of the source).
    quote: str | None = None


class ToolMeta(BaseModel):
    """Who ran what, and how long it took."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    #: Agent name resolved from the bearer key (``default`` for the shared key).
    agent: str = "default"
    #: Tool contract version (``v1``; see the contract doc section 8).
    toolset: str = "v1"
    took_ms: int = 0


class ErrorInfo(BaseModel):
    """The error half of the contract (``isError: true`` + this object)."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    retry_after: int | None = None
    #: What the agent should try next (optional, but the useful part).
    hint: str | None = None


class Envelope(BaseModel, Generic[T]):
    """Uniform successful result: ``{data, meta, warnings, citations}``."""

    model_config = ConfigDict(extra="forbid")

    data: T
    meta: ToolMeta
    #: Truncation, degradations, clamped arguments -- empty list, never missing.
    warnings: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class ErrorEnvelope(BaseModel):
    """Uniform failed result."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorInfo
