"""Pydantic schemas for the ingestion endpoints (MVP-SPEC section 2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IngestRequest(BaseModel):
    """Body of ``POST /api/papers/ingest`` (URL ingestion)."""

    model_config = ConfigDict(extra="forbid")

    source_type: str = Field(default="url", description="Only url is supported.")
    source: str = Field(description="HTTP(S) URL of the PDF to ingest.")

    @field_validator("source_type")
    @classmethod
    def _check_source_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != "url":
            raise ValueError("source_type must be url")
        return normalized

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.lower().startswith(("http://", "https://")):
            raise ValueError("source must be an http(s) URL")
        return normalized


class IngestAccepted(BaseModel):
    """Immediate response of both ingestion endpoints."""

    model_config = ConfigDict(extra="ignore")

    job_id: str
    paper_id: str | None = None
    status: str
    duplicate: bool = False
    stage: str | None = None
    created_at: datetime | None = None
    message: str | None = None


__all__ = ["IngestAccepted", "IngestRequest"]
