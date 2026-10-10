"""Pydantic schemas for the metadata endpoints (import, review, manual edits)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SourceOut(BaseModel):
    """One ``paper_sources`` row (``raw`` is deliberately not exposed)."""

    model_config = ConfigDict(extra="ignore")

    source_id: str
    paper_id: str | None = None
    source_type: str
    source_ref: str
    content_type: str | None = None
    match_status: str
    match_method: str | None = None
    match_confidence: float | None = None
    importer: str | None = None
    fetched_at: datetime | None = None
    imported_at: datetime | None = None


class IdentifierOut(BaseModel):
    """One ``paper_identifiers`` row."""

    model_config = ConfigDict(extra="ignore")

    scheme: str
    value: str
    normalized_value: str
    is_primary: bool = False
    first_source_id: str | None = None


class ProvenanceEntry(BaseModel):
    """One field claim (current or historical)."""

    model_config = ConfigDict(extra="ignore")

    provenance_id: str
    value: Any = None
    source_id: str | None = None
    confidence: float | None = None
    is_current: bool = False
    decided_by: str
    decided_at: datetime | None = None


class PaperMetadataOut(BaseModel):
    """Response of ``GET /api/papers/{paper_id}/metadata``."""

    model_config = ConfigDict(extra="ignore")

    paper_id: str
    status: str
    fingerprint: str
    values: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, list[ProvenanceEntry]] = Field(default_factory=dict)
    sources: list[SourceOut] = Field(default_factory=list)
    identifiers: list[IdentifierOut] = Field(default_factory=list)
    tags: dict[str, list[str]] = Field(default_factory=dict)


class MetadataPatch(BaseModel):
    """Body of ``PATCH /api/papers/{paper_id}/metadata``.

    Every field is optional; only the ones present are written. Unknown keys are
    reported back in ``rejected`` rather than silently ignored.
    """

    model_config = ConfigDict(extra="allow")

    title: str | None = None
    abstract: str | None = None
    language: str | None = None
    year: int | None = None
    venue: str | dict[str, Any] | None = None
    venue_year: int | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    paper_type: str | None = None
    publication_date: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    authors: list[str] | str | None = None
    tags: list[str] | str | None = None
    url: str | None = None


class MetadataPatchOut(BaseModel):
    """Result of a manual edit."""

    model_config = ConfigDict(extra="ignore")

    paper_id: str
    fields: list[str] = Field(default_factory=list)
    fingerprint: str | None = None
    rejected: list[str] = Field(default_factory=list)


class MetadataRollbackIn(BaseModel):
    """Body of ``POST /api/papers/{paper_id}/metadata/rollback``."""

    field: str
    provenance_id: str


class MetadataRollbackOut(BaseModel):
    """Result of a rollback."""

    model_config = ConfigDict(extra="ignore")

    paper_id: str
    field: str
    provenance_id: str
    value: Any = None
    decided_by: str


class ImportReportOut(BaseModel):
    """Response of ``POST /api/metadata/import`` (and the CLI report)."""

    model_config = ConfigDict(extra="ignore")

    #: 载荷里检测到的条目数（含解析失败与 limit 截断的）。
    detected: int = 0
    #: 进入匹配的条目数（= detected − failed − skipped）。
    total: int = 0
    matched: int = 0
    created_shell: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    unchanged: int = 0
    #: 解析失败被跳过的条目数（明细见 failures，单条脏数据不再让整批 422）。
    failed: int = 0
    #: 被 limit 截断、未处理的条目数。
    skipped: int = 0
    #: ``[{index, identifier, reason}]``。
    failures: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    dry_run: bool = True
    format: str = "generic"


class ConflictOut(BaseModel):
    """A field two sources disagree about (kept value + the one that lost)."""

    model_config = ConfigDict(extra="ignore")

    paper_id: str
    field: str
    kept: Any = None
    rejected: Any = None
    source_id: str | None = None
    decided_at: datetime | None = None


class ReviewOut(BaseModel):
    """Response of ``GET /api/metadata/review``."""

    model_config = ConfigDict(extra="ignore")

    total: int = 0
    items: list[SourceOut] = Field(default_factory=list)
    conflicts: list[ConflictOut] = Field(default_factory=list)


class AttachIn(BaseModel):
    """Body of ``POST /api/metadata/sources/{source_id}/attach``."""

    paper_id: str


class AttachOut(BaseModel):
    """Result of an attach."""

    model_config = ConfigDict(extra="ignore")

    source_id: str
    paper_id: str
    source_type: str
    match_status: str
    match_method: str | None = None
    merged_fields: list[str] = Field(default_factory=list)


class ApplyEntryIn(BaseModel):
    """One decision from a previous report: this source belongs to that paper."""

    source_ref: str
    paper_id: str
    source_type: str | None = None


class ApplyIn(BaseModel):
    """Body of ``POST /api/metadata/apply``."""

    entries: list[ApplyEntryIn] = Field(default_factory=list)
    mode: Literal["fill", "overwrite"] = "fill"
    fields: list[str] | None = None


class ApplyOut(BaseModel):
    """Result of a batch apply."""

    model_config = ConfigDict(extra="ignore")

    applied: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "ApplyEntryIn",
    "ApplyIn",
    "ApplyOut",
    "AttachIn",
    "AttachOut",
    "ConflictOut",
    "IdentifierOut",
    "ImportReportOut",
    "MetadataPatch",
    "MetadataPatchOut",
    "MetadataRollbackIn",
    "MetadataRollbackOut",
    "PaperMetadataOut",
    "ProvenanceEntry",
    "ReviewOut",
    "SourceOut",
]