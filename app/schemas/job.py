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


class QueueOut(BaseModel):
    """Response body of ``GET /api/jobs/queue`` (in-process ingestion queue)."""

    model_config = ConfigDict(extra="ignore")

    #: Whether the worker coroutines are alive (the app lifespan starts them).
    started: bool = False
    #: Ceiling on parallel pipelines (``INGEST_CONCURRENCY``).
    concurrency: int = 1
    #: Pipelines executing right now.
    running: int = 0
    #: Jobs waiting for a free slot, in insertion order.
    queued: int = 0
    #: Of ``queued``, how many are interactive (single-file uploads).
    queued_high: int = 0
    #: Of ``queued``, how many are batch (multi-file / folder uploads).
    queued_low: int = 0
    running_job_ids: list[str] = Field(default_factory=list)
    queued_job_ids: list[str] = Field(default_factory=list)


__all__ = ["JobListOut", "JobOut", "QueueOut"]
