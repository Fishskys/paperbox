"""Ingestion orchestration: job state machine, validation and payload download.

The MVP runs ingestion inline through FastAPI ``BackgroundTasks`` (no Redis or
Celery). The state machine is:

    RECEIVED -> DOWNLOADING -> STORED -> COMPLETED
                      \\-> FAILED

DUPLICATE is expressed on the job payload as ``duplicate=True`` together with
the ``paper_id`` of the paper that already holds the same content.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import IngestionJob, Paper, new_uuid
from app.services import paper_service

logger = get_logger(__name__)

STAGE_RECEIVED = "RECEIVED"
#: Waiting in the ingestion queue for a free pipeline slot (2026-09-19).
STAGE_QUEUED = "QUEUED"
STAGE_DOWNLOADING = "DOWNLOADING"
STAGE_STORED = "STORED"
STAGE_COMPLETED = "COMPLETED"
STAGE_FAILED = "FAILED"

#: ``payload["source_type"]`` of a PDF that already sits on this machine: the
#: server reads it directly (``/ingest/dir`` and ``/ingest/compressed``).
SOURCE_TYPE_LOCAL = "local_path"

#: Stages a worker sets *while* a pipeline is running. A job found in one of
#: these at startup was interrupted by a process restart (the pipeline lives in
#: the web process), so ``recover_jobs`` fails it with ``INTERRUPTED``.
IN_FLIGHT_STAGES: tuple[str, ...] = (
    "DOWNLOADING",
    "STORED",
    "PARSING",
    "CHUNKING",
    "EMBEDDING",
    "INDEXING",
)

STATUS_DUPLICATE = "DUPLICATE"
STATUS_ACCEPTED = "RECEIVED"

PROGRESS_RECEIVED = 0.0
PROGRESS_QUEUED = 0.0
PROGRESS_DOWNLOADING = 10.0
PROGRESS_STORED = 30.0
PROGRESS_COMPLETED = 100.0

KIND_INGEST = "ingest"
PDF_CONTENT_TYPE = "application/pdf"
OCTET_STREAM = "application/octet-stream"
CHUNK_SIZE = 1024 * 1024
MAX_ERROR_LENGTH = 2000

_PDF_SUFFIX = re.compile(r"\\.pdf$", re.IGNORECASE)


class IngestionError(RuntimeError):
    """Raised when ingestion cannot proceed (message is safe for the job row)."""


class UnsupportedSource(IngestionError):
    """The payload is not an accepted PDF source (maps to HTTP 422)."""


class LocalSourceUnavailable(IngestionError):
    """A ``local_path`` job points at a file the server cannot read any more.

    The file existed when the request was accepted (``/ingest/dir`` scanned it,
    or an archive was unpacked) but is gone or unreadable by the time a pipeline
    slot frees up. Classified as ``DOWNLOAD_FAILED``: the payload could not be
    obtained, exactly like a dead URL.
    """


@dataclass(frozen=True)
class DownloadResult:
    """Bytes pulled from a URL plus the derived filename and content type."""

    data: bytes
    filename: str
    content_type: str | None
    source_url: str


@dataclass(frozen=True)
class UploadResult:
    """Bytes of an uploaded file plus its declared metadata."""

    data: bytes
    filename: str
    content_type: str | None


# --------------------------------------------------------------------------- #
# validation helpers (pure functions -> easy to unit test)
# --------------------------------------------------------------------------- #
def max_file_bytes() -> int:
    """Configured upload/download ceiling in bytes."""
    return settings.ingest_max_file_mb * 1024 * 1024


def is_pdf(filename: str | None, content_type: str | None) -> bool:
    """Accept a payload only for ``application/pdf`` or a ``.pdf`` name."""
    if content_type:
        normalized = content_type.split(";", 1)[0].strip().lower()
        if normalized == PDF_CONTENT_TYPE:
            return True
    return bool(filename and _PDF_SUFFIX.search(filename.strip()))


def ensure_valid_url(source: str) -> str:
    """Validate an ingestion URL, raising :class:`UnsupportedSource` otherwise."""
    candidate = (source or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UnsupportedSource("source must be an http(s) URL")
    return candidate


def ensure_size(size_bytes: int, limit: int | None = None) -> None:
    """Reject payloads larger than the configured ceiling."""
    limit = limit if limit is not None else max_file_bytes()
    if size_bytes > limit:
        raise UnsupportedSource(
            f"file too large: {size_bytes} bytes exceeds {limit} bytes"
        )


def filename_from_url(url: str) -> str:
    """Best-effort filename from a URL path (already URL-decoded)."""
    path = urlparse(url).path or ""
    tail = unquote(path.rstrip("/").rsplit("/", 1)[-1]) if path else ""
    return tail or "download.pdf"


def validate_pdf_payload(
    data: bytes, filename: str | None, content_type: str | None
) -> None:
    """Common checks shared by the URL and the multipart ingestion paths."""
    if not data:
        raise UnsupportedSource("uploaded file is empty")
    if not is_pdf(filename, content_type):
        raise UnsupportedSource("only PDF files are supported")
    ensure_size(len(data))


def title_for_ingest(filename: str | None, source: str | None) -> str:
    """Provisional title: file stem, else the URL tail, else a placeholder."""
    stem = (filename or "").strip()
    stem = _PDF_SUFFIX.sub("", stem).strip()
    if stem:
        return stem
    if source:
        return paper_service.title_from_url(source)
    return "untitled"


# --------------------------------------------------------------------------- #
# job helpers
# --------------------------------------------------------------------------- #
def validate_source_payload(payload: dict) -> None:
    """Reject a job payload the pipeline could never process.

    Only ``local_path`` needs checking at creation time: the path is supplied by
    the client (via ``/ingest/dir``) or by the archive service, and a relative or
    empty path would only blow up much later, inside a worker, where the caller
    can no longer be told. Raises :class:`UnsupportedSource` (HTTP 422).
    """
    source_type = str(payload.get("source_type") or "").lower()
    if source_type != SOURCE_TYPE_LOCAL:
        return
    raw = str(payload.get("local_path") or "").strip()
    if not raw:
        raise UnsupportedSource("local_path is required for a local_path source")
    if not os.path.isabs(raw):
        raise UnsupportedSource(f"local_path must be an absolute path: {raw}")


def create_job(
    session: Session,
    *,
    source_type: str,
    source: str | None = None,
    filename: str | None = None,
    content_type: str | None = None,
    size_bytes: int | None = None,
    payload: dict | None = None,
) -> IngestionJob:
    """Insert a ``RECEIVED`` ingestion job (caller commits).

    ``payload`` carries the source-type specific fields (``object_key`` for a
    staged upload, ``local_path``/``cleanup_after`` for a server-side file); the
    named arguments are the ones every source type shares and win on collision.
    The merged payload is validated before the row is created.
    """
    fields: dict = {"source_type": source_type}
    for key, value in dict(payload or {}).items():
        if key == "source_type":
            continue
        if value is not None:
            fields[key] = value
    if source:
        fields["source"] = source
    if filename:
        fields["filename"] = filename
    if content_type:
        fields["content_type"] = content_type
    if size_bytes is not None:
        fields["size_bytes"] = size_bytes
    validate_source_payload(fields)

    job = IngestionJob(
        id=new_uuid(),
        kind=KIND_INGEST,
        stage=STAGE_RECEIVED,
        progress=PROGRESS_RECEIVED,
        payload=fields,
        started_at=datetime.now(timezone.utc),
    )
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: str) -> IngestionJob | None:
    """Fetch one job by id."""
    return session.execute(
        select(IngestionJob).where(IngestionJob.id == job_id)
    ).scalar_one_or_none()


def list_jobs(
    session: Session, *, limit: int = 20, paper_id: str | None = None
) -> tuple[list[IngestionJob], int]:
    """Return ``(jobs, total)`` newest first, optionally for one paper."""
    from sqlalchemy import func, select as _select

    statement = _select(IngestionJob)
    if paper_id:
        statement = statement.where(IngestionJob.paper_id == paper_id)
    total = session.execute(
        _select(func.count()).select_from(statement.subquery())
    ).scalar_one()
    rows = (
        session.execute(
            statement.order_by(IngestionJob.created_at.desc())
            .limit(max(1, min(limit, 200)))
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


def job_duplicate(job: IngestionJob) -> bool:
    payload = job.payload or {}
    return bool(payload.get("duplicate"))


def serialize_job(job: IngestionJob) -> dict:
    """Shape a job row for :class:`app.schemas.job.JobOut`."""
    return {
        "job_id": job.id,
        "paper_id": job.paper_id,
        "stage": job.stage,
        "progress": float(job.progress or 0.0),
        "duplicate": job_duplicate(job),
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "finished_at": job.finished_at,
    }


def accepted_payload(
    job: IngestionJob,
    *,
    status_value: str = STATUS_ACCEPTED,
    message: str | None = None,
) -> dict:
    """Shape the immediate ingestion response for ``IngestAccepted``."""
    return {
        "job_id": job.id,
        "paper_id": job.paper_id,
        "status": status_value,
        "duplicate": job_duplicate(job),
        "stage": job.stage,
        "created_at": job.created_at,
        "message": message,
    }


def resolve_duplicate(
    session: Session,
    paper: Paper,
    job: IngestionJob,
    message: str = "duplicate content: an identical paper already exists",
) -> IngestionJob:
    """Mark a job as a completed no-op pointing at the existing paper."""
    payload = dict(job.payload or {})
    payload["duplicate"] = True
    job.payload = payload
    job.paper_id = paper.id
    job.stage = STAGE_COMPLETED
    job.progress = PROGRESS_COMPLETED
    job.error_message = None
    job.finished_at = datetime.now(timezone.utc)
    session.flush()
    logger.info(
        "ingest duplicate detected",
        extra={"extra_fields": {"job_id": job.id, "paper_id": paper.id}},
    )
    return job


def mark_failed(
    session: Session,
    job: IngestionJob,
    message: str,
    code: str | None = None,
) -> IngestionJob:
    """Persist a FAILED job (best effort, never raises).

    ``stage``/``progress`` deliberately keep the values of the stage that failed
    (P1 section A1): the client sees *where* the pipeline died, not just that it
    did. ``FAILED`` therefore lives on ``stage`` only for failures raised before
    any stage transition (``RECEIVED``).
    """
    job.stage = STAGE_FAILED
    job.error_code = code
    job.error_message = message[:MAX_ERROR_LENGTH]
    job.finished_at = datetime.now(timezone.utc)
    try:
        session.flush()
    except Exception:  # noqa: BLE001 - failure reporting must not mask the cause
        logger.exception("could not persist job failure")
    return job


def prepare_retry(session: Session, job_id: str) -> IngestionJob | None:
    """Reset a FAILED job so it can be driven again (plan section 22).

    The guarded ``UPDATE ... WHERE stage = 'FAILED'`` is the concurrency gate:
    exactly one caller can flip a given job out of FAILED, so double-triggering
    the retry endpoint cannot start two workers on the same job. Returns the
    refreshed row, or ``None`` when the job was not FAILED (unknown id, still
    running/completed, or the caller lost the race).
    """
    claimed = session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id, IngestionJob.stage == STAGE_FAILED)
        .values(
            stage=STAGE_RECEIVED,
            progress=PROGRESS_RECEIVED,
            error_code=None,
            error_message=None,
            finished_at=None,
            started_at=datetime.now(timezone.utc),
        )
    )
    if claimed.rowcount != 1:
        session.rollback()
        return None
    session.commit()
    return get_job(session, job_id)


def mark_queued(session: Session, job_id: str) -> IngestionJob | None:
    """Park a job in ``QUEUED`` while it waits for a pipeline slot (2026-09-19).

    Guarded ``UPDATE ... WHERE stage IN ('RECEIVED', 'QUEUED')``: a job that has
    already been picked up by a worker (or finished) is left alone, so the queue
    can never rewind a running pipeline. Returns the refreshed row, or ``None``
    when the job was not queueable.
    """
    claimed = session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == job_id,
            IngestionJob.stage.in_((STAGE_RECEIVED, STAGE_QUEUED)),
        )
        .values(stage=STAGE_QUEUED, progress=PROGRESS_QUEUED)
    )
    if claimed.rowcount != 1:
        session.rollback()
        return None
    session.commit()
    return get_job(session, job_id)


def recover_jobs(
    session: Session,
) -> tuple[list[tuple[str, dict]], list[str]]:
    """Find jobs a previous process left behind (called once at startup).

    Returns ``(requeue, interrupted)``:

    * ``requeue`` -- ``(job_id, payload)`` for jobs that never started
      (``RECEIVED``/``QUEUED`` and never finished): they are safe to run again.
    * ``interrupted`` -- ids of jobs that were mid-pipeline when the process
      died. They are marked ``FAILED`` with ``error_code='INTERRUPTED'`` so the
      client sees them instead of a job that hangs forever, and
      ``POST /api/jobs/{id}/retry`` can re-drive them.
    """
    rows = (
        session.execute(
            select(IngestionJob).where(IngestionJob.finished_at.is_(None))
        )
        .scalars()
        .all()
    )
    requeue: list[tuple[str, dict]] = []
    interrupted: list[str] = []
    for job in rows:
        if job.stage in (STAGE_RECEIVED, STAGE_QUEUED):
            requeue.append((job.id, dict(job.payload or {})))
        elif job.stage in IN_FLIGHT_STAGES:
            mark_failed(
                session,
                job,
                "interrupted by a process restart; retry the job to resume",
                code="INTERRUPTED",
            )
            interrupted.append(job.id)
    if interrupted:
        session.commit()
    return requeue, interrupted


# --------------------------------------------------------------------------- #
# payload acquisition
# --------------------------------------------------------------------------- #
def download_pdf(url: str) -> DownloadResult:
    """Download a PDF over HTTP(S) with a streaming size guard."""
    limit = max_file_bytes()
    filename = filename_from_url(url)
    try:
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=settings.ingest_download_timeout,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type")
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit():
                ensure_size(int(declared), limit)
            buffer = io.BytesIO()
            total = 0
            for chunk in response.iter_bytes(CHUNK_SIZE):
                total += len(chunk)
                if total > limit:
                    raise UnsupportedSource(
                        f"file too large: exceeds {settings.ingest_max_file_mb} MB"
                    )
                buffer.write(chunk)
        return DownloadResult(
            data=buffer.getvalue(),
            filename=filename,
            content_type=content_type,
            source_url=url,
        )
    except UnsupportedSource:
        raise
    except httpx.HTTPStatusError as exc:
        raise IngestionError(
            f"download failed with HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise IngestionError(f"download failed: {exc}") from exc


def read_upload(data: bytes, filename: str, content_type: str | None) -> UploadResult:
    """Validate an uploaded file payload."""
    validate_pdf_payload(data, filename, content_type)
    return UploadResult(data=data, filename=filename, content_type=content_type)


__all__ = [
    "DownloadResult",
    "LocalSourceUnavailable",
    "SOURCE_TYPE_LOCAL",
    "IN_FLIGHT_STAGES",
    "IngestionError",
    "STAGE_COMPLETED",
    "STAGE_DOWNLOADING",
    "STAGE_FAILED",
    "STAGE_QUEUED",
    "STAGE_RECEIVED",
    "STAGE_STORED",
    "STATUS_ACCEPTED",
    "STATUS_DUPLICATE",
    "UnsupportedSource",
    "UploadResult",
    "accepted_payload",
    "create_job",
    "download_pdf",
    "ensure_size",
    "ensure_valid_url",
    "filename_from_url",
    "get_job",
    "is_pdf",
    "job_duplicate",
    "list_jobs",
    "mark_failed",
    "mark_queued",
    "max_file_bytes",
    "prepare_retry",
    "read_upload",
    "recover_jobs",
    "resolve_duplicate",
    "serialize_job",
    "title_for_ingest",
    "validate_pdf_payload",
    "validate_source_payload",
]
