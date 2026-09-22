"""Ingestion job status endpoint (MVP-SPEC section 2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import require_api_key
from app.db.session import get_db
from app.schemas.job import JobListOut, JobOut, QueueOut
from app.services import ingestion_service
from app.workers import queue as job_queue

router = APIRouter(
    prefix="/api/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/queue", response_model=QueueOut)
def get_queue() -> QueueOut:
    """Depth of the in-process ingestion queue (2026-09-19).

    ``concurrency`` is the ceiling (``INGEST_CONCURRENCY``), ``running`` the
    pipelines currently executing and ``queued`` the uploads waiting for a free
    slot. Declared before ``/{job_id}`` on purpose: the path parameter would
    otherwise swallow ``queue``.
    """
    return QueueOut.model_validate(job_queue.stats())


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, session: Session = Depends(get_db)) -> JobOut:
    """Return the current stage/progress of one ingestion job."""
    job = ingestion_service.get_job(session, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="job not found"
        )
    return JobOut.model_validate(ingestion_service.serialize_job(job))


@router.post(
    "/{job_id}/retry",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_job(
    job_id: str,
    session: Session = Depends(get_db),
) -> JobOut:
    """Re-drive a FAILED job (plan section 22).

    Jobs that failed after the STORED checkpoint resume with PARSING -> INDEXING
    on top of the surviving paper row and stored original; jobs that failed
    before it redo the download/store transaction from the job payload. The
    response is the reset job (``QUEUED``); poll ``GET /api/jobs/{id}`` as with
    any ingestion.
    """
    if ingestion_service.get_job(session, job_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="job not found"
        )
    prepared = ingestion_service.prepare_retry(session, job_id)
    if prepared is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only FAILED jobs can be retried",
        )
    queued = job_queue.submit(session, job_id, job_queue.KIND_RETRY) or prepared
    return JobOut.model_validate(ingestion_service.serialize_job(queued))


@router.get("", response_model=JobListOut)
def list_jobs(
    limit: int = Query(ingestion_service.DEFAULT_JOB_LIMIT, ge=1, le=200),
    offset: int = Query(0, ge=0, description="rows to skip (newest first)"),
    stage: str | None = Query(
        default=None, description="only jobs observed in this stage"
    ),
    paper_id: str | None = Query(default=None),
    session: Session = Depends(get_db),
) -> JobListOut:
    """List recent jobs newest first, pageable and filterable (2026-09-23).

    ``limit`` + ``offset`` walk the history server-side (the WebUI's job tab pages
    with them) and ``total`` always counts the *filtered* set, so the browser can
    render "第 x / y 页" from one response. An unknown ``stage`` is a 422: a typo
    must not look like "no such jobs".
    """
    if stage is not None and stage not in ingestion_service.STAGES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"unknown stage: {stage} "
                f"(expected one of {', '.join(ingestion_service.STAGES)})"
            ),
        )
    rows, total = ingestion_service.list_jobs(
        session, limit=limit, offset=offset, stage=stage, paper_id=paper_id
    )
    return JobListOut(
        total=total,
        limit=limit,
        offset=offset,
        stage=stage,
        jobs=[JobOut.model_validate(ingestion_service.serialize_job(row)) for row in rows],
    )


__all__ = ["router"]
