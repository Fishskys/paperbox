"""Paper metadata endpoints (MVP-SPEC section 2)."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.security import require_api_key
from app.db.models import PaperChunk
from app.db.session import get_db
from app.schemas.paper import PaperChunkList, PaperChunkOut, PaperListOut, PaperOut
from app.schemas.metadata import (
    MetadataPatch,
    MetadataPatchOut,
    MetadataRollbackIn,
    MetadataRollbackOut,
    PaperMetadataOut,
)
from app.search import opensearch
from app.search.opensearch import SearchIndexError
from app.services import ingestion_service as ingest
from app.services import metadata_manual
from app.services import object_storage
from app.services import paper_service as papers
from app.workers import queue as job_queue

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/papers",
    tags=["papers"],
    dependencies=[Depends(require_api_key)],
)

NOT_FOUND = "paper not found"
DISPOSITION = "attachment; filename={name}"


def _load_paper(session: Session, paper_id: str):
    paper = papers.get_paper(session, paper_id)
    if paper is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return paper


@router.get("", response_model=PaperListOut)
def list_papers(
    limit: int = 20,
    offset: int = 0,
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, description="case-insensitive title substring"),
    session: Session = Depends(get_db),
) -> PaperListOut:
    """List papers newest first (for browsing UIs and bulk maintenance)."""
    rows, total = papers.list_papers(
        session, limit=limit, offset=offset, status=status_filter, query=q
    )
    return PaperListOut(
        total=total,
        limit=limit,
        offset=offset,
        papers=[
            PaperOut.model_validate(
                papers.serialize_paper(row, source_url=row.url)
            )
            for row in rows
        ],
    )


@router.get("/{paper_id}", response_model=PaperOut)
def get_paper(paper_id: str, session: Session = Depends(get_db)) -> PaperOut:
    """Return the metadata of one paper (soft-deleted papers are 404)."""
    paper = _load_paper(session, paper_id)
    payload = papers.serialize_paper(paper, source_url=paper.url)
    return PaperOut.model_validate(payload)


@router.get("/{paper_id}/file")
def get_paper_file(paper_id: str, session: Session = Depends(get_db)):
    """Stream the stored original PDF with an attachment disposition."""
    paper = _load_paper(session, paper_id)
    record = papers.original_file(paper)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="paper file not found"
        )

    try:
        stream = object_storage.open_stream(record.object_key, bucket=record.bucket)
    except object_storage.ObjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="paper file not found"
        ) from exc
    except object_storage.ObjectStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="object storage unavailable",
        ) from exc

    filename = record.filename or object_storage.ORIGINAL_FILENAME
    media_type = record.content_type or object_storage.DEFAULT_PDF_CONTENT_TYPE
    headers = {"Content-Disposition": DISPOSITION.format(name=filename)}
    if record.size_bytes is not None:
        headers["Content-Length"] = str(record.size_bytes)

    def _iterate():
        with stream as body:
            while True:
                chunk = body.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(_iterate(), media_type=media_type, headers=headers)


@router.get("/{paper_id}/chunks", response_model=PaperChunkList)
def get_paper_chunks(
    paper_id: str,
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_db),
) -> PaperChunkList:
    """Return the stored chunks of one paper in reading order.

    ``limit``/``offset`` page through long papers (default 50 chunks per call);
    each chunk carries its page range and section so an answer can cite them.
    """
    _load_paper(session, paper_id)
    total = session.execute(
        select(func.count(PaperChunk.id)).where(PaperChunk.paper_id == paper_id)
    ).scalar_one()
    rows = (
        session.execute(
            select(PaperChunk)
            .where(PaperChunk.paper_id == paper_id)
            .order_by(PaperChunk.chunk_index)
            .limit(max(1, min(limit, 200)))
            .offset(max(0, offset))
        )
        .scalars()
        .all()
    )
    chunks = [
        PaperChunkOut(
            chunk_id=row.id,
            chunk_index=row.chunk_index,
            page_start=row.page_start,
            page_end=row.page_end,
            section=row.section,
            subsection=row.subsection,
            text=row.text,
            token_count=row.token_count,
            char_count=row.char_count,
        )
        for row in rows
    ]
    return PaperChunkList(paper_id=paper_id, total=total, chunks=chunks)


@router.get("/{paper_id}/metadata", response_model=PaperMetadataOut)
def get_paper_metadata(
    paper_id: str, session: Session = Depends(get_db)
) -> PaperMetadataOut:
    """Current metadata plus, per field, who said what and when (section 9).

    ``values`` is the merged current value of every field; ``provenance`` lists the
    claims behind them (including the ones that lost, with the ``provenance_id`` a
    rollback needs); ``sources`` names the records the paper was described by.
    """
    paper = _load_paper(session, paper_id)
    return PaperMetadataOut.model_validate(metadata_manual.metadata_view(session, paper))


@router.patch("/{paper_id}/metadata", response_model=MetadataPatchOut)
def patch_paper_metadata(
    paper_id: str,
    body: MetadataPatch,
    session: Session = Depends(get_db),
) -> MetadataPatchOut:
    """Edit one paper's metadata by hand (decision 12).

    A human edit is recorded as ``decided_by='manual'`` provenance and always wins
    over what a source or a heuristic said -- but it stays a claim, so it can be
    rolled back. Changing ``doi``/``arxiv_id`` replaces the identifier and
    re-derives the fingerprint.
    """
    paper = _load_paper(session, paper_id)
    payload = body.model_dump(exclude_unset=True)
    result = metadata_manual.patch_metadata(session, paper, payload)
    session.commit()
    return MetadataPatchOut.model_validate(result.as_dict())


@router.post("/{paper_id}/metadata/rollback", response_model=MetadataRollbackOut)
def rollback_paper_metadata(
    paper_id: str,
    body: MetadataRollbackIn,
    session: Session = Depends(get_db),
) -> MetadataRollbackOut:
    """Restore one field to an earlier claim (nothing is deleted)."""
    paper = _load_paper(session, paper_id)
    try:
        row = metadata_manual.rollback_metadata(
            session, paper, body.field, body.provenance_id
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    session.commit()
    return MetadataRollbackOut(
        paper_id=paper.id,
        field=row.field,
        provenance_id=row.id,
        value=row.value,
        decided_by=row.decided_by,
    )


@router.delete("/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_paper(paper_id: str, session: Session = Depends(get_db)) -> Response:
    """Delete a paper everywhere, following plan section 23.

    Order: load from PostgreSQL -> drop the paper's chunk documents from
    OpenSearch -> remove the stored objects from MinIO -> mark the paper deleted
    in PostgreSQL. Both purge steps are idempotent (missing documents/objects are
    tolerated), so if one of them fails the paper stays visible and the caller can
    simply retry the same request instead of ending up half-deleted.
    """
    paper = _load_paper(session, paper_id)

    try:
        removed_chunks = opensearch.delete_by_paper_id(paper.id)
    except SearchIndexError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"search index cleanup failed: {exc}",
        ) from exc

    try:
        removed_objects = object_storage.delete_prefix(paper.id)
    except object_storage.ObjectStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"object storage cleanup failed: {exc}",
        ) from exc

    papers.soft_delete_paper(session, paper)
    session.commit()
    logger.info(
        "paper deleted",
        extra={
            "extra_fields": {
                "paper_id": paper.id,
                "chunks_removed": removed_chunks,
                "objects_removed": removed_objects,
            }
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{paper_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
def reindex_paper_endpoint(
    paper_id: str,
    session: Session = Depends(get_db),
) -> dict:
    """Re-parse, re-chunk, re-embed and re-index one paper (plan section 24).

    Returns immediately with the ``job_id`` to poll via ``GET /api/jobs/{id}``.
    """
    paper = _load_paper(session, paper_id)
    record = papers.original_file(paper)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="paper file not found"
        )
    job = ingest.create_job(
        session,
        source_type="reindex",
        source=paper.url,
        filename=record.filename,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
    )
    job.paper_id = paper.id
    session.commit()
    job = job_queue.submit(session, job.id, job_queue.KIND_REINDEX) or job
    return {
        "job_id": job.id,
        "paper_id": paper.id,
        "status": job.stage,
        "stage": job.stage,
    }


__all__ = ["router"]
