"""Pydantic schemas for ``GET /api/consistency`` (the three-way drift report)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PaperConsistencyOut(BaseModel):
    """One paper whose three copies disagree (PostgreSQL / MinIO / OpenSearch)."""

    model_config = ConfigDict(extra="ignore")

    paper_id: str
    title: str | None = None
    status: str
    deleted: bool = False
    files_pg: int = 0
    objects_minio: int = 0
    chunks_pg: int = 0
    chunks_os: int = 0
    issues: list[str] = Field(default_factory=list)
    missing_objects: list[str] = Field(default_factory=list)
    orphan_objects: list[str] = Field(default_factory=list)
    #: Parser provenance: what PostgreSQL stamps vs what the documents carry.
    parser_backend: str | None = None
    index_backends: list[str] = Field(default_factory=list)


class ConsistencyTotalsOut(BaseModel):
    """Store-wide counters (always exact, regardless of ``limit``)."""

    model_config = ConfigDict(extra="ignore")

    papers: int = 0
    papers_live: int = 0
    papers_deleted: int = 0
    files_pg: int = 0
    objects_minio: int = 0
    chunks_pg: int = 0
    documents_os: int = 0
    staging_objects: int = 0
    problems: int = 0
    orphan_objects: int = 0
    orphan_documents: int = 0


class ParserBackendsOut(BaseModel):
    """Which parser produced the chunks, counted on both sides.

    ``papers`` counts live paper rows by their stamp; ``documents`` counts index
    documents, so a half-applied backend switch shows up as a paper counted under
    one backend and its documents under another. ``unknown`` means "no stamp"
    (rows written before the stamp existed).
    """

    model_config = ConfigDict(extra="ignore")

    papers: dict[str, int] = Field(default_factory=dict)
    documents: dict[str, int] = Field(default_factory=dict)


class ConsistencyOut(BaseModel):
    """The full report: totals, per-paper problems, store-level orphans, errors."""

    model_config = ConfigDict(extra="ignore")

    checked_at: str
    consistent: bool
    index: str
    index_exists: bool
    totals: ConsistencyTotalsOut
    parser_backends: ParserBackendsOut = Field(default_factory=ParserBackendsOut)
    problems: list[PaperConsistencyOut] = Field(default_factory=list)
    orphan_objects: list[str] = Field(default_factory=list)
    orphan_documents: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    truncated: bool = False
    took_ms: float = 0.0