"""Transactional metadata outbox; PostgreSQL commits survive index failures."""

from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import MetadataIndexUpdate, PaperChunk, new_uuid

logger = get_logger(__name__)


def enqueue(session: Session, paper_id: str) -> None:
    if session.scalar(select(PaperChunk.id).where(PaperChunk.paper_id == paper_id).limit(1)) is None:
        return
    if session.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    values = {"paper_id": paper_id, "revision": new_uuid(), "attempts": 0,
              "last_error": None, "updated_at": datetime.now(timezone.utc)}
    session.execute(insert(MetadataIndexUpdate).values(**values).on_conflict_do_update(
        index_elements=[MetadataIndexUpdate.paper_id], set_={k: v for k, v in values.items() if k != "paper_id"}))


def flush_pending(session: Session, *, paper_id: str | None = None, limit: int = 20) -> dict[str, int]:
    from app.search import opensearch, snapshot
    from app.services import paper_service

    statement = select(MetadataIndexUpdate.paper_id, MetadataIndexUpdate.revision).order_by(MetadataIndexUpdate.updated_at).limit(limit).with_for_update(skip_locked=True)
    if paper_id:
        statement = statement.where(MetadataIndexUpdate.paper_id == paper_id)
    pending = list(session.execute(statement))
    if not pending:
        return {"updated": 0, "failed": 0, "pending": 0}
    updates = []
    for current_id, revision in pending:
        paper = paper_service.get_paper(session, current_id)
        if paper is None:
            continue
        doc = {**snapshot.paper_metadata_snapshot(paper), "title": paper.title, "year": paper.year,
               "authors": paper_service.paper_author_names(paper), "doi": paper.doi, "arxiv_id": paper.arxiv_id}
        updates.extend({"chunk_id": cid, "doc": doc} for cid in session.scalars(
            select(PaperChunk.id).where(PaperChunk.paper_id == current_id)))
    try:
        result = opensearch.bulk_update_documents(updates) if updates else {"updated": 0, "failed": 0}
        error = "bulk_update_failed" if result["failed"] else None
    except Exception as exc:
        # Keep credential-bearing URLs and full backend responses out of the queue.
        error = type(exc).__name__
        result = {"updated": 0, "failed": len(updates)}
    for current_id, revision in pending:
        condition = (MetadataIndexUpdate.paper_id == current_id) & (MetadataIndexUpdate.revision == revision)
        if error:
            session.execute(update(MetadataIndexUpdate).where(condition).values(
                attempts=MetadataIndexUpdate.attempts + 1, last_error=error, updated_at=datetime.now(timezone.utc)))
        else:
            session.execute(delete(MetadataIndexUpdate).where(condition))
    session.commit()
    logger.info("metadata index synchronization", extra={"extra_fields": {
        "updated": result["updated"], "failed": result["failed"], "papers": len(pending), "error_code": error}})
    remaining = session.scalar(select(func.count()).select_from(MetadataIndexUpdate).where(
        MetadataIndexUpdate.paper_id.in_([row[0] for row in pending])))
    return {**result, "pending": remaining}
