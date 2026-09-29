"""Metadata endpoints: import, review queue, human attribution, batch apply.

These are the write-side companions of the read-only paper endpoints (section 9 of
``docs/architecture/metadata-architecture.md``). Two rules shape all of them:

* **nothing is written unless the caller says so** -- ``POST /api/metadata/import``
  defaults to ``dry_run=true`` and only an explicit ``apply`` (or ``dry_run=false``)
  touches the database;
* **a human decision is never silently dropped** -- an ambiguous record goes to
  ``GET /api/metadata/review`` and is attached by hand, and a field two sources
  disagree about is reported with both values instead of being resolved by rank.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.security import require_api_key
from app.db.session import get_db
from app.schemas.metadata import (
    ApplyIn,
    ApplyOut,
    AttachIn,
    AttachOut,
    ImportReportOut,
    ReviewOut,
)
from app.services import metadata_import as importer
from app.services import metadata_matcher as matcher
from app.services import metadata_merge as merge
from app.services import metadata_sources as sources
from app.services import paper_service, provenance_service

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/metadata",
    tags=["metadata"],
    dependencies=[Depends(require_api_key)],
)

UNSUPPORTED_MEDIA = "send the records as multipart/form-data (file) or application/json"
SOURCE_NOT_FOUND = "source not found"
PAPER_NOT_FOUND = "paper not found"


def _load_source(session: Session, source_id: str):
    from app.db.models import PaperSource

    source = session.get(PaperSource, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=SOURCE_NOT_FOUND)
    return source


async def _payload_from_request(request: Request) -> Any:
    """Read the records from a multipart upload or a JSON body."""
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="multipart request must carry a 'file' part",
            )
        data = await upload.read()
        try:
            return importer.load_payload(data)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid JSON: {exc}",
            ) from exc
    if "application/json" in content_type:
        try:
            return await request.json()
        except Exception as exc:  # noqa: BLE001 - a malformed body is the client's fault
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid JSON: {exc}",
            ) from exc
    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=UNSUPPORTED_MEDIA
    )


@router.post("/import", response_model=ImportReportOut)
async def import_metadata(
    request: Request,
    dry_run: bool = Query(default=True, description="report only (the default)"),
    apply: bool | None = Query(default=None, description="write the changes (overrides dry_run)"),
    limit: int | None = Query(default=None, ge=1),
    source_type: str = Query(
        default=sources.SOURCE_TYPE_IMPORT_FILE,
        description="how to label the records: import_file (default), ieee_api, manual, ...",
    ),
    session: Session = Depends(get_db),
) -> ImportReportOut:
    """Import external metadata (IEEE raw JSON / CSL-JSON / this project's shape).

    Format is detected from the payload. Nothing is written unless ``apply=true``
    or ``dry_run=false``; re-importing the same record is a no-op either way
    (``UNIQUE(source_type, source_ref)``).
    """
    payload = await _payload_from_request(request)
    effective_apply = bool(apply) if apply is not None else not dry_run
    if source_type not in sources.SOURCE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown source_type: {source_type}",
        )
    try:
        report = importer.import_payload(
            session,
            payload,
            apply=effective_apply,
            source_type=source_type,
            importer="api:/api/metadata/import",
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if effective_apply:
        session.commit()
    logger.info(
        "metadata import",
        extra={
            "extra_fields": {
                "total": report.total,
                "matched": report.matched,
                "created_shell": report.created_shell,
                "ambiguous": report.ambiguous,
                "dry_run": report.dry_run,
            }
        },
    )
    return ImportReportOut.model_validate(report.as_dict())


@router.get("/review", response_model=ReviewOut)
def review_queue(
    match_status: list[str] | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_db),
) -> ReviewOut:
    """Records waiting for a human decision, plus the conflicts already recorded."""
    statuses = tuple(match_status) if match_status else sources.REVIEW_STATUSES
    rows = sources.review_queue(session, statuses=statuses, limit=limit)
    conflicts = provenance_service.recorded_conflicts(session, limit=limit)
    return ReviewOut(
        total=len(rows),
        items=[sources.serialize_source(row) for row in rows],
        conflicts=conflicts,
    )


@router.post("/sources/{source_id}/attach", response_model=AttachOut)
def attach_source(
    source_id: str,
    body: AttachIn,
    session: Session = Depends(get_db),
) -> AttachOut:
    """Attach an unmatched source record to a paper and merge its values in.

    This is the human answer to an ``ambiguous`` record: the values of the stored
    record are replayed through the merge engine (the ``raw`` snapshot is what makes
    that possible without asking the platform again).
    """
    source = _load_source(session, source_id)
    paper = paper_service.get_paper(session, body.paper_id)
    if paper is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PAPER_NOT_FOUND)

    sources.attach_source(session, source, paper, method=matcher.METHOD_MANUAL)
    merged = _replay_source(session, source, paper)
    session.commit()
    return AttachOut(
        source_id=source.id,
        paper_id=paper.id,
        source_type=source.source_type,
        match_status=source.match_status,
        match_method=source.match_method,
        merged_fields=merged,
    )


def _replay_source(session: Session, source, paper) -> list[str]:
    """Merge a stored source record's ``raw`` payload into a paper."""
    from app.services import metadata_identifiers as identifiers

    try:
        parsed = importer.parse_records([source.raw])[0]
    except Exception as exc:  # noqa: BLE001 - a raw payload we cannot read is not fatal
        logger.warning("cannot replay source %s: %s", source.id, exc)
        return []
    report = merge.merge_values(
        session,
        paper,
        parsed.values,
        source_type=source.source_type,
        source_id=source.id,
        confidence=source.match_confidence or 1.0,
    )
    identifiers.refresh_primary(session, paper.id)
    identifiers.mirror_legacy_columns(session, paper)
    identifiers.upgrade_fingerprint(session, paper, sha256=parsed.sha256)
    return [item.field for item in report.applied]


@router.post("/apply", response_model=ApplyOut)
def apply_decisions(
    body: ApplyIn,
    session: Session = Depends(get_db),
) -> ApplyOut:
    """Apply decisions from a previous report (which source belongs to which paper).

    ``mode="overwrite"`` re-states the record's values as a manual decision for the
    listed ``fields`` (or all of them), which is the escape hatch from rule R2 when
    a human has decided that a source wins.
    """
    from app.services import metadata_manual

    applied = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    for entry in body.entries:
        source = None
        for source_type in (
            [entry.source_type] if entry.source_type else list(sources.SOURCE_TYPES)
        ):
            source = sources.find_source(session, source_type, entry.source_ref)
            if source is not None:
                break
        if source is None:
            skipped += 1
            errors.append({"source_ref": entry.source_ref, "error": SOURCE_NOT_FOUND})
            continue
        paper = paper_service.get_paper(session, entry.paper_id)
        if paper is None:
            skipped += 1
            errors.append({"source_ref": entry.source_ref, "error": PAPER_NOT_FOUND})
            continue
        sources.attach_source(session, source, paper, method=matcher.METHOD_MANUAL)
        if body.mode == "overwrite":
            try:
                parsed = importer.parse_records([source.raw])[0]
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                errors.append({"source_ref": entry.source_ref, "error": str(exc)})
                continue
            wanted = body.fields or [field for field in parsed.values]
            payload = {
                _patch_key(field): parsed.values[field]
                for field in wanted
                if field in parsed.values and _patch_key(field) is not None
            }
            if payload:
                metadata_manual.patch_metadata(session, paper, payload)
        else:
            _replay_source(session, source, paper)
        applied += 1
    session.commit()
    return ApplyOut(applied=applied, skipped=skipped, errors=errors)


def _patch_key(field: str) -> str | None:
    """Map a claim field back onto the ``PATCH`` key a human would use."""
    mapping = {
        "title": "title",
        "abstract": "abstract",
        "language": "language",
        "year": "year",
        "volume": "volume",
        "issue": "issue",
        "pages": "pages",
        "paper_type": "paper_type",
        "publication_date": "publication_date",
        "url": "url",
        "authors": "authors",
        "venue": "venue",
        "identifier:doi": "doi",
        "identifier:arxiv": "arxiv_id",
    }
    return mapping.get(field)


__all__ = ["router"]