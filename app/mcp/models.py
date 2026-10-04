"""Typed contract of the MCP tools.

Every tool result carries :class:`Envelope` -- ``data`` plus ``meta``, ``warnings``
and ``citations`` -- and every failure carries :class:`ErrorEnvelope`
(``docs/architecture/11-mcp-agent-interface.md`` section 3). The MCP ``tools/list``
schema is generated from these models, so nothing here may degrade into a bare
``object``: a caller has to be able to see the shape without guessing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.paper import PaperDegradationOut, PaperOut

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


class ChunkView(BaseModel):
    """One chunk of a paper as the reading tools publish it.

    ``page``/``page_end``/``section`` come from the parser (they are what makes a
    citation exact); ``primary`` marks the chunk the caller asked about in
    ``paper_get_context``; ``truncated`` means the text was cut to fit the caller's
    character budget -- the schema never silently shortens a chunk.
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    #: Reading order inside the paper (0-based).
    chunk_index: int
    page: int | None = None
    page_end: int | None = None
    section: str | None = None
    subsection: str | None = None
    text: str
    #: Characters of ``text`` in this response (after any budget cut).
    chars: int
    token_count: int | None = None
    #: True for the chunk the caller asked for (``paper_get_context`` only).
    primary: bool = False
    #: True when ``text`` was cut to fit the character budget.
    truncated: bool = False


class ChunkPageData(BaseModel):
    """``paper_get_chunks``: one page of a paper's chunks."""

    model_config = ConfigDict(extra="forbid")

    paper_id: str
    title: str = ""
    #: Chunks this paper has in total (so a caller knows how much is left).
    total: int
    returned: int
    offset: int
    #: The window the caller asked for, after validation.
    limit: int
    #: True when the character budget cut the page short.
    truncated: bool
    #: Pass this as ``offset`` to continue reading the paper; ``None`` when the
    #: caller has seen everything (either the budget cut the page or the paper
    #: simply continues -- both cases set it).
    next_offset: int | None = None
    chunks: list[ChunkView] = Field(default_factory=list)


class ContextData(BaseModel):
    """``paper_get_context``: a chunk plus its neighbours, in reading order."""

    model_config = ConfigDict(extra="forbid")

    paper_id: str
    title: str = ""
    target_chunk_id: str
    returned: int
    truncated: bool
    #: Neighbours the caller asked for that the paper does not have (the window
    #: hit the start or the end of the paper).
    missing_before: int = 0
    missing_after: int = 0
    chunks: list[ChunkView] = Field(default_factory=list)


class ProvenanceClaimView(BaseModel):
    """One metadata claim behind a paper field (the "where did this come from")."""

    model_config = ConfigDict(extra="forbid")

    provenance_id: str
    value: Any = None
    source_id: str | None = None
    confidence: float | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None


class PaperDetailData(BaseModel):
    """``paper_get``: metadata plus where it came from and how it was parsed."""

    model_config = ConfigDict(extra="forbid")

    paper: PaperOut
    #: Field name -> the claim that currently holds (full history is on REST).
    provenance: dict[str, list[ProvenanceClaimView]] = Field(default_factory=dict)
    #: Unresolved parsing degradations (also summarised in ``warnings``).
    degradations: list[PaperDegradationOut] = Field(default_factory=list)
    #: Chunks the paper has in PostgreSQL -- 0 means it was never chunked.
    chunk_count: int = 0


class FileData(BaseModel):
    """``paper_get_file``: a short-lived link to the stored original."""

    model_config = ConfigDict(extra="forbid")

    #: Signed URL, valid until ``expires_at``; carries no long-lived credential.
    download_url: str
    expires_at: datetime
    filename: str
    bytes: int | None = None
    content_type: str | None = None
    #: Highest page number the chunks mention (``None`` when unknown).
    page_count: int | None = None


class JobRefData(BaseModel):
    """``paper_import`` / ``paper_reindex``: the job a write queued."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    paper_id: str | None = None
    #: ``completed`` / ``failed`` / ``running`` (``running`` = the wait bound expired).
    status: str
    stage: str
    error_code: str | None = None
    error_message: str | None = None
    #: How long this call waited before answering.
    waited_s: int = 0


class ImportPreviewData(BaseModel):
    """``paper_import(dry_run=true)``: what would be queued, and nothing else."""

    model_config = ConfigDict(extra="forbid")

    source: str
    source_type: str
    #: Always ``False``: a dry run creates no job. Present so the shape is explicit.
    queued: bool = False
    filename: str | None = None
    size_bytes: int | None = None
    content_type: str | None = None
    #: For ``local_path``: the resolved path that passed the whitelist check.
    resolved_path: str | None = None


class ReindexPreviewData(BaseModel):
    """``paper_reindex(dry_run=true)``: the blast radius of a rebuild."""

    model_config = ConfigDict(extra="forbid")

    paper_id: str
    title: str = ""
    #: Chunks currently stored for this paper (they are replaced by the rebuild).
    chunks: int
    has_original_file: bool
    filename: str | None = None
    running_jobs: int = 0
    queued: bool = False


class DeletePreviewData(BaseModel):
    """``paper_delete``: what was (or would be) removed."""

    model_config = ConfigDict(extra="forbid")

    paper_id: str
    title: str = ""
    chunks: int
    objects: int
    object_bytes: int = 0
    #: Jobs still running for this paper -- deleting during a pipeline run is the
    #: caller's decision, but it must be visible.
    running_jobs: int = 0
    #: ``False`` on a dry run; ``True`` once the paper was actually soft-deleted.
    deleted: bool = False
    #: Objects removed (only meaningful when ``deleted`` is true).
    objects_removed: int = 0


class ChangeView(BaseModel):
    """One metadata field, before and after (``paper_update_metadata``)."""

    model_config = ConfigDict(extra="forbid")

    before: Any = None
    #: On a dry run this is the value the caller asked for, not what the writers
    #: would derive (venue resolution, fingerprint) -- applying it is what decides.
    after: Any = None


class MetadataPatchData(BaseModel):
    """``paper_update_metadata``: what changed (or would change)."""

    model_config = ConfigDict(extra="forbid")

    paper_id: str
    applied: bool = False
    changes: dict[str, ChangeView] = Field(default_factory=dict)
    #: Fields the service actually wrote (empty on a dry run).
    changed: list[str] = Field(default_factory=list)
    #: Keys the service refused (unknown patch keys).
    rejected: list[str] = Field(default_factory=list)
    fingerprint: str | None = None
    #: Where to undo it (contract section 5.2: rollback stays on REST).
    rollback: str | None = None
