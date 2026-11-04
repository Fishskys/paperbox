"""Durable administrative task contracts."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MaintenanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    operation: Literal["metadata", "consistency"]
    dry_run: bool = True
    paper_id: UUID | None = None
    limit: int = Field(default=20, ge=1, le=2000)
    refresh_index: bool = True
    parser_papers: bool = False
    preview_job_id: UUID | None = None

    @model_validator(mode="after")
    def require_preview(self):
        if self.operation == "metadata" and not self.dry_run and not self.preview_job_id:
            raise ValueError("metadata apply requires a completed preview_job_id")
        if self.operation == "consistency" and (not self.dry_run or self.paper_id or self.preview_job_id):
            raise ValueError("consistency is a read-only store-wide scan")
        return self


class MaintenanceOut(BaseModel):
    job_id: str
    operation: str
    dry_run: bool
    stage: str
    progress: float
    report_available: bool
    error_code: str | None = None
    error_message: str | None = None
