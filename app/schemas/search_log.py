"""Pydantic schemas for ``GET /api/search-logs`` (SPEC-P1 section B)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SearchLogOut(BaseModel):
    """One logged ``POST /api/search`` call."""

    model_config = ConfigDict(extra="ignore")

    id: str
    request_id: str | None = None
    created_at: datetime | None = None
    query: str
    #: Rewritten query actually used for retrieval (``None`` when not applied).
    rewritten_query: str | None = None
    mode: str
    top_k: int
    rerank: bool = False
    filters: dict[str, Any] | None = None
    candidates: int | None = None
    returned: int = 0
    took_ms: int | None = None
    results: list[dict[str, Any]] | None = None


class SearchLogListOut(BaseModel):
    """Response body of ``GET /api/search-logs``."""

    model_config = ConfigDict(extra="ignore")

    total: int = 0
    logs: list[SearchLogOut] = Field(default_factory=list)


__all__ = ["SearchLogListOut", "SearchLogOut"]
