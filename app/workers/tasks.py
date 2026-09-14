"""Background ingestion worker (MVP-SPEC section 2 + section 7).

``run_ingestion_job`` is executed by FastAPI ``BackgroundTasks`` after the HTTP
response has been sent. It owns its own session and never re-raises: failures are
recorded on the ``ingestion_jobs`` row so ``GET /api/jobs/{job_id}`` can report
them.

The MVP pipeline runs the whole way through::

    DOWNLOADING(10) -> STORED(30) -> PARSING(45) -> CHUNKING(60)
                    -> EMBEDDING(80) -> INDEXING(95) -> COMPLETED(100)

The first transaction downloads/validates/stores the PDF; the second one parses,
chunks, embeds and indexes it, then flips ``papers.status``
``PENDING -> PROCESSING -> INDEXED`` (``FAILED`` on any error).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import classify_failure
from app.core.logging import get_logger
from app.db.models import IngestionJob, Paper, PaperChunk, new_uuid
from app.db.session import SessionLocal
from app.parsing.chunking import chunk_document
from app.parsing.pdf import extract_pages
from app.parsing.structure import detect_sections, merge_short_sections
from app.search import opensearch
from app.services import embedding_service
from app.services import ingestion_service as ingest
from app.services import metadata_service, object_storage, paper_service

logger = get_logger(__name__)

#: Test/observability hook, fired right after each stage commit (None in production).
stage_commit_hook: Callable[[str, str, float], None] | None = None

#: Pipeline stages/progress. The full sequence is
#: RECEIVED(0) -> DOWNLOADING(10) -> STORED(30) -> PARSING(45) -> CHUNKING(60)
#: -> EMBEDDING(80) -> INDEXING(95) -> COMPLETED(100). FAILED keeps the stage
#: and progress it failed at.
STAGE_DOWNLOADING = "DOWNLOADING"
STAGE_STORED = "STORED"
STAGE_PARSING = "PARSING"
STAGE_CHUNKING = "CHUNKING"
STAGE_EMBEDDING = "EMBEDDING"
STAGE_INDEXING = "INDEXING"
PROGRESS_DOWNLOADING = 10.0
PROGRESS_STORED = 30.0
PROGRESS_PARSING = 45.0
PROGRESS_CHUNKING = 60.0
PROGRESS_EMBEDDING = 80.0
PROGRESS_INDEXING = 95.0


@dataclass(frozen=True)
class PayloadOutcome:
    """Result of the transaction that downloads/validates and stores the PDF."""

    paper_id: str
    duplicate: bool = False


def reindex_paper(
    session: Session, paper_id: str, job: IngestionJob | None = None
) -> IngestionJob:
    """Re-run PARSING -> INDEXING for an already stored paper (plan section 24).

    Used by ``POST /api/papers/{id}/reindex`` and by ``scripts/reindex.py`` when
    the chunking strategy, the embedding model or the index mapping changes.
    Pass ``job`` to reuse a job created by the caller (the API does this so the
    client can poll ``/api/jobs/{id}`` immediately); otherwise one is created.
    Returns the bookkeeping job; raises ``LookupError`` when the paper or its
    original object is missing.
    """
    paper = paper_service.get_paper(session, paper_id)
    if paper is None:
        raise LookupError(f"paper {paper_id} not found")
    record = paper_service.original_file(paper)
    if record is None:
        raise LookupError(f"paper {paper_id} has no stored original file")

    if job is None:
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

    try:
        _run_pipeline(session, job, paper, record.object_key, dedupe=False)
    except Exception as exc:  # noqa: BLE001 - mirror the ingestion path
        _record_failure(session, job.id, exc)
        raise
    return job


def run_reindex_job(job_id: str) -> None:
    """Background entry point for ``POST /api/papers/{id}/reindex``."""
    session = SessionLocal()
    try:
        job = ingest.get_job(session, job_id)
        if job is None:
            logger.warning("reindex job %s vanished", job_id)
            return
        if not job.paper_id:
            ingest.mark_failed(session, job, "reindex job has no paper_id")
            session.commit()
            return
        reindex_paper(session, job.paper_id, job=job)
    except Exception as exc:  # noqa: BLE001 - record and swallow: this is a worker
        logger.exception("reindex job %s failed", job_id)
        _record_failure(session, job_id, exc)
    finally:
        session.close()


def run_ingestion_job(job_id: str) -> None:
    """Drive one job from DOWNLOADING through INDEXING to COMPLETED (or FAILED)."""
    session = SessionLocal()
    try:
        job = ingest.get_job(session, job_id)
        if job is None:
            logger.warning("ingestion job %s not found", job_id)
            return
        try:
            outcome = _process_job(session, job)
        except Exception as exc:  # noqa: BLE001 - the job row records the failure
            session.rollback()
            logger.exception("ingestion job %s failed", job_id)
            _record_failure(session, job_id, exc)
            return
        logger.info(
            "ingestion job finished",
            extra={
                "extra_fields": {
                    "job_id": job_id,
                    "paper_id": outcome.paper_id,
                    "duplicate": outcome.duplicate,
                }
            },
        )
    finally:
        session.close()


def run_retry_job(job_id: str) -> None:
    """Background entry point for ``POST /api/jobs/{job_id}/retry`` (plan section 22).

    A job that failed *after* the STORED checkpoint (``paper_id`` set) has a
    paper row and a stored original to build on, so it resumes with the reindex
    semantics (PARSING -> INDEXING, no fingerprint discard). A job that failed
    before it persisted nothing, so the download/store transaction is redone
    from the job payload.
    """
    session = SessionLocal()
    try:
        job = ingest.get_job(session, job_id)
        has_paper = bool(job.paper_id) if job is not None else False
    finally:
        session.close()
    if job is None:
        logger.warning("retry job %s vanished", job_id)
        return
    if has_paper:
        run_reindex_job(job_id)
        return
    run_ingestion_job(job_id)


def _advance_stage(
    session: Session, job: IngestionJob, stage: str, progress: float
) -> None:
    """Set ``stage``/``progress`` and COMMIT so other sessions can see it.

    ``flush`` alone keeps the new values inside the worker's still-open
    transaction, so ``GET /api/jobs/{id}`` (a different session, hence a
    different transaction) only ever observed ``RECEIVED`` and the final
    ``COMPLETED``/``FAILED``. Each stage boundary is a consistent checkpoint
    (whatever has been written so far is valid), so committing there is safe.
    """
    job.stage = stage
    job.progress = progress
    session.commit()
    hook = stage_commit_hook
    if hook is not None:
        hook(job.id, stage, progress)


def _process_job(session: Session, job) -> PayloadOutcome:
    """Store the original PDF, then parse/chunk/embed/index it."""
    payload = job.payload or {}
    source_type = str(payload.get("source_type", "url")).lower()
    source_url = payload.get("source")

    _advance_stage(
        session, job, ingest.STAGE_DOWNLOADING, ingest.PROGRESS_DOWNLOADING
    )

    if source_type == "file":
        object_key = payload.get("object_key")
        if not object_key:
            raise ingest.IngestionError("uploaded file payload is missing")
        data = object_storage.download_bytes(object_key)
        filename = payload.get("filename") or object_storage.ORIGINAL_FILENAME
        content_type = payload.get("content_type") or ingest.PDF_CONTENT_TYPE
    else:
        url = ingest.ensure_valid_url(str(source_url or ""))
        download = ingest.download_pdf(url)
        data = download.data
        filename = download.filename
        content_type = download.content_type or ingest.PDF_CONTENT_TYPE

    ingest.validate_pdf_payload(data, filename, content_type)
    digest = paper_service.compute_sha256(data)

    existing = paper_service.find_by_sha256(session, digest)
    if existing is None:
        fingerprint = paper_service.build_fingerprint(sha256=digest)
        existing = paper_service.find_by_fingerprint(session, fingerprint)
    if existing is not None:
        ingest.resolve_duplicate(session, existing, job)
        session.commit()
        return PayloadOutcome(paper_id=existing.id, duplicate=True)

    paper_id = new_uuid()
    title = ingest.title_for_ingest(filename, source_url)
    fingerprint = paper_service.build_fingerprint(sha256=digest)
    paper = paper_service.create_paper(
        session,
        title=title,
        fingerprint=fingerprint,
        paper_id=paper_id,
        url=source_url,
        status=paper_service.STATUS_PENDING,
    )

    stored = object_storage.upload_bytes(
        object_storage.build_object_key(paper_id),
        data,
        content_type=ingest.PDF_CONTENT_TYPE,
        metadata={"paper_id": paper_id, "kind": "original"},
    )
    paper_service.register_original_file(
        session,
        paper,
        object_key=stored.object_key,
        bucket=stored.bucket,
        url=source_url,
        sha256=digest,
        size_bytes=stored.size_bytes,
        filename=filename,
        content_type=ingest.PDF_CONTENT_TYPE,
    )

    job.paper_id = paper.id
    job.stage = ingest.STAGE_STORED
    job.progress = ingest.PROGRESS_STORED
    paper.embedding_model = settings.embedding_model
    paper.embedding_dimension = settings.embedding_dimension
    # Commit the STORED checkpoint before the (slower) parsing half so a crash
    # later cannot lose the MinIO object + DB row.
    session.commit()

    try:
        _run_pipeline(session, job, paper, stored.object_key)
    except Exception:
        session.rollback()
        raise
    return PayloadOutcome(paper_id=paper_id)


def _run_pipeline(
    session: Session, job, paper: Paper, object_key: str, *, dedupe: bool = True
) -> None:
    """PARSING -> CHUNKING -> EMBEDDING -> INDEXING for one stored paper.

    ``dedupe`` is only safe for a *fresh* ingest: a fingerprint collision means
    the document was already in the library, so the row created by this ingest is
    thrown away. A reindex of an already indexed paper must NOT be discarded --
    it would purge the surviving paper's chunks and index documents. Reindex
    therefore calls with ``dedupe=False`` and keeps its original fingerprint when
    the upgraded one is taken by another live paper.
    """
    paper.status = paper_service.STATUS_PROCESSING

    _advance_stage(session, job, STAGE_PARSING, PROGRESS_PARSING)

    data = object_storage.download_bytes(object_key)
    pages = extract_pages(data)
    sections = merge_short_sections(detect_sections(pages))
    _backfill_metadata(session, paper, pages, data)

    # Parsing revealed DOI/arXiv/title, so the sha256 fingerprint can now be
    # upgraded to the real identity. A collision means this document is already
    # in the library: the fresh paper is discarded and the job ends as a
    # completed duplicate, before any chunk or index document is written.
    conflict = _upgrade_fingerprint(
        session, paper, sha256=_original_sha256(paper), discard_on_conflict=dedupe
    )
    if conflict is not None:
        _discard_duplicate_paper(session, paper, job, conflict)
        return

    _advance_stage(session, job, STAGE_CHUNKING, PROGRESS_CHUNKING)

    chunks = chunk_document(pages, sections)
    if not chunks:
        raise ingest.IngestionError("parsing produced no chunks")
    rows = _replace_chunks(session, paper, chunks)

    _advance_stage(session, job, STAGE_EMBEDDING, PROGRESS_EMBEDDING)

    vectors = embedding_service.embed_texts([chunk.text for chunk in chunks])
    if len(vectors) != len(rows):
        raise ingest.IngestionError(
            f"embedding count mismatch: {len(vectors)} vectors for {len(rows)} chunks"
        )
    now = datetime.now(timezone.utc)
    _write_embeddings(session, paper, rows, vectors, now)

    _advance_stage(session, job, STAGE_INDEXING, PROGRESS_INDEXING)

    opensearch.ensure_index()
    opensearch.delete_by_paper_id(paper.id)
    rows_out = _index_rows(paper, rows, vectors)
    result = opensearch.bulk_index_chunks(rows_out, refresh=True)
    if result["failed"]:
        raise ingest.IngestionError(
            f"opensearch rejected {result['failed']} chunk documents"
        )
    _mark_indexed(session, paper, rows, now)

    job.stage = ingest.STAGE_COMPLETED
    job.progress = ingest.PROGRESS_COMPLETED
    job.finished_at = now
    paper.status = paper_service.STATUS_INDEXED
    session.commit()


def _upgrade_fingerprint(
    session: Session,
    paper: Paper,
    sha256: str | None,
    *,
    discard_on_conflict: bool = True,
) -> Paper | None:
    """Re-derive the fingerprint now that parsing revealed DOI/arXiv/title.

    The STORED checkpoint fingerprints by ``sha256`` (nothing else is known
    then). Once metadata is backfilled the plan section 5.1 priority applies
    (DOI > arXiv > normalized title + first author + year > sha256), so the row
    is upgraded here -- before any chunks or index documents are written.

    Returns the *other* live paper that already claims the upgraded fingerprint,
    or ``None`` on success. Raises nothing on a race: the partial unique index
    ``uq_papers_fingerprint_live`` is the arbiter and the resulting
    ``IntegrityError`` is handled by the caller.
    """
    current = paper.fingerprint
    upgraded = paper_service.build_fingerprint(
        doi=paper.doi,
        arxiv_id=paper.arxiv_id,
        title=paper.title,
        first_author=_first_author_name(paper),
        year=paper.year,
        sha256=sha256,
    )
    if upgraded == current:
        return None

    conflict = _find_other_live_paper_by_fingerprint(session, upgraded, paper.id)
    if conflict is not None:
        if not discard_on_conflict:
            # Reindex path: this paper is already live and indexed, so it must
            # not be thrown away. Keep the fingerprint it has and carry on.
            logger.warning(
                "fingerprint upgrade skipped: %s already claimed by %s",
                upgraded,
                conflict.id,
                extra={
                    "extra_fields": {
                        "paper_id": paper.id,
                        "kept_fingerprint": current,
                        "wanted_fingerprint": upgraded,
                        "conflicting_paper_id": conflict.id,
                    }
                },
            )
            return None
        return conflict

    paper.fingerprint = upgraded
    try:
        session.flush()
    except IntegrityError:
        # Another ingest claimed the same DOI/arXiv between the lookup and the
        # flush; fall back to the duplicate path.
        session.rollback()
        conflict = _find_other_live_paper_by_fingerprint(session, upgraded, paper.id)
        if conflict is None:
            raise
        if not discard_on_conflict:
            logger.warning(
                "fingerprint upgrade lost a race for %s; keeping %s",
                upgraded,
                current,
                extra={"extra_fields": {"paper_id": paper.id}},
            )
            return None
        return conflict

    logger.info(
        "fingerprint upgraded",
        extra={
            "extra_fields": {
                "paper_id": paper.id,
                "from": current,
                "to": upgraded,
            }
        },
    )
    return None


def _first_author_name(paper: Paper) -> str | None:
    """Name of the paper's first author, in author order."""
    names = paper_service.paper_author_names(paper)
    return names[0] if names else None


def _original_sha256(paper: Paper) -> str | None:
    """Content hash recorded for this paper's stored original file.

    The pre-parse fingerprint is ``sha256:<digest>``; the digest lives on the
    ``paper_files`` row, so it has to be read back here to keep the sha256
    fallback of :func:`app.services.paper_service.build_fingerprint` intact when
    the fingerprint is upgraded after parsing.
    """
    record = paper_service.original_file(paper)
    candidates = [record] if record is not None else list(getattr(paper, "files", ()))
    for item in candidates:
        if item is None:
            continue
        digest = getattr(item, "sha256", None)
        if isinstance(digest, str) and digest.strip():
            return digest.strip()
    return None


def _find_other_live_paper_by_fingerprint(
    session: Session, fingerprint: str, paper_id: str
) -> Paper | None:
    """A live paper (other than ``paper_id``) holding ``fingerprint``."""
    statement = (
        select(Paper)
        .where(
            Paper.fingerprint == fingerprint,
            Paper.deleted_at.is_(None),
            Paper.id != paper_id,
        )
        .limit(1)
    )
    return session.execute(statement).scalars().first()


def _discard_duplicate_paper(
    session: Session, paper: Paper, job: IngestionJob, existing: Paper
) -> None:
    """Turn this ingest into a duplicate of ``existing`` (plan section 5.1).

    The freshly created paper lost the race: its chunks, index documents and
    stored objects are purged, the row is soft-deleted (which also releases the
    ``sha256:`` fingerprint it held) and the job is closed as a COMPLETED
    no-op pointing at the surviving paper -- the same shape
    ``ingest.resolve_duplicate`` produces for the pre-parse dedupe path.
    """
    session.query(PaperChunk).filter(PaperChunk.paper_id == paper.id).delete(
        synchronize_session=False
    )
    try:
        opensearch.delete_by_paper_id(paper.id)
    except Exception:  # noqa: BLE001 - cleanup is best effort, the row wins
        logger.warning("could not remove index documents for %s", paper.id)
    try:
        object_storage.delete_prefix(paper.id)
    except Exception:  # noqa: BLE001 - cleanup is best effort, the row wins
        logger.warning("could not remove stored objects for %s", paper.id)

    paper_service.soft_delete_paper(session, paper)
    ingest.resolve_duplicate(session, existing, job)
    session.commit()
    logger.info(
        "ingest duplicate detected after metadata upgrade",
        extra={
            "extra_fields": {
                "job_id": job.id,
                "discarded_paper_id": paper.id,
                "existing_paper_id": existing.id,
            }
        },
    )


def _backfill_metadata(session: Session, paper: Paper, pages, pdf_bytes: bytes | None = None) -> None:
    """Fill title/abstract/year/authors/arxiv_id from the parsed first pages."""
    metadata = metadata_service.extract_metadata(pages, url=paper.url, pdf_bytes=pdf_bytes)
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        paper.title = title.strip()
    abstract = metadata.get("abstract")
    if isinstance(abstract, str) and abstract.strip():
        paper.abstract = abstract.strip()
    year = metadata.get("year")
    if isinstance(year, int) and paper.year is None:
        paper.year = year
    arxiv_id = metadata.get("arxiv_id")
    if isinstance(arxiv_id, str) and arxiv_id.strip() and not paper.arxiv_id:
        paper.arxiv_id = paper_service.normalize_arxiv_id(arxiv_id)
    doi = metadata.get("doi")
    if isinstance(doi, str) and doi.strip() and not paper.doi:
        paper.doi = paper_service.normalize_doi(doi)
    authors = metadata.get("authors") or []
    if authors:
        paper_service.set_paper_authors(session, paper, list(authors))
    session.flush()


def _replace_chunks(session: Session, paper: Paper, chunks) -> list[PaperChunk]:
    """Delete previous chunks of this paper and insert the freshly built ones.

    Returns the persisted rows in reading order: the embedding and indexing
    stages work on ORM rows (they carry the database id used as ``chunk_id``).
    """
    session.query(PaperChunk).filter(PaperChunk.paper_id == paper.id).delete(
        synchronize_session=False
    )
    rows: list[PaperChunk] = []
    for chunk in chunks:
        row = PaperChunk(
            id=new_uuid(),
            paper_id=paper.id,
            chunk_index=chunk.chunk_index,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            section=chunk.section,
            subsection=chunk.section_title
            if chunk.section_title and chunk.section_title != chunk.section
            else None,
            text=chunk.text,
            token_count=chunk.token_count,
            char_count=chunk.char_count,
            embedding_model=settings.embedding_model,
            embedding_dimension=settings.embedding_dimension,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def _write_embeddings(
    session: Session,
    paper: Paper,
    rows: list[PaperChunk],
    vectors: list[list[float]],
    now: datetime,
) -> None:
    """Flag the persisted chunks as embedded.

    The vectors themselves live in OpenSearch (plan section 9); PostgreSQL keeps
    only the embedding provenance (model, dimension, timestamp).
    """
    for row, vector in zip(rows, vectors):
        metadata = dict(row.doc_metadata or {})
        metadata["embedding_model"] = settings.embedding_model
        if vector is not None:
            metadata["embedding_dimension"] = len(vector)
        row.doc_metadata = metadata
        row.embedding_model = settings.embedding_model
        row.embedding_dimension = settings.embedding_dimension
        row.embedded_at = now
    paper.embedding_model = settings.embedding_model
    paper.embedding_dimension = settings.embedding_dimension
    session.flush()


def _index_rows(
    paper: Paper, rows: list[PaperChunk], vectors: list[list[float]]
) -> list[dict]:
    """Build one OpenSearch document per persisted chunk (metadata + vector)."""
    authors = paper_service.paper_author_names(paper)
    tags = [
        link.tag.name
        for link in getattr(paper, "tag_links", [])
        if getattr(link, "tag", None) is not None
    ]
    venue = paper.venue.name if paper.venue is not None else None
    documents: list[dict] = []
    for row, vector in zip(rows, vectors):
        documents.append(
            {
                "chunk_id": row.id,
                "paper_id": paper.id,
                "title": paper.title,
                "authors": authors,
                "year": paper.year,
                "venue": venue,
                "doi": paper.doi,
                "arxiv_id": paper.arxiv_id,
                "tags": tags,
                "section": row.section,
                "section_title": row.subsection or row.section,
                "page_start": row.page_start,
                "page_end": row.page_end,
                "chunk_index": row.chunk_index,
                "text": row.text,
                "embedding": vector,
                "embedding_model": settings.embedding_model,
                "embedding_dimension": settings.embedding_dimension,
            }
        )
    return documents


def _mark_indexed(session: Session, paper: Paper, chunks, now: datetime) -> None:
    """Stamp ``indexed_at`` on every chunk of this paper."""
    ids = [chunk.id for chunk in chunks if chunk.id]
    if not ids:
        return
    (
        session.query(PaperChunk)
        .filter(PaperChunk.id.in_(ids))
        .update({PaperChunk.indexed_at: now}, synchronize_session=False)
    )
    session.flush()


def _record_failure(session: Session, job_id: str, exc: Exception) -> None:
    """Best-effort FAILED bookkeeping in a fresh transaction."""
    try:
        session.rollback()
        job = ingest.get_job(session, job_id)
        if job is None:
            return
        failure = classify_failure(exc)
        logger.warning(
            "ingestion job %s failed with %s", job_id, failure.code
        )
        ingest.mark_failed(session, job, failure.message, code=failure.code)
        if job.paper_id:
            paper = session.get(Paper, job.paper_id)
            if paper is not None:
                paper.status = paper_service.STATUS_FAILED
        session.commit()
    except Exception:  # noqa: BLE001 - nothing else we can do here
        logger.exception("could not record failure for job %s", job_id)
        session.rollback()


__all__ = [
    "PROGRESS_CHUNKING",
    "PROGRESS_DOWNLOADING",
    "PROGRESS_EMBEDDING",
    "PROGRESS_INDEXING",
    "PROGRESS_PARSING",
    "PROGRESS_STORED",
    "PayloadOutcome",
    "STAGE_CHUNKING",
    "STAGE_DOWNLOADING",
    "STAGE_EMBEDDING",
    "STAGE_INDEXING",
    "STAGE_PARSING",
    "STAGE_STORED",
    "reindex_paper",
    "run_ingestion_job",
    "run_reindex_job",
    "run_retry_job",
]
