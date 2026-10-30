"""Bind an original upload to an explicitly selected paper, without auto-match."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import IngestionJob, Paper
from app.services import ingestion_service as ingest
from app.services import metadata_identifiers, paper_service


class AttachmentError(ingest.IngestionError):
    """A missing target or a conflicting attachment (safe for API/job output)."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def target_for_upload(
    session: Session, paper_id: str, *, lock: bool = False,
    exclude_job_id: str | None = None,
) -> Paper:
    paper = paper_service.get_paper(session, paper_id)
    if paper is None:
        raise AttachmentError("目标论文不存在", 404)
    if lock:
        paper = session.execute(
            select(Paper).where(Paper.id == paper.id, Paper.deleted_at.is_(None))
            .with_for_update().execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if paper is None:
            raise AttachmentError("目标论文不存在", 404)
        session.expire(paper, ["files"])
    if paper_service.live_files(paper):
        raise AttachmentError("该论文已有原文，不能通过补传覆盖")
    active = select(IngestionJob.id).where(
        IngestionJob.paper_id == paper.id,
        IngestionJob.stage.not_in((ingest.STAGE_COMPLETED, ingest.STAGE_FAILED)),
    )
    if exclude_job_id:
        active = active.where(IngestionJob.id != exclude_job_id)
    if session.scalar(active.limit(1)):
        raise AttachmentError("该论文已有进行中的任务，请先查看任务状态")
    return paper


def validate_content(session: Session, paper: Paper, digest: str) -> None:
    existing = ingest.find_existing_paper(session, digest)
    if existing is not None and existing.id != paper.id:
        raise AttachmentError(f"相同 PDF 已属于另一篇论文：{existing.id}")


def create_job(
    session: Session, paper_id: str, *, object_key: str, digest: str,
    filename: str, content_type: str, size_bytes: int,
) -> IngestionJob:
    paper = target_for_upload(session, paper_id, lock=True)
    validate_content(session, paper, digest)
    job = ingest.create_job(
        session, source_type="file", filename=filename,
        content_type=content_type, size_bytes=size_bytes,
        payload={"object_key": object_key, "target_paper_id": paper.id},
    )
    job.paper_id = paper.id
    session.flush()
    return job


def validate_identifiers(session: Session, paper: Paper, embedded) -> None:
    """Reject exact identity conflicts; a user-selected target is never rematched."""
    for scheme, supplied, current in (
        ("doi", embedded.doi, paper.doi),
        ("arxiv", embedded.arxiv_id, paper.arxiv_id),
    ):
        normalized = metadata_identifiers.normalize_identifier(scheme, supplied)
        if not normalized:
            continue
        known = metadata_identifiers.normalize_identifier(scheme, current)
        if known and known != normalized:
            raise AttachmentError(f"PDF 的 {scheme} 与目标论文不一致，请核对原文")
        owner = metadata_identifiers.find_identifier(session, scheme, normalized)
        if owner is not None and owner.paper_id != paper.id:
            raise AttachmentError(f"PDF 的 {scheme} 已属于另一篇论文：{owner.paper_id}")
        # Older rows may only have the mirrored legacy columns.
        column = Paper.doi if scheme == "doi" else Paper.arxiv_id
        other = session.scalar(select(Paper.id).where(
            column == normalized, Paper.id != paper.id, Paper.deleted_at.is_(None),
        ).limit(1))
        if other:
            raise AttachmentError(f"PDF 的 {scheme} 已属于另一篇论文：{other}")
