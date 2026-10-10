"""Pydantic schemas for the paper metadata endpoints (MVP-SPEC section 2)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PaperFileOut(BaseModel):
    """One stored artifact of a paper (a ``paper_files`` row)."""

    model_config = ConfigDict(extra="ignore")

    storage_key: str
    sha256: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    url: str | None = None


class PaperOut(BaseModel):
    """Response body of ``GET /api/papers/{paper_id}``."""

    model_config = ConfigDict(extra="ignore")

    paper_id: str
    title: str
    abstract: str | None = None
    language: str | None = None
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    url: str | None = None
    venue: str | None = None
    #: Metadata snapshot (see ``docs/architecture/08-metadata.md``): the edition
    #: year of the venue, the literature type and the citation fields.
    venue_year: int | None = None
    paper_type: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publication_date: date | None = None
    authors: list[str] = Field(default_factory=list)
    status: str
    fingerprint: str
    files: list[PaperFileOut] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PaperListOut(BaseModel):
    """Response body of ``GET /api/papers`` (browsing/pagination)."""

    model_config = ConfigDict(extra="ignore")

    total: int = 0
    limit: int = 20
    offset: int = 0
    papers: list[PaperOut] = Field(default_factory=list)


class PaperChunkOut(BaseModel):
    """One chunk of a paper (populated from phase 4 onwards)."""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str
    chunk_index: int
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    subsection: str | None = None
    text: str
    token_count: int | None = None
    char_count: int | None = None


class PaperChunkList(BaseModel):
    """Response body of ``GET /api/papers/{paper_id}/chunks``."""

    paper_id: str
    total: int = 0
    chunks: list[PaperChunkOut] = Field(default_factory=list)


class PaperDegradationOut(BaseModel):
    """One entry of the degradation ledger (plan T7.3).

    A degraded result is usable but thinner than it could have been -- docling
    was unreachable and pypdf took over, a section's sentences could not be
    embedded and the length policy cut it instead. ``resolved_at`` is set once a
    later run of the same stage no longer reported the cause.
    """

    model_config = ConfigDict(extra="ignore")

    stage: str
    code: str
    detail: dict = Field(default_factory=dict)
    occurrences: int = 1
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    resolved_at: datetime | None = None
    job_id: str | None = None


class PaperDegradationList(BaseModel):
    """Response body of ``GET /api/papers/{paper_id}/degradations``."""

    paper_id: str
    total: int = 0
    degraded: bool = False
    degradations: list[PaperDegradationOut] = Field(default_factory=list)


class ReindexIn(BaseModel):
    """Body of ``POST /api/papers/reindex``（批量重建索引）。

    默认 **dry_run=True**（与元数据导入一致）：先看"要重建哪些、为什么"，确认了再写。
    选择优先级：``paper_ids`` > ``include_all`` > ``reasons`` > 默认（所有检测到的理由的并集）。
    ``reasons`` 的取值来自 ``app/services/reindex_service.py`` 的 ``REASON_*``；
    新理由只要加进那份 DETECTORS 就自动可用，调用方不必改。
    """

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True
    include_all: bool = False
    paper_ids: list[str] | None = None
    reasons: list[str] | None = None


class ReindexOut(BaseModel):
    """批量重建的处置报告：为什么、选了几篇、排了哪些作业、跳过了什么。"""

    model_config = ConfigDict(extra="ignore")

    dry_run: bool
    selected: int
    queued: int = 0
    job_ids: list[str] = Field(default_factory=list)
    skipped: list[dict[str, str]] = Field(default_factory=list)
    reasons: list[dict[str, Any]] = Field(default_factory=list)
    skipped_reasons: list[dict[str, Any]] = Field(default_factory=list)
    embedding: dict[str, Any] = Field(default_factory=dict)
    parser: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


__all__ = [
    "PaperChunkList",
    "PaperChunkOut",
    "PaperFileOut",
    "PaperOut",
    "ReindexIn",
    "ReindexOut",
]
