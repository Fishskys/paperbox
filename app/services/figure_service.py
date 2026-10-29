"""Versioned paper figures: queue, publish, read and delete through one service."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import re
import shutil
import uuid

from sqlalchemy import func, select

from app.core.config import settings
from app.db.models import (
    FigureExtractionRun as Run,
    IngestionJob,
    Paper,
    PaperFigure as Figure,
    PaperFigureAsset as Asset,
)
from app.services import object_storage, paper_service

SOURCE_TYPE = "figures"
ACTIVE = ("queued", "running")
BACKEND_REVISION = "3d7ad46753d4a315cccd1c2bcab398380e88c534"


class FigureError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code = code
        self.status = status


def now():
    return datetime.now(timezone.utc)


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise FigureError("FIGURE_NOT_FOUND", "resource not found", 404) from exc


def paper(session, paper_id, *, lock=False):
    query = select(Paper).where(Paper.id == _uuid(paper_id), Paper.deleted_at.is_(None))
    result = session.scalar(
        (query.with_for_update() if lock else query).execution_options(
            populate_existing=True
        )
    )
    if result is None:
        raise FigureError("PAPER_NOT_FOUND", "paper not found", 404)
    return result


def prefix(run):
    return f"papers/{run.paper_id}/figures/{run.id}/"


def options():
    jar = Path(settings.pdffigures2_jar).resolve()
    java = shutil.which(settings.pdffigures2_java)
    if not jar.is_file() or java is None:
        raise FigureError(
            "FIGURE_BACKEND_UNAVAILABLE",
            "Build PDFFigures2 and configure PDFFIGURES2_JAR and PDFFIGURES2_JAVA",
            503,
        )
    return {
        "jar": str(jar),
        "java": java,
        "backend_version": BACKEND_REVISION
        + ":"
        + hashlib.sha256(jar.read_bytes()).hexdigest(),
        "exporter_version": version("pymupdf"),
        "schema_version": 1,
        "timeout": settings.figure_extract_timeout,
        "dpi": settings.figure_render_dpi,
        "max_dpi": settings.figure_render_max_dpi,
        "max_pixels": settings.figure_max_pixels,
        "thumbnail_edge": settings.figure_thumbnail_max_edge,
        "max_figures": settings.figure_max_per_paper,
        "max_temp_bytes": settings.figure_max_temp_bytes,
    }


def request_extraction(session, paper_id, *, force=False, retry_job=None):
    from app.services import ingestion_service as jobs
    from app.workers import queue

    record = paper(session, paper_id, lock=True)
    source = paper_service.original_file(record)
    if source is None:
        raise FigureError("FIGURE_PDF_REQUIRED", "paper has no original PDF")
    active = session.scalar(
        select(Run).where(Run.paper_id == record.id, Run.status.in_(ACTIVE))
    )
    if active:
        if retry_job:
            raise FigureError("FIGURE_JOB_ACTIVE", "another extraction is active")
        return {
            "paper_id": record.id,
            "job_id": active.job_id,
            "status": active.status,
            "generation_id": active.id,
            "status_url": f"/api/jobs/{active.job_id}",
        }, 202
    configured = options()
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in configured.items()
                if key not in ("jar", "java")
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    current = session.scalar(
        select(Run).where(Run.paper_id == record.id, Run.is_current.is_(True))
    )
    if (
        not force
        and current
        and current.options_hash == fingerprint
        and current.source_file_id == source.id
        and current.source_sha256 == source.sha256
    ):
        return {
            "paper_id": record.id,
            "job_id": current.job_id,
            "status": current.status,
            "generation_id": current.id,
            "status_url": f"/api/papers/{record.id}/figures",
        }, 200
    if not queue.get_queue().started:
        raise FigureError("FIGURE_QUEUE_UNAVAILABLE", "job queue is not running", 503)
    if retry_job is not None and retry_job.stage != jobs.STAGE_FAILED:
        raise FigureError("FIGURE_NOT_RETRYABLE", "only failed jobs can be retried")
    job = retry_job or IngestionJob(
        id=str(uuid.uuid4()), paper_id=record.id, kind=SOURCE_TYPE
    )
    job.stage, job.progress = jobs.STAGE_RECEIVED, 0
    job.error_code = job.error_message = None
    job.started_at = job.finished_at = None
    run = Run(
        id=str(uuid.uuid4()),
        paper_id=record.id,
        job_id=job.id,
        source_file_id=source.id,
        source_sha256=source.sha256,
        options_hash=fingerprint,
        options=configured,
        bucket=source.bucket,
        status="queued",
        expected_revision=record.figure_revision,
    )
    job.payload = {"source_type": SOURCE_TYPE, "run_id": run.id}
    session.add(job)
    session.flush()
    session.add(run)
    job.stage = jobs.STAGE_QUEUED
    session.commit()
    if not queue.get_queue().enqueue(
        job.id, queue.KIND_FIGURES, queue.PRIORITY_BATCH, allow_inline=False
    ):
        run.status, run.finished_at = "failed", now()
        jobs.mark_failed(
            session,
            job,
            "figure queue stopped before submission",
            code="QUEUE_UNAVAILABLE",
        )
        session.commit()
        raise FigureError(
            "FIGURE_QUEUE_UNAVAILABLE", "job queue stopped before submission", 503
        )
    return {
        "paper_id": record.id,
        "job_id": job.id,
        "status": "queued",
        "generation_id": run.id,
        "status_url": f"/api/jobs/{job.id}",
    }, 202


def assets_by_figure(session, identifiers):
    """Load assets for API and archive serialization."""
    result = defaultdict(list)
    if identifiers:
        for asset in session.scalars(
            select(Asset).where(Asset.figure_id.in_(identifiers))
        ):
            result[asset.figure_id].append(asset)
    return result


def serialize_figure(figure, assets):
    """Return public figure metadata without storage locations."""
    base = f"/api/papers/{figure.paper_id}/figures/{figure.id}/file"
    parts = []
    for original in figure.parts:
        part = dict(original)
        for role in ("preview", "thumbnail"):
            part[role + "_url"] = (
                f"{base}?variant={role}&part={part['part']}"
                if any(
                    asset.role == role and asset.part_index == part["part"]
                    for asset in assets
                )
                else None
            )
        parts.append(part)
    label = re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        figure.figure_number or f"unlabeled-p{parts[0]['page']}-{figure.ordinal}",
    )
    return {
        "figure_id": figure.id,
        "ordinal": figure.ordinal,
        "label_raw": figure.label_raw,
        "figure_number": figure.figure_number,
        "display_name": "figure-" + label,
        "caption_text": figure.caption_text,
        "caption_source": "pdffigures2",
        "match_status": figure.match_status,
        "native_format": "pdf",
        "native_url": base + "?variant=native",
        "parts": parts,
        "quality": {
            "native": "source_pdf_crop",
            "preview": "rasterized",
            "redacted": False,
        },
    }


def list_figures(session, paper_id, *, limit=20, offset=0, generation_id=None):
    record = paper(session, paper_id)
    current = session.scalar(
        select(Run).where(Run.paper_id == record.id, Run.is_current.is_(True))
    )
    if generation_id and (current is None or current.id != generation_id):
        raise FigureError(
            "FIGURE_GENERATION_CHANGED", "the figure generation has changed"
        )
    active = session.scalar(
        select(Run).where(Run.paper_id == record.id, Run.status.in_(ACTIVE))
    )
    latest = session.scalar(
        select(Run)
        .where(Run.paper_id == record.id)
        .order_by(Run.created_at.desc(), Run.id.desc())
    )
    figures, total = [], 0
    if current:
        query = select(Figure).where(
            Figure.run_id == current.id, Figure.deleted_at.is_(None)
        )
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        rows = list(
            session.scalars(query.order_by(Figure.ordinal).limit(limit).offset(offset))
        )
        assets = assets_by_figure(session, [figure.id for figure in rows])
        figures = [serialize_figure(figure, assets[figure.id]) for figure in rows]
    state = (
        current.status
        if current
        else (active or latest).status
        if active or latest
        else "not_extracted"
    )
    if (
        not current
        and not active
        and latest
        and latest.status in ("completed", "partial")
    ):
        state = "deleted"
    source = paper_service.original_file(record)
    if current and (
        source is None
        or current.source_file_id != source.id
        or source.sha256
        and source.sha256 != current.source_sha256
    ):
        state = "stale"
    return {
        "paper_id": record.id,
        "generation_id": current.id if current else None,
        "status": state,
        "job_id": active.job_id if active else None,
        "total": total,
        "limit": limit,
        "offset": offset,
        "figures": figures,
        "warnings": current.warnings if current else [],
        "last_error": latest.error_message
        if latest and latest.status == "failed"
        else None,
    }


def get_figure(session, paper_id, figure_id):
    paper(session, paper_id)
    figure = session.scalar(
        select(Figure)
        .join(Run, Figure.run_id == Run.id)
        .where(
            Figure.id == _uuid(figure_id),
            Figure.paper_id == paper_id,
            Figure.deleted_at.is_(None),
            Run.is_current.is_(True),
        )
    )
    if figure is None:
        raise FigureError("FIGURE_NOT_FOUND", "figure not found", 404)
    return figure


def detail(session, paper_id, figure_id):
    figure = get_figure(session, paper_id, figure_id)
    return serialize_figure(figure, assets_by_figure(session, [figure.id])[figure.id])


def download(session, paper_id, figure_id, *, variant="preview", part=1):
    figure = get_figure(session, paper_id, figure_id)
    run = session.get(Run, figure.run_id)
    index = 0 if variant == "native" else part
    asset = session.scalar(
        select(Asset).where(
            Asset.figure_id == figure.id,
            Asset.role == variant,
            Asset.part_index == index,
        )
    )
    if asset is None:
        raise FigureError(
            "FIGURE_ASSET_NOT_FOUND", "requested figure asset is unavailable", 404
        )
    label = serialize_figure(figure, [])["display_name"]
    return {
        "key": asset.object_key,
        "bucket": run.bucket,
        "mime_type": asset.mime_type,
        "size_bytes": asset.size_bytes,
        "sha256": asset.sha256,
        "filename": label
        + (".pdf" if variant == "native" else f"-{variant}-{index}.png"),
    }


def _cancel(session, runs):
    from app.workers.figure_tasks import cancel

    for run in runs:
        if run.status in ACTIVE:
            run.status = "cancelled"
            run.cleanup_pending = True
            if run.started_at is None:
                run.finished_at = now()
            job = session.get(IngestionJob, run.job_id) if run.job_id else None
            if job:
                job.stage, job.error_code = "FAILED", "FIGURE_CANCELLED"
                job.error_message, job.finished_at = (
                    "figure extraction cancelled",
                    now(),
                )
            cancel(run.id)


def cleanup(session, *, store=object_storage, run_ids=None, limit=20):
    query = select(Run).where(
        Run.cleanup_pending.is_(True),
        Run.is_current.is_(False),
        Run.finished_at.is_not(None),
    )
    if run_ids is not None:
        query = query.where(Run.id.in_(run_ids))
    removed = 0
    for run in session.scalars(query.limit(limit)):
        try:
            for item in store.list_objects(prefix=prefix(run), bucket=run.bucket):
                store.delete_object(item.object_name, bucket=run.bucket)
                removed += 1
        except Exception:
            continue
        run.cleanup_pending = False
        for figure in session.scalars(
            select(Figure).where(Figure.run_id == run.id, Figure.deleted_at.is_(None))
        ):
            figure.deleted_at = now()
        for figure in session.scalars(select(Figure).where(Figure.run_id == run.id)):
            figure.cleanup_pending = False
    singles = (
        select(Figure)
        .join(Run, Figure.run_id == Run.id)
        .where(
            Figure.cleanup_pending.is_(True),
            Figure.deleted_at.is_not(None),
            Run.is_current.is_(True),
        )
    )
    if run_ids is not None:
        singles = singles.where(Figure.run_id.in_(run_ids))
    for figure in session.scalars(singles.limit(limit)):
        run = session.get(Run, figure.run_id)
        try:
            for item in store.list_objects(
                prefix=prefix(run) + figure.id + "/", bucket=run.bucket
            ):
                store.delete_object(item.object_name, bucket=run.bucket)
                removed += 1
        except Exception:
            continue
        figure.cleanup_pending = False
    session.commit()
    return removed


def delete_figures(
    session, paper_id, *, figure_id=None, dry_run=False, store=object_storage
):
    record = paper(session, paper_id, lock=True)
    runs = list(session.scalars(select(Run).where(Run.paper_id == record.id)))
    query = select(Figure).where(Figure.paper_id == record.id)
    if figure_id:
        query = query.where(Figure.id == _uuid(figure_id))
    figures = list(session.scalars(query))
    if figure_id and (
        not figures
        or not any(run.is_current and run.id == figures[0].run_id for run in runs)
    ):
        raise FigureError("FIGURE_NOT_FOUND", "figure not found", 404)
    visible = [figure for figure in figures if figure.deleted_at is None]
    if dry_run:
        return {
            "paper_id": record.id,
            "figures_deleted": len(visible),
            "objects_deleted": 0,
            "cleanup_pending": any(run.cleanup_pending for run in runs),
            "dry_run": True,
        }
    record.figure_revision += 1
    _cancel(session, runs)
    for figure in visible:
        figure.deleted_at = now()
        figure.cleanup_pending = True
    if not figure_id:
        for run in runs:
            run.is_current, run.cleanup_pending = False, True
            if run.started_at is None:
                run.finished_at = now()
    session.commit()
    removed = cleanup(session, store=store, run_ids=[run.id for run in runs])
    pending = any(run.cleanup_pending for run in runs) or any(
        figure.cleanup_pending for figure in figures
    )
    return {
        "paper_id": record.id,
        "figures_deleted": len(visible),
        "objects_deleted": removed,
        "cleanup_pending": pending,
        "dry_run": False,
    }


def invalidate_for_purge(session, record):
    record = paper(session, record.id, lock=True)
    record.figure_revision += 1
    _cancel(
        session,
        list(
            session.scalars(
                select(Run).where(Run.paper_id == record.id, Run.status.in_(ACTIVE))
            )
        ),
    )
    session.commit()


def check_run(session, run_id):
    run = session.get(Run, run_id)
    if run is None:
        raise FigureError("FIGURE_CANCELLED", "extraction no longer exists")
    record = paper(session, run.paper_id, lock=True)
    session.refresh(run)
    session.expire(record, ["files"])
    source = paper_service.original_file(record)
    if run.status not in ACTIVE or record.figure_revision != run.expected_revision:
        raise FigureError("FIGURE_CANCELLED", "extraction was cancelled or superseded")
    if (
        source is None
        or source.id != run.source_file_id
        or (source.sha256 and run.source_sha256 and source.sha256 != run.source_sha256)
    ):
        raise FigureError(
            "FIGURE_SOURCE_CHANGED", "paper PDF changed during extraction"
        )
    return record, source


def fail(session, run_id, code, message):
    run = session.get(Run, run_id)
    if run is None:
        return
    if run.is_current:
        return
    if run.status != "cancelled":
        run.status, run.error_message = "failed", message[:2000]
        job = session.get(IngestionJob, run.job_id)
        if job:
            from app.services.ingestion_service import mark_failed

            mark_failed(session, job, message, code=code)
    run.finished_at, run.cleanup_pending = now(), True
    session.commit()


def publish(session, run_id, result, directory, *, store=object_storage):
    record, source = check_run(session, run_id)
    run = session.get(Run, run_id)
    if source.sha256 and source.sha256 != result["source_sha256"]:
        raise FigureError(
            "FIGURE_SOURCE_CHANGED", "stored PDF checksum differs from metadata"
        )
    run.source_sha256 = result["source_sha256"]
    session.commit()
    total = 0
    for figure in result["figures"]:
        _uuid(figure["id"])
        for asset in figure["assets"]:
            check_run(session, run_id)
            session.rollback()
            path = (directory / asset["path"]).resolve()
            if not path.is_relative_to((directory / figure["id"]).resolve()):
                raise ValueError("invalid figure asset path")
            total += path.stat().st_size
            if total > run.options["max_temp_bytes"]:
                raise ValueError("figure assets exceed byte budget")
            if (
                path.stat().st_size != asset["size_bytes"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]
            ):
                raise ValueError("figure asset checksum mismatch")
            with path.open("rb") as content:
                store.upload_file(
                    prefix(run) + asset["path"],
                    content,
                    length=asset["size_bytes"],
                    content_type=asset["mime_type"],
                    bucket=run.bucket,
                )
    store.upload_bytes(
        prefix(run) + "manifest.json",
        json.dumps(result, ensure_ascii=False).encode(),
        content_type="application/json",
        bucket=run.bucket,
    )
    record, source = check_run(session, run_id)
    previous = list(
        session.scalars(
            select(Run).where(Run.paper_id == record.id, Run.is_current.is_(True))
        )
    )
    for old in previous:
        old.is_current, old.cleanup_pending = False, True
    session.flush()
    for ordinal, item in enumerate(result["figures"], 1):
        figure = Figure(
            id=item["id"],
            run_id=run.id,
            paper_id=record.id,
            ordinal=ordinal,
            label_raw=item["label_raw"],
            figure_number=item["figure_number"],
            caption_text=item["caption_text"],
            parts=item["parts"],
            match_status=item["match_status"],
        )
        session.add(figure)
        session.flush()
        for asset in item["assets"]:
            session.add(
                Asset(
                    figure_id=figure.id,
                    role=asset["role"],
                    part_index=asset["part"],
                    object_key=prefix(run) + asset["path"],
                    mime_type=asset["mime_type"],
                    sha256=asset["sha256"],
                    size_bytes=asset["size_bytes"],
                    width=asset["width"],
                    height=asset["height"],
                    dpi=asset["dpi"],
                )
            )
    run.status, run.is_current, run.finished_at = result["status"], True, now()
    run.warnings = result["warnings"]
    job = session.get(IngestionJob, run.job_id)
    job.stage, job.progress, job.finished_at = "COMPLETED", 100, now()
    session.commit()
    cleanup(session, store=store, run_ids=[old.id for old in previous])


def recover(session):
    for run in session.scalars(
        select(Run).where(Run.status.in_(("queued", "running", "cancelled")))
    ):
        job = session.get(IngestionJob, run.job_id) if run.job_id else None
        if run.status == "cancelled" or (job and job.stage == "FAILED"):
            run.finished_at, run.cleanup_pending = now(), True
            if run.status != "cancelled":
                run.status, run.error_message = (
                    "failed",
                    "extraction interrupted by process restart",
                )
    session.commit()
    cleanup(session)
