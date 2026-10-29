"""Public figure contracts; storage locations never cross the API boundary."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExtractIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


class ExtractOut(BaseModel):
    paper_id: str
    job_id: str
    status: str
    generation_id: str
    status_url: str


class FigureArchivePendingOut(ExtractOut):
    download_url: str


class FigurePart(BaseModel):
    part: int
    page: int
    bbox: list[float] | None = None
    export_bbox: list[float] | None = None
    source_geometry: dict[str, Any] | None = None
    coordinate_space: str = "visible-page-points"
    dpi: float | None = None
    width: int | None = None
    height: int | None = None
    preview_downsampled: bool = False
    preview_url: str | None = None
    thumbnail_url: str | None = None


class FigureOut(BaseModel):
    figure_id: str
    ordinal: int
    label_raw: str | None
    figure_number: str | None
    display_name: str
    caption_text: str
    caption_source: str
    match_status: str
    native_format: str
    native_url: str
    parts: list[FigurePart]
    quality: dict[str, Any]


class FigureListOut(BaseModel):
    paper_id: str
    generation_id: str | None
    status: str
    job_id: str | None
    total: int
    limit: int
    offset: int
    figures: list[FigureOut]
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    last_error: str | None = None


class FigureDeleteOut(BaseModel):
    paper_id: str
    figures_deleted: int
    objects_deleted: int
    cleanup_pending: bool
    dry_run: bool
