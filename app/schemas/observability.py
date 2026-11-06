"""Administrator runtime measurements and bounded application log views."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator


class MemoryOut(BaseModel):
    rss_bytes: int | None = None
    pss_bytes: int | None = None
    processes: int = 0
    partial: bool = False
    source: Literal["backend", "container", "unavailable"] = "unavailable"
    container: str | None = None
    reason: str | None = None


class RuntimeOut(BaseModel):
    measured_at: datetime
    backend: MemoryOut
    dependencies: dict[str, MemoryOut]


class LogEntryOut(BaseModel):
    id: str
    timestamp: datetime
    level: str
    logger: str
    request_id: str | None = None
    key_prefix: str | None = None
    message: str


class LogsOut(BaseModel):
    entries: list[LogEntryOut] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 50
    available: bool = True
    reason: str | None = None
    truncated: bool = False
    scanned_bytes: int = 0
    scan_limit_bytes: int
    measured_at: datetime


class LogFilters(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None
    logger: str = Field(default="", max_length=128)
    q: str = Field(default="", max_length=200)
    request_id: str = Field(default="", max_length=128)
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    offset: int = Field(default=0, ge=0, le=1000000)
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.since and self.until and self.since > self.until:
            raise ValueError("开始时间不能晚于结束时间")
        return self
