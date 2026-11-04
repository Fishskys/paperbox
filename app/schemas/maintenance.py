"""Durable administrative task contracts."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.metadata import ReextractOut


class MaintenanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    operation: Literal["metadata", "consistency", "reindex"]
    dry_run: bool = True
    paper_id: UUID | None = None
    paper_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)
    limit: int = Field(default=20, ge=1, le=2000)
    refresh_index: bool = True
    parser_papers: bool = False
    preview_job_id: UUID | None = None

    @model_validator(mode="after")
    def require_preview(self):
        if self.paper_id and self.paper_ids:
            raise ValueError("use paper_id or paper_ids, not both")
        if self.paper_ids and len(set(self.paper_ids)) != len(self.paper_ids):
            raise ValueError("paper_ids must be unique")
        if self.operation in ("metadata", "reindex") and not self.dry_run and not self.preview_job_id:
            raise ValueError("apply requires a completed preview_job_id")
        if self.operation == "reindex" and not self.paper_ids:
            raise ValueError("reindex requires an explicit bounded paper_ids list")
        if self.operation == "consistency" and (not self.dry_run or self.paper_id or self.paper_ids or self.preview_job_id):
            raise ValueError("consistency is a read-only store-wide scan")
        if self.operation == "consistency" and self.limit > 1000:
            raise ValueError("consistency limit must not exceed 1000")
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


class MaintenanceItemOut(BaseModel):
    paper_id: str
    title: str | None = None
    status: str
    error: str | None = None
    job_id: str | None = None


class MaintenanceReportOut(ReextractOut):
    items: list[MaintenanceItemOut] = Field(default_factory=list)
    queued: int = 0
    skipped_active: int = 0
