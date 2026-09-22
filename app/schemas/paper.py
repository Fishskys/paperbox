"""Pydantic schemas for the paper metadata endpoints (MVP-SPEC section 2)."""

from __future__ import annotations

from datetime import date, datetime

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


__all__ = ["PaperChunkList", "PaperChunkOut", "PaperFileOut", "PaperOut"]
