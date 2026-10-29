"""Paper figure browsing and background extraction."""

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.core.security import require_read, require_write
from app.db.session import get_db
from app.schemas.figure import (
    ExtractIn,
    ExtractOut,
    FigureOut,
    FigureListOut,
    FigureDeleteOut,
    FigureArchivePendingOut,
)
from app.services import figure_service as figures, figure_archive, object_storage

router = APIRouter(
    prefix="/api/papers/{paper_id}/figures",
    tags=["figures"],
    dependencies=[Depends(require_read)],
)


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except figures.FigureError as exc:
        raise HTTPException(
            exc.status, detail={"code": exc.code, "message": str(exc)}
        ) from exc


@router.get("", response_model=FigureListOut)
def list_figures(
    paper_id: str,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    generation_id: str | None = None,
    session: Session = Depends(get_db),
):
    return _call(
        figures.list_figures,
        session,
        paper_id,
        limit=limit,
        offset=offset,
        generation_id=generation_id,
    )


@router.post(
    "/extract",
    response_model=ExtractOut,
    status_code=202,
    responses={200: {"model": ExtractOut}},
    dependencies=[Depends(require_write)],
)
def extract(
    paper_id: str, body: ExtractIn = ExtractIn(), session: Session = Depends(get_db)
):
    result, code = _call(
        figures.request_extraction, session, paper_id, force=body.force
    )
    return JSONResponse(result, status_code=code)


@router.delete(
    "", response_model=FigureDeleteOut, dependencies=[Depends(require_write)]
)
def delete_all(
    paper_id: str, dry_run: bool = False, session: Session = Depends(get_db)
):
    return _call(figures.delete_figures, session, paper_id, dry_run=dry_run)


@router.get(
    "/download",
    response_class=StreamingResponse,
    responses={
        202: {
            "model": FigureArchivePendingOut,
            "description": "Extraction queued or running; retry download_url when complete",
        },
        200: {
            "content": {
                "application/zip": {"schema": {"type": "string", "format": "binary"}}
            }
        },
    },
)
def download_archive(
    paper_id: str,
    request: Request,
    variant: Literal["native", "preview", "thumbnail", "all"] = "native",
    generation_id: str | None = None,
    session: Session = Depends(get_db),
):
    try:
        archive = _call(
            figure_archive.prepare,
            session,
            paper_id,
            variant=variant,
            generation_id=generation_id,
        )
    except HTTPException as exc:
        if (
            not isinstance(exc.detail, dict)
            or exc.detail.get("code") != "FIGURES_NOT_EXTRACTED"
        ):
            raise
        require_write(request)
        archive = _call(
            figure_archive.prepare,
            session,
            paper_id,
            variant=variant,
            generation_id=generation_id,
            extract_if_missing=True,
        )
    if isinstance(archive, dict):
        pending = FigureArchivePendingOut.model_validate(archive)
        return JSONResponse(
            pending.model_dump(),
            status_code=202,
            headers={
                "Location": pending.status_url,
                "Retry-After": "2",
                "Cache-Control": "private, no-store",
            },
        )

    def stream():
        try:
            while block := archive.content.read(64 * 1024):
                yield block
        finally:
            archive.content.close()

    return StreamingResponse(
        stream(),
        media_type="application/zip",
        headers={
            "Content-Length": str(archive.size_bytes),
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(archive.filename)}",
            "Cache-Control": "private, no-store",
            "X-Paperbox-Figure-Generation": archive.generation_id,
        },
        background=BackgroundTask(archive.content.close),
    )


@router.get("/{figure_id}", response_model=FigureOut)
def detail(paper_id: str, figure_id: str, session: Session = Depends(get_db)):
    return _call(figures.detail, session, paper_id, figure_id)


@router.delete(
    "/{figure_id}",
    response_model=FigureDeleteOut,
    dependencies=[Depends(require_write)],
)
def delete_one(
    paper_id: str,
    figure_id: str,
    dry_run: bool = False,
    session: Session = Depends(get_db),
):
    return _call(
        figures.delete_figures, session, paper_id, figure_id=figure_id, dry_run=dry_run
    )


@router.get("/{figure_id}/file")
def download(
    paper_id: str,
    figure_id: str,
    request: Request,
    variant: Literal["native", "preview", "thumbnail"] = "preview",
    part: int = Query(1, ge=1),
    disposition: Literal["inline", "attachment"] = "inline",
    session: Session = Depends(get_db),
):
    asset = _call(
        figures.download, session, paper_id, figure_id, variant=variant, part=part
    )
    try:
        object_storage.stat_object(asset["key"], bucket=asset["bucket"])
    except object_storage.ObjectNotFound as exc:
        raise HTTPException(404, "figure asset not found") from exc
    except object_storage.ObjectStorageError as exc:
        raise HTTPException(503, "object storage unavailable") from exc
    etag = '"' + asset["sha256"] + '"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    if any(
        value.strip().removeprefix("W/") in (etag, "*")
        for value in request.headers.get("if-none-match", "").split(",")
    ):
        return Response(status_code=304, headers=headers)
    headers.update(
        {
            "Content-Length": str(asset["size_bytes"]),
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(asset['filename'])}",
        }
    )

    def stream():
        with object_storage.open_stream(
            asset["key"], bucket=asset["bucket"]
        ) as content:
            while block := content.read(64 * 1024):
                yield block

    return StreamingResponse(stream(), media_type=asset["mime_type"], headers=headers)
