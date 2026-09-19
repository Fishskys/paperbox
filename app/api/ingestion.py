"""Paper ingestion endpoints (MVP-SPEC section 2).

Both endpoints create the job row, park it in ``QUEUED`` and hand it to the
in-process ingestion queue (``app.workers.queue``): at most
``INGEST_CONCURRENCY`` pipelines run at once, everything else waits in FIFO
order. The caller polls ``GET /api/jobs/{job_id}`` (or watches
``GET /api/jobs/queue``) for progress.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import require_api_key
from app.db.session import get_db
from app.schemas.ingestion import IngestAccepted, IngestRequest
from app.services import ingestion_service as ingest
from app.services import object_storage
from app.workers import queue as job_queue

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/papers",
    tags=["ingestion"],
    dependencies=[Depends(require_api_key)],
)

UPLOAD_PREFIX = "uploads"


@router.post("/ingest", response_model=IngestAccepted, status_code=status.HTTP_202_ACCEPTED)
def ingest_url(
    payload: IngestRequest,
    session: Session = Depends(get_db),
) -> IngestAccepted:
    """Queue a URL ingestion (job starts as soon as a pipeline slot is free)."""
    try:
        source = ingest.ensure_valid_url(payload.source)
    except ingest.UnsupportedSource as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    job = ingest.create_job(session, source_type="url", source=source)
    session.commit()
    queued = job_queue.submit(session, job.id, job_queue.KIND_INGEST) or job
    return IngestAccepted.model_validate(ingest.accepted_payload(queued))


@router.post(
    "/ingest/file",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_file(
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> IngestAccepted:
    """Queue a multipart PDF upload; the worker stores it and dedupes by SHA256.

    The bytes are staged into MinIO here (so the queue only holds a job id), then
    the job waits for a pipeline slot -- uploads beyond ``INGEST_CONCURRENCY``
    report stage ``QUEUED`` until one frees up.
    """
    raw = await file.read()
    filename = file.filename or object_storage.ORIGINAL_FILENAME
    content_type = file.content_type

    try:
        ingest.validate_pdf_payload(raw, filename, content_type)
    except ingest.UnsupportedSource as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    staging_key = f"{UPLOAD_PREFIX}/{ingest.filename_from_url(filename)}"
    try:
        object_storage.upload_bytes(
            staging_key, raw, content_type=ingest.PDF_CONTENT_TYPE
        )
    except object_storage.ObjectStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="object storage unavailable",
        ) from exc

    job = ingest.create_job(
        session,
        source_type="file",
        filename=filename,
        content_type=content_type,
        size_bytes=len(raw),
    )
    payload = dict(job.payload or {})
    payload["object_key"] = staging_key
    job.payload = payload
    session.commit()

    queued = job_queue.submit(session, job.id, job_queue.KIND_INGEST) or job
    return IngestAccepted.model_validate(
        ingest.accepted_payload(
            queued,
            message=f"staged upload for ingestion ({settings.ingest_max_file_mb} MB limit)",
        )
    )


__all__ = ["router"]
