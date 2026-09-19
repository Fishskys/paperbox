"""Paper ingestion endpoints (MVP-SPEC section 2 + plan section 3).

Three public ways to get a PDF in, all sharing one state machine
(``ingestion_jobs``) and one queue (``app.workers.queue``):

* ``POST /api/papers/ingest/files`` -- multipart, one or more files (2026-09-19);
  the bytes are streamed into object storage under ``uploads/<request_id>/``
  while a SHA256 is computed, so a duplicate is detected without a second read;
* ``POST /api/papers/ingest/dir`` -- the server reads a directory on its own
  filesystem (whitelisted by ``INGEST_LOCAL_ROOTS``);
* ``POST /api/papers/ingest/compressed`` -- the server unpacks a ZIP archive.

``POST /api/papers/ingest`` (URL) and ``POST /api/papers/ingest/file`` (single
file) stay as they were; the latter is a thin wrapper over ``/ingest/files``.

Every endpoint answers ``202`` with a per-file outcome and never starts a
pipeline itself: the job is parked in ``QUEUED`` and handed to the queue, which
runs at most ``INGEST_CONCURRENCY`` pipelines at once.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.errors import classify_failure
from app.core.logging import get_logger
from app.core.security import require_api_key
from app.db.session import get_db
from app.schemas.ingestion import (
    STATUS_ACCEPTED,
    STATUS_DUPLICATE,
    STATUS_REJECTED,
    IngestAccepted,
    IngestFileResult,
    IngestFilesAccepted,
    IngestRequest,
)
from app.services import ingestion_service as ingest
from app.services import object_storage, paper_service, upload_admission
from app.workers import queue as job_queue

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/papers",
    tags=["ingestion"],
    dependencies=[Depends(require_api_key)],
)


def _busy(reason: str, retry_after: int) -> HTTPException:
    """``429`` with the header the client is expected to honour."""
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"server busy: {reason}; retry after {retry_after}s",
        headers={"Retry-After": str(retry_after)},
    )


def _rejected(
    filename: str, exc: BaseException, *, size_bytes: int | None = None
) -> IngestFileResult:
    """Turn a per-file failure into a result row (never fails the request)."""
    failure = classify_failure(exc)
    logger.warning(
        "upload rejected: %s (%s)",
        filename,
        failure.code,
        extra={"extra_fields": {"error_code": failure.code}},
    )
    return IngestFileResult(
        filename=filename,
        status=STATUS_REJECTED,
        error_code=failure.code,
        error_message=failure.message,
        size_bytes=size_bytes,
    )


def _stage_upload(
    staging_key: str, upload: UploadFile, declared_size: int | None
) -> tuple[object_storage.StoredObject, str, int]:
    """Stream one multipart part into the staging area and hash it.

    Runs in a worker thread (object storage is blocking). The declared size from
    the multipart parser lets MinIO stream the part straight through; when it is
    missing the part is read into memory instead, still bounded by
    ``INGEST_MAX_FILE_MB``.
    """
    metadata = {"kind": "staging"}
    if declared_size is None:
        data = upload.file.read()
        if not data:
            raise ingest.UnsupportedSource("uploaded file is empty")
        ingest.ensure_size(len(data))
        stored = object_storage.upload_bytes(
            staging_key,
            data,
            content_type=ingest.PDF_CONTENT_TYPE,
            metadata=metadata,
        )
        return stored, paper_service.compute_sha256(data), len(data)

    stored = object_storage.upload_stream_hashed(
        staging_key,
        upload.file,
        length=declared_size,
        content_type=ingest.PDF_CONTENT_TYPE,
        metadata=metadata,
    )
    size = stored.size_bytes or declared_size
    if size == 0:
        raise ingest.UnsupportedSource("uploaded file is empty")
    ingest.ensure_size(size)
    return stored, stored.sha256 or "", size


async def stage_and_queue(
    session: Session,
    upload: UploadFile,
    *,
    index: int,
    request_id: str,
    priority: int,
) -> IngestFileResult:
    """Validate, stage, dedupe and queue exactly one uploaded file.

    The part is isolated: any failure (bad type, too large, storage down) is
    reported as a ``rejected`` row for that file while the other parts of the
    same request keep going.
    """
    filename = upload.filename or object_storage.ORIGINAL_FILENAME
    content_type = upload.content_type or ingest.PDF_CONTENT_TYPE
    declared = upload.size if isinstance(upload.size, int) and upload.size >= 0 else None

    try:
        if declared is not None and declared == 0:
            raise ingest.UnsupportedSource("uploaded file is empty")
        if not ingest.is_pdf(filename, content_type):
            raise ingest.UnsupportedSource("only PDF files are supported")
        if declared is not None:
            ingest.ensure_size(declared)
    except ingest.UnsupportedSource as exc:
        return _rejected(filename, exc, size_bytes=declared)

    staging_key = object_storage.build_staging_key(request_id, index, filename)
    try:
        stored, digest, size = await run_in_threadpool(
            _stage_upload, staging_key, upload, declared
        )
    except Exception as exc:  # noqa: BLE001 - one bad part must not fail the batch
        session.rollback()
        return _rejected(filename, exc, size_bytes=declared)

    try:
        existing = ingest.find_existing_paper(session, digest)
        if existing is not None:
            # Same content is already in the library: the staged copy has served
            # its only purpose (hashing) and goes away immediately.
            await run_in_threadpool(_discard_staging, staging_key)
            job = ingest.create_job(
                session,
                source_type="file",
                filename=filename,
                content_type=content_type,
                size_bytes=size,
            )
            ingest.resolve_duplicate(session, existing, job)
            session.commit()
            return IngestFileResult(
                filename=filename,
                status=STATUS_DUPLICATE,
                job_id=job.id,
                paper_id=existing.id,
                size_bytes=size,
            )

        job = ingest.create_job(
            session,
            source_type="file",
            filename=filename,
            content_type=content_type,
            size_bytes=size,
            payload={"object_key": staging_key},
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 - database trouble is per-part too
        session.rollback()
        await run_in_threadpool(_discard_staging, staging_key)
        return _rejected(filename, exc, size_bytes=size)

    job_queue.submit(session, job.id, job_queue.KIND_INGEST, priority)
    return IngestFileResult(
        filename=filename,
        status=STATUS_ACCEPTED,
        job_id=job.id,
        size_bytes=size,
    )


def _discard_staging(staging_key: str) -> None:
    """Best-effort removal of a staging object (the GC is the safety net)."""
    try:
        object_storage.delete_object(staging_key)
    except Exception:  # noqa: BLE001 - the GC retries, the request is fine
        logger.warning("could not delete staging object %s", staging_key)


def summarize(
    request_id: str, results: list[IngestFileResult], *, message: str | None = None
) -> IngestFilesAccepted:
    """Count the per-file outcomes of one batch request."""
    return IngestFilesAccepted(
        request_id=request_id,
        accepted=sum(1 for item in results if item.status == STATUS_ACCEPTED),
        duplicate=sum(1 for item in results if item.status == STATUS_DUPLICATE),
        rejected=sum(1 for item in results if item.status == STATUS_REJECTED),
        results=results,
        message=message,
    )


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
    "/ingest/files",
    response_model=IngestFilesAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_files(
    files: list[UploadFile] = File(...),
    session: Session = Depends(get_db),
) -> IngestFilesAccepted:
    """Queue one or more uploaded PDFs (2026-09-19, plan section 3.1).

    ``files`` may be repeated. One file is *interactive* (a human is waiting, it
    jumps ahead in the queue); two or more is *batch*. The response is per-file:
    a bad part is reported as ``rejected`` and the rest still go through.

    Back-pressure is the server's decision, not the client's: ``429`` with
    ``Retry-After`` means "too many uploads in flight" (``INGEST_UPLOAD_CONCURRENCY``)
    or "the processing backlog is deep enough that batch uploads must wait"
    (``INGEST_QUEUE_HIGH_WATERMARK``, batch only -- a single file is always let
    through). ``422`` means the request itself is out of bounds (too many files)
    and ``413`` that it is too large; neither stages anything.
    """
    uploads = list(files)
    if not uploads:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="at least one file is required",
        )

    max_files = settings.ingest_max_files_per_request
    if len(uploads) > max_files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"too many files: {len(uploads)} exceeds "
                f"INGEST_MAX_FILES_PER_REQUEST={max_files}"
            ),
        )

    request_limit = settings.ingest_max_request_mb * 1024 * 1024
    declared_total = sum(
        upload.size
        for upload in uploads
        if isinstance(upload.size, int) and upload.size > 0
    )
    if declared_total > request_limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"request too large: {declared_total} bytes exceeds "
                f"INGEST_MAX_REQUEST_MB={settings.ingest_max_request_mb}"
            ),
        )

    batch = len(uploads) > 1
    priority = (
        job_queue.PRIORITY_BATCH if batch else job_queue.PRIORITY_INTERACTIVE
    )
    admission = upload_admission.get_admission()
    if batch and admission.should_throttle_batch():
        raise _busy("ingestion backlog", upload_admission.RETRY_AFTER_SECONDS)

    request_id = uuid.uuid4().hex[:16]
    try:
        with admission.slot():
            results = [
                await stage_and_queue(
                    session,
                    upload,
                    index=index,
                    request_id=request_id,
                    priority=priority,
                )
                for index, upload in enumerate(uploads, start=1)
            ]
    except upload_admission.AdmissionRejected as exc:
        raise _busy(exc.reason, exc.retry_after) from exc

    logger.info(
        "ingest/files request finished",
        extra={
            "extra_fields": {
                "request_id": request_id,
                "files": len(results),
                "priority": priority,
            }
        },
    )
    return summarize(
        request_id,
        results,
        message=(
            f"{settings.ingest_max_files_per_request} files / "
            f"{settings.ingest_max_request_mb} MB per request, "
            f"{settings.ingest_max_file_mb} MB per file"
        ),
    )


@router.post(
    "/ingest/file",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_file(
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> IngestAccepted:
    """Queue a single multipart PDF upload.

    A thin wrapper over ``/ingest/files`` (2026-09-19): same streaming write,
    same content dedupe, same queue and the same response shape as before. The
    only difference from the batch endpoint is the error contract -- a single
    file that cannot be accepted is a ``422`` on the request itself, because
    there is no other part to report it next to.
    """
    admission = upload_admission.get_admission()
    request_id = uuid.uuid4().hex[:16]
    try:
        with admission.slot():
            result = await stage_and_queue(
                session,
                file,
                index=1,
                request_id=request_id,
                priority=job_queue.PRIORITY_INTERACTIVE,
            )
    except upload_admission.AdmissionRejected as exc:
        raise _busy(exc.reason, exc.retry_after) from exc

    if result.status == STATUS_REJECTED:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=result.error_message or "upload rejected",
        )

    job = ingest.get_job(session, str(result.job_id)) if result.job_id else None
    if job is None:  # pragma: no cover - the row was just committed
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="job not found after staging",
        )
    return IngestAccepted.model_validate(
        ingest.accepted_payload(
            job,
            message=(
                f"staged upload for ingestion "
                f"({settings.ingest_max_file_mb} MB limit)"
            ),
        )
    )


__all__ = ["router"]
