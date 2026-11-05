"""Bounded administrator cleanup requests and persisted report contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["app_logs", "search_logs", "jobs", "parse_cache", "figure_history", "orphan_objects", "deleted_papers", "indices"]


class CleanupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    operation: Literal["inventory", "cleanup"] = "cleanup"
    categories: list[Category] = Field(min_length=1, max_length=8)
    dry_run: bool = True
    paper_ids: list[UUID] = Field(default_factory=list, max_length=100)
    indices: list[str] = Field(default_factory=list, max_length=20)
    preview_job_id: UUID | None = None
    retry_job_id: UUID | None = None

    @model_validator(mode="after")
    def bounded_scope(self):
        if any(len(values) != len(set(values)) for values in (self.categories, self.paper_ids, self.indices)):
            raise ValueError("duplicate selections are not allowed")
        if self.operation == "inventory" and (not self.dry_run or self.preview_job_id or self.retry_job_id):
            raise ValueError("inventory is read-only")
        if not self.dry_run and not (self.preview_job_id or self.retry_job_id):
            raise ValueError("apply requires a completed preview or a failed execution")
        if self.preview_job_id and self.retry_job_id:
            raise ValueError("choose preview or failed execution, not both")
        if self.dry_run and (self.preview_job_id or self.retry_job_id):
            raise ValueError("preview must not reference an earlier execution")
        if set(self.categories) & {"deleted_papers", "indices"} and len(self.categories) != 1:
            raise ValueError("permanent deletion and index deletion require separate previews")
        if self.operation == "cleanup" and "deleted_papers" in self.categories and not self.paper_ids:
            raise ValueError("permanent deletion requires explicit paper_ids")
        if self.paper_ids and "deleted_papers" not in self.categories:
            raise ValueError("paper_ids are only used for permanent deletion")
        if self.operation == "cleanup" and "indices" in self.categories and not self.indices:
            raise ValueError("index deletion requires explicit index names")
        if self.indices and "indices" not in self.categories:
            raise ValueError("indices are only used for index cleanup")
        for name in self.indices:
            validate_index_name(name)
        return self


def validate_index_name(name: str) -> str:
    if not name or name in (".", "..") or name[0] in "_-+" or name != name.lower() or len(name.encode()) > 255 or any(c.isspace() or c in '\\/*?"<>|,#:' for c in name):
        raise ValueError("an explicit physical index name is required")
    return name


class IndexClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    is_rollback: bool = False

    @model_validator(mode="after")
    def name_is_explicit(self):
        validate_index_name(self.name)
        return self


class CleanupItemOut(BaseModel):
    id: str
    category: str
    resource_key: str
    size_bytes: int | None = None
    state: str
    error: str | None = None
    detail: dict = Field(default_factory=dict)


class CleanupReportOut(BaseModel):
    job_id: str
    operation: str
    dry_run: bool
    expires_at: datetime | None = None
    total: int
    ready: int
    protected: int
    completed: int
    skipped: int
    failed: int
    size_bytes: int | None = None
    limited: bool = False
    errors: list[str] = Field(default_factory=list)
    items: list[CleanupItemOut] = Field(default_factory=list)


class CleanupListOut(BaseModel):
    total: int
    jobs: list[dict]
