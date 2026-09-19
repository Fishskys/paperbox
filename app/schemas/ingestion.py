"""Pydantic schemas for the ingestion endpoints (MVP-SPEC section 2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: ``status`` values of one entry in a batch upload result.
STATUS_ACCEPTED = "accepted"
STATUS_DUPLICATE = "duplicate"
STATUS_REJECTED = "rejected"


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
    """Immediate response of the single-source ingestion endpoints."""

    model_config = ConfigDict(extra="ignore")

    job_id: str
    paper_id: str | None = None
    status: str
    duplicate: bool = False
    stage: str | None = None
    created_at: datetime | None = None
    message: str | None = None


class IngestFileResult(BaseModel):
    """Per-file outcome inside a batch upload response (2026-09-19).

    ``status`` is ``accepted`` (job created and queued), ``duplicate`` (the
    content is already in the library -- no new paper) or ``rejected`` (the file
    never became a job; ``error_code`` says why).
    """

    model_config = ConfigDict(extra="ignore")

    filename: str
    status: str
    job_id: str | None = None
    paper_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    size_bytes: int | None = None


class IngestFilesAccepted(BaseModel):
    """Response body of ``POST /api/papers/ingest/files`` (2026-09-19).

    One request carries one or more files; each part is validated, staged and
    queued on its own, so a single bad file does not fail the request. The
    counts are always ``accepted + duplicate + rejected == len(results)``.
    """

    model_config = ConfigDict(extra="ignore")

    request_id: str
    accepted: int = 0
    duplicate: int = 0
    rejected: int = 0
    results: list[IngestFileResult] = Field(default_factory=list)
    message: str | None = None


class IngestDirRequest(BaseModel):
    """Body of ``POST /api/papers/ingest/dir`` (2026-09-19).

    The server reads the directory itself: this endpoint exists for the case
    where the PDFs already live on the same machine (or the same mounted
    volume) as the app, so a 1000-file import transfers zero bytes.
    """

    model_config = ConfigDict(extra="forbid")

    root: str = Field(description="Absolute directory to scan.")
    glob: str = Field(default="**/*.pdf", description="Pattern relative to root.")
    recursive: bool = Field(default=True, description="Walk subdirectories.")
    limit: int = Field(default=2000, ge=1, description="Stop after N matched files.")
    dry_run: bool = Field(
        default=False,
        description="Report what would be imported without creating any job.",
    )


class IngestDirJob(BaseModel):
    """One file the directory scan turned into a job (or would have)."""

    model_config = ConfigDict(extra="ignore")

    filename: str
    path: str | None = None
    status: str
    job_id: str | None = None
    paper_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    size_bytes: int | None = None


class IngestDirAccepted(BaseModel):
    """Response body of ``POST /api/papers/ingest/dir``.

    ``matched`` counts every file the glob found (before the ``limit`` cap),
    ``accepted``/``duplicate``/``rejected`` count the outcomes and ``skipped``
    the matches beyond ``limit``.
    """

    model_config = ConfigDict(extra="ignore")

    root: str
    glob: str
    recursive: bool = True
    dry_run: bool = False
    matched: int = 0
    accepted: int = 0
    duplicate: int = 0
    rejected: int = 0
    skipped: int = 0
    jobs: list[IngestDirJob] = Field(default_factory=list)
    message: str | None = None


__all__ = [
    "IngestAccepted",
    "IngestDirAccepted",
    "IngestDirJob",
    "IngestDirRequest",
    "IngestFileResult",
    "IngestFilesAccepted",
    "IngestRequest",
    "STATUS_ACCEPTED",
    "STATUS_DUPLICATE",
    "STATUS_REJECTED",
]
