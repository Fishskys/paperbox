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
from sqlalchemy.orm import Session

from app.api.downloads import stream_original

from app.core.logging import get_logger
from app.core.security import require_admin, require_api_key, require_write
from app.db.session import get_db
from app.schemas.paper import (
    ReindexIn,
    ReindexOut,
    PaperChunkList,
    PaperDegradationList,
    PaperDegradationOut,
    PaperListOut,
    PaperOut,
)
from app.schemas.metadata import (
    ConflictDismissIn,
    ConflictDismissOut,
    MetadataPatch,
    MetadataPatchOut,
    MetadataRollbackIn,
    MetadataRollbackOut,
    PaperMetadataOut,
)
from app.search import opensearch
from app.search.opensearch import SearchIndexError
from app.services import chunk_service
from app.services import degradation_service
from app.services import ingestion_service as ingest
from app.services import metadata_manual
from app.services import object_storage
from app.services import paper_service as papers
from app.services import reindex_service
from app.workers import queue as job_queue

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/papers",
    tags=["papers"],
    dependencies=[Depends(require_api_key)],
)

NOT_FOUND = "paper not found"


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
    venue: list[str] | None = Query(default=None, description="venue name (repeatable)"),
    year_from: int | None = Query(default=None, description="inclusive lower bound on year"),
    year_to: int | None = Query(default=None, description="inclusive upper bound on year"),
    paper_type: list[str] | None = Query(
        default=None,
        description="journal | conference | preprint | early_access | standard (repeatable)",
    ),
    tag: list[str] | None = Query(default=None, description="tag name, any kind (repeatable)"),
    session: Session = Depends(get_db),
) -> PaperListOut:
    """List papers newest first (for browsing UIs and bulk maintenance).

    The metadata filters read PostgreSQL directly, so unlike the search filters
    they see a metadata change immediately (no reindex needed).
    """
    rows, total = papers.list_papers(
        session,
        limit=limit,
        offset=offset,
        status=status_filter,
        query=q,
        venue=venue,
        year_from=year_from,
        year_to=year_to,
        paper_type=paper_type,
        tag=tag,
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
    """Stream the stored original PDF with an attachment disposition.

    The API key is the credential here; the agent-facing alternative is the
    short-lived signed URL from ``GET /api/downloads/{paper_id}`` (same bytes,
    same streaming code).
    """
    paper = _load_paper(session, paper_id)
    record = papers.original_file(paper)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="paper file not found"
        )
    return stream_original(record)


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
    The ordering and clamping rules live in :mod:`app.services.chunk_service`,
    which the MCP reading tools share.
    """
    _load_paper(session, paper_id)
    return chunk_service.list_chunks(session, paper_id, limit=limit, offset=offset)


@router.get("/{paper_id}/degradations", response_model=PaperDegradationList)
def get_paper_degradations(
    paper_id: str,
    include_resolved: bool = Query(
        default=False,
        description="also list causes a later run of the stage no longer reported",
    ),
    session: Session = Depends(get_db),
) -> PaperDegradationList:
    """Return what the pipeline had to give up on while indexing this paper.

    Empty is the normal answer. A non-empty list means the paper is indexed but
    thinner than it could be -- e.g. ``chunking/semantic_fallback`` while the
    embedding server was down -- and is the signal for a re-run
    (``scripts/reindex.py --degraded``).
    """
    _load_paper(session, paper_id)
    rows = degradation_service.list_for_paper(
        session, paper_id, include_resolved=include_resolved
    )
    items = [degradation_service.to_out(row) for row in rows]
    return PaperDegradationList(
        paper_id=paper_id,
        total=len(items),
        degraded=any(item.resolved_at is None for item in items),
        degradations=items,
    )


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


@router.patch("/{paper_id}/metadata", response_model=MetadataPatchOut, dependencies=[Depends(require_write)])
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


@router.post(
    "/{paper_id}/metadata/conflicts/dismiss",
    response_model=ConflictDismissOut,
    dependencies=[Depends(require_write)],
)
def dismiss_paper_conflict(
    paper_id: str,
    body: ConflictDismissIn,
    session: Session = Depends(get_db),
) -> ConflictDismissOut:
    """人类裁决：保留现值，这条分歧不再进复核清单。

    与 ``rollback`` 配对 —— 那条是"被拒值其实是对的"。两者都不删任何声明，只改"哪条生效"
    或"分歧是否还开着"，所以此后仍可回滚（设计 §8 规则 5）。
    """
    paper = _load_paper(session, paper_id)
    try:
        row = metadata_manual.dismiss_conflict(
            session, paper, body.field, body.provenance_id
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    session.commit()
    return ConflictDismissOut(
        paper_id=paper.id, field=row.field, provenance_id=row.id
    )


@router.post("/{paper_id}/metadata/rollback", response_model=MetadataRollbackOut, dependencies=[Depends(require_write)])
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


@router.delete("/{paper_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
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
        papers.purge_paper(session, paper)
    except SearchIndexError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"search index cleanup failed: {exc}",
        ) from exc
    except object_storage.ObjectStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"object storage cleanup failed: {exc}",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reindex", response_model=ReindexOut, dependencies=[Depends(require_write)])
def reindex_library_endpoint(
    body: ReindexIn,
    session: Session = Depends(get_db),
) -> ReindexOut:
    """整库/批量重建索引：先**检测**为什么要重建，再按论文入队（plan §24）。

    两种真实场景驱动它：**换了 embedding 模型**（旧向量与新查询不可比，必须整库重嵌）与
    **从 pypdf 换成 docling**（要重新解析才能吃到新产物）。检测不止这两种，见
    ``app/services/reindex_service.py`` 的 DETECTORS —— 加一条新理由不需要改这个端点。

    选择逻辑与 ``scripts/reindex.py`` 共用（同一份 SQL、同一套检测），差别只在执行：
    脚本在本进程同步跑（可 Ctrl-C、可 ``--dry-run``），这里按论文**入队**后立即返回 ——
    单篇 30–370 秒，整库是小时级操作，不能让它挂在一次 HTTP 请求上。

    ``dry_run`` 默认 **true**：先拿到"选 N 篇 + 每条理由"，确认后再调一次写库。
    """
    try:
        plan = reindex_service.select_papers(
            session,
            paper_ids=body.paper_ids,
            reasons=body.reasons,
            include_all=body.include_all,
        )
    except ValueError as exc:  # 未知理由码
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    report = plan.as_dict()
    job_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    if not body.dry_run and plan.papers:
        job_ids, skipped = reindex_service.queue(session, plan.papers)

    note = (
        "试运行：没有排任何作业。确认后带 dry_run=false 再调一次。"
        if body.dry_run
        else (
            f"已排 {len(job_ids)} 个重建作业（队列按 INGEST_CONCURRENCY 串行执行，"
            "整库是小时级操作）；用 GET /api/jobs/{job_id} 或 GET /api/jobs/queue 跟进。"
        )
    )
    if not plan.papers:
        note = "没有需要重建的论文（检测全部为空）；要用 include_all=true 强制整库。"

    return ReindexOut(
        dry_run=body.dry_run,
        selected=len(plan.papers),
        queued=len(job_ids),
        job_ids=job_ids,
        skipped=skipped,
        reasons=report["reasons"],
        skipped_reasons=report["skipped_reasons"],
        embedding=report["embedding"],
        parser=report["parser"],
        note=note,
    )


@router.post("/{paper_id}/reindex", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_write)])
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
    job = ingest.create_reindex_job(session, paper, record)
    return {
        "job_id": job.id,
        "paper_id": paper.id,
        "status": job.stage,
        "stage": job.stage,
    }


__all__ = ["router"]
