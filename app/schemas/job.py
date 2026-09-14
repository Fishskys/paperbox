"""Pydantic schemas for the ingestion job status endpoint."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class JobOut(BaseModel):
    """Response body of ``GET /api/jobs/{job_id}``."""

    model_config = ConfigDict(extra="ignore")

    job_id: str
    paper_id: str | None = None
    stage: str
    progress: float = 0.0
    duplicate: bool = False
    #: Structured failure reason (SPEC-P1 A2); one of ``app.core.errors`` codes,
    #: or ``None`` when the job has not failed.
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


class JobListOut(BaseModel):
    """Response body of ``GET /api/jobs`` (recent jobs)."""

    model_config = ConfigDict(extra="ignore")

    total: int = 0
    jobs: list[JobOut] = Field(default_factory=list)


__all__ = ["JobListOut", "JobOut"]
