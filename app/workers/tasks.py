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
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import classify_failure
from app.core.logging import get_logger
from app.db.models import IngestionJob, Paper, PaperChunk, PaperFile, new_uuid
from app.db.session import SessionLocal
from app.parsing.chunking import chunk_document
from app.parsing.pdf import EmbeddedMetadata, extract_embedded_metadata, extract_pages
from app.parsing.structure import detect_sections, merge_short_sections
from app.search import mappings, opensearch, snapshot
from app.services import embedding_service
from app.services import ingestion_service as ingest
from app.services import metadata_identifiers, metadata_matcher, metadata_merge
from app.services import metadata_service, metadata_shell, metadata_sources
from app.services import object_storage, paper_service, provenance_service

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
    #: ``False`` when the file was stored but not indexed (a non-primary version
    #: arrived, section 7.1). The job still ends ``COMPLETED``.
    indexed: bool = True
    reason: str | None = None


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
    """Store the original PDF, then parse/chunk/embed/index it.

    Three source types reach this function (``payload["source_type"]``):

    * ``file`` -- bytes already staged in object storage by ``/ingest/files``;
    * ``local_path`` -- the server reads a file off its own disk (``/ingest/dir``,
      ``/ingest/compressed``); nothing is staged;
    * ``url`` -- the worker downloads it.

    Whatever the source, the payload is deduped by SHA256 *before* a paper row is
    created, so a duplicate never produces a paper, a chunk or an index document.
    """
    payload = job.payload or {}
    source_url = payload.get("source")

    _advance_stage(
        session, job, ingest.STAGE_DOWNLOADING, ingest.PROGRESS_DOWNLOADING
    )

    source = _load_source(payload)
    digest = source.sha256

    existing = paper_service.find_by_sha256(session, digest)
    if existing is None:
        fingerprint = paper_service.build_fingerprint(sha256=digest)
        existing = paper_service.find_by_fingerprint(session, fingerprint)
    if existing is not None:
        ingest.resolve_duplicate(session, existing, job)
        session.commit()
        # Both temporary copies have served their purpose even when the content
        # turned out to be a duplicate: the staged upload and the extracted
        # archive file. Leaving them behind would fill the disk with files no
        # job will ever read.
        _cleanup_source(source, payload, payload.get("object_key"))
        return PayloadOutcome(paper_id=existing.id, duplicate=True)

    paper_id = new_uuid()
    title = ingest.title_for_ingest(source.filename, source_url)
    fingerprint = paper_service.build_fingerprint(sha256=digest)
    paper = paper_service.create_paper(
        session,
        title=title,
        fingerprint=fingerprint,
        paper_id=paper_id,
        url=source_url,
        status=paper_service.STATUS_PENDING,
    )

    stored = _store_source(paper_id, source)
    file_record = paper_service.register_original_file(
        session,
        paper,
        object_key=stored.object_key,
        bucket=stored.bucket,
        url=source_url,
        sha256=digest,
        size_bytes=stored.size_bytes,
        filename=source.filename,
        content_type=ingest.PDF_CONTENT_TYPE,
        kind=str(payload.get("file_kind") or paper_service.FILE_KIND_ORIGINAL),
    )

    job.paper_id = paper.id
    job.stage = ingest.STAGE_STORED
    job.progress = ingest.PROGRESS_STORED
    paper.embedding_model = settings.embedding_model
    paper.embedding_dimension = settings.embedding_dimension
    # Commit the STORED checkpoint before the (slower) parsing half so a crash
    # later cannot lose the MinIO object + DB row.
    session.commit()

    # The bytes are in object storage now: drop the staging object / the extracted
    # local file so neither accumulates (plan section 5).
    _cleanup_source(source, payload, payload.get("object_key"))

    try:
        _run_pipeline(session, job, paper, stored.object_key, file_record=file_record)
    except Exception:
        session.rollback()
        raise
    return PayloadOutcome(paper_id=paper_id)


@dataclass(frozen=True)
class _Source:
    """Where the PDF bytes come from, plus the digest the dedupe needs.

    Either ``data`` (bytes already in memory) or ``local_path`` (read straight
    into object storage, never buffered) is set.
    """

    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    data: bytes | None = None
    local_path: Path | None = None


def _load_source(payload) -> _Source:
    """Acquire the PDF for a job and hash it, without buffering local files."""
    source_type = str(payload.get("source_type", "url")).lower()

    if source_type == ingest.SOURCE_TYPE_LOCAL:
        return _load_local_source(payload)

    if source_type == "file":
        object_key = payload.get("object_key")
        if not object_key:
            raise ingest.IngestionError("uploaded file payload is missing")
        data = object_storage.download_bytes(object_key)
        filename = payload.get("filename") or object_storage.ORIGINAL_FILENAME
        content_type = payload.get("content_type") or ingest.PDF_CONTENT_TYPE
        ingest.validate_pdf_payload(data, filename, content_type)
        return _Source(
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            sha256=paper_service.compute_sha256(data),
            data=data,
        )

    url = ingest.ensure_valid_url(str(payload.get("source") or ""))
    download = ingest.download_pdf(url)
    content_type = download.content_type or ingest.PDF_CONTENT_TYPE
    ingest.validate_pdf_payload(download.data, download.filename, content_type)
    return _Source(
        filename=download.filename,
        content_type=content_type,
        size_bytes=len(download.data),
        sha256=paper_service.compute_sha256(download.data),
        data=download.data,
    )


def _load_local_source(payload) -> _Source:
    """Validate a server-side path and hash it in one streaming pass."""
    raw = str(payload.get("local_path") or "").strip()
    if not raw:
        raise ingest.LocalSourceUnavailable("job payload has no local_path")
    path = Path(raw)
    try:
        stat = path.stat()
    except OSError as exc:
        raise ingest.LocalSourceUnavailable(f"local file is gone: {path}") from exc
    if not path.is_file():
        raise ingest.LocalSourceUnavailable(f"local path is not a file: {path}")
    if stat.st_size == 0:
        raise ingest.UnsupportedSource("local file is empty")
    ingest.ensure_size(stat.st_size)

    filename = str(payload.get("filename") or path.name)
    content_type = str(payload.get("content_type") or ingest.PDF_CONTENT_TYPE)
    if not ingest.is_pdf(filename, content_type):
        raise ingest.UnsupportedSource("only PDF files are supported")
    return _Source(
        filename=filename,
        content_type=content_type,
        size_bytes=stat.st_size,
        sha256=paper_service.compute_sha256_file(path),
        local_path=path,
    )


def _store_source(paper_id: str, source: _Source):
    """Upload the payload to ``papers/<paper_id>/original.pdf``.

    A ``local_path`` source is streamed off disk; everything else is already in
    memory (and already validated), so it goes up in one call.
    """
    key = object_storage.build_object_key(paper_id)
    metadata = {"paper_id": paper_id, "kind": "original"}
    if source.local_path is not None:
        with source.local_path.open("rb") as handle:
            return object_storage.upload_file(
                key,
                handle,
                length=source.size_bytes,
                content_type=ingest.PDF_CONTENT_TYPE,
                metadata=metadata,
            )
    return object_storage.upload_bytes(
        key,
        source.data or b"",
        content_type=ingest.PDF_CONTENT_TYPE,
        metadata=metadata,
    )


def _cleanup_source(
    source: _Source, payload, staging_key: object | None = None
) -> None:
    """Remove the temporary copies of a payload that has been stored for good.

    Two things may be left over after the STORED checkpoint: the staging object
    of a multipart upload (``payload["object_key"]``) and the local file of a
    ``local_path`` source that asked to be cleaned up (``cleanup_after``, set for
    files extracted from an archive). Both deletions are best effort: the paper
    is already stored and must not fail because a leftover could not be removed.
    """
    if staging_key:
        try:
            object_storage.delete_object(str(staging_key))
        except Exception:  # noqa: BLE001 - the GC retries, the paper is safe
            logger.warning("could not delete staging object %s", staging_key)
    if source.local_path is None or not payload.get("cleanup_after"):
        return
    remove_local_file(source.local_path)


def remove_local_file(path) -> bool:
    """Delete a server-side file and prune its directory when it emptied.

    Returns whether the file is gone. Used for archive extraction directories
    (``cleanup_after``): the extraction dir must disappear once its last PDF has
    been stored, or a 1000-file archive would leave 1000 empty directories
    behind. Never raises -- the caller is a worker that must not fail here.
    """
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("could not delete local file %s", path)
        return False
    parent = Path(path).parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        logger.debug("could not prune directory %s", parent)
    return True


def _run_pipeline(
    session: Session,
    job,
    paper: Paper,
    object_key: str,
    *,
    dedupe: bool = True,
    file_record: PaperFile | None = None,
) -> None:
    """PARSING -> CHUNKING -> EMBEDDING -> INDEXING for one stored paper.

    ``dedupe`` is only safe for a *fresh* ingest: a fingerprint collision means
    the document was already in the library, so the row created by this ingest is
    thrown away. A reindex of an already indexed paper must NOT be discarded --
    it would purge the surviving paper's chunks and index documents. Reindex
    therefore calls with ``dedupe=False`` and keeps its original fingerprint when
    the upgraded one is taken by another live paper.

    A fresh ingest additionally resolves *which paper* the PDF belongs to
    (sections 7 and 7.1): a record imported before its file left a shell paper
    behind, and a second version of an already indexed paper belongs to that paper
    rather than to a new one. When the arriving version is not the primary one,
    the job ends here as a completed no-op -- nothing is parsed and the index of
    the surviving version is left untouched.
    """
    paper.status = paper_service.STATUS_PROCESSING

    _advance_stage(session, job, STAGE_PARSING, PROGRESS_PARSING)

    data = object_storage.download_bytes(object_key)
    pages = extract_pages(data)
    sections = merge_short_sections(detect_sections(pages))

    record = None
    if dedupe and file_record is not None:
        resolution = _resolve_target_paper(
            session, job, paper, object_key, file_record, data, pages
        )
        paper = resolution.paper
        object_key = resolution.object_key
        record = resolution.record
        if resolution.decision == paper_service.PRIMARY_ACTION_NON_PRIMARY:
            _finish_non_primary(session, job, paper, resolution.previous_status)
            return

    _reset_placeholder_title(session, paper, job)
    _backfill_metadata(session, paper, pages, data)
    _restore_placeholder_title(session, paper, job)

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


@dataclass(frozen=True)
class _TargetResolution:
    """Which paper an arriving PDF belongs to, and whether it is the primary one."""

    paper: Paper
    object_key: str
    record: PaperFile | None
    decision: str
    previous_status: str


def _resolve_target_paper(
    session: Session,
    job: IngestionJob,
    paper: Paper,
    object_key: str,
    record: PaperFile,
    data: bytes,
    pages,
) -> _TargetResolution:
    """Reuse an existing paper for this PDF when the evidence says so.

    Two situations reach this code (sections 7 and 7.1 of the plan):

    * a **shell** exists because the record was imported before its file -- the
      PDF must join it instead of creating a second paper;
    * the paper is already indexed and this is **another version** of it (arXiv
      preprint vs. published PDF) -- the file joins the paper and the
      primary-version rule decides whether anything is re-indexed.

    Evidence is the embedded PDF metadata first (layer 1: DOI/arXiv are exact),
    then the first-page heuristics (title + first author + year, 0.8). The paper
    row created by this ingest is excluded: it must never match itself.
    """
    previous_status = paper.status
    match = _match_existing_paper(session, paper, record, data, pages)
    if match is not None and match.matched and match.paper is not None:
        # Remember the status of the paper we are joining *before* adoption (a
        # shell turns PENDING there); a non-primary arrival has to leave it alone.
        previous_status = match.paper.status
        source_id = _first_source_id(session, match.paper)
        paper = metadata_shell.adopt_paper(
            session, paper, match.paper, source_id=source_id
        )
        payload = dict(job.payload or {})
        payload["reused_paper_id"] = paper.id
        payload["match_method"] = match.method
        job.payload = payload
        session.flush()

    outcome = paper_service.apply_primary_selection(session, paper, incoming=record)
    return _TargetResolution(
        paper=paper,
        object_key=record.object_key,
        record=record,
        decision=outcome.action,
        previous_status=previous_status,
    )


def _match_existing_paper(
    session: Session, paper: Paper, record: PaperFile | None, data: bytes, pages
):
    """Match the arriving PDF against the library, or ``None`` when nothing fits."""
    filename = record.filename if record is not None else None
    embedded = extract_embedded_metadata(data)
    if not embedded.is_empty:
        match_input = metadata_matcher.match_input_from_values(
            metadata_service.embedded_claim_values(embedded), filename=filename
        )
        result = metadata_matcher.match_record(
            session, match_input, exclude_paper_id=paper.id
        )
        if result.matched:
            return result

    heuristics = {}
    try:
        heuristics = metadata_service.extract_metadata(
            pages, url=paper.url, pdf_bytes=None
        )
    except Exception as exc:  # noqa: BLE001 - matching is best effort
        # The heuristics are only used here to *find* a paper to reuse; failing to
        # compute them must never fail the ingest itself (the parse stage later
        # runs the real extraction and reports its own errors).
        logger.debug("cannot run heuristics for matching: %s", exc)
    match_input = metadata_matcher.match_input_from_values(
        metadata_service.heuristic_claim_values(heuristics), filename=filename
    )
    if not match_input.identifiers and not match_input.title:
        return None
    result = metadata_matcher.match_record(
        session, match_input, exclude_paper_id=paper.id
    )
    return result if result.matched else None


def _first_source_id(session: Session, paper: Paper) -> str | None:
    """The source record a reused paper was built from (the file inherits it)."""
    rows = metadata_sources.sources_for_paper(session, paper.id)
    return rows[0].id if rows else None


def _finish_non_primary(
    session: Session, job: IngestionJob, paper: Paper, previous_status: str
) -> None:
    """Close a job whose file is only a stored, non-primary version.

    Nothing is parsed, no chunk or index document is touched: the paper keeps the
    version that is already indexed (section 7.1).
    """
    payload = dict(job.payload or {})
    payload["indexed"] = False
    payload["reason"] = "non_primary_version"
    job.payload = payload
    job.paper_id = paper.id
    job.stage = ingest.STAGE_COMPLETED
    job.progress = ingest.PROGRESS_COMPLETED
    job.finished_at = datetime.now(timezone.utc)
    if previous_status and previous_status != paper_service.STATUS_PROCESSING:
        paper.status = previous_status
    session.commit()
    logger.info(
        "non-primary version stored without indexing",
        extra={
            "extra_fields": {
                "job_id": job.id,
                "paper_id": paper.id,
                "reason": "non_primary_version",
            }
        },
    )


def _placeholder_title(job: IngestionJob) -> str:
    """The file-name derived title this ingest gave the paper (if any)."""
    payload = job.payload or {}
    return ingest.title_for_ingest(payload.get("filename"), payload.get("source"))


def _reset_placeholder_title(session: Session, paper: Paper, job: IngestionJob) -> None:
    """Drop the file-name title so the heuristics can state the real one.

    A fresh ingest names the paper after its file, and that name was never a
    claim. "Fill blanks only" would therefore keep ``low_power_sram.pdf`` as the
    title forever; clearing it here is what makes the first-page heuristics (or a
    structured source) able to state the actual title.
    """
    if provenance_service.current_claim(session, paper.id, "title") is not None:
        return
    placeholder = _placeholder_title(job)
    if placeholder and (paper.title or "").strip() == placeholder.strip():
        paper.title = ""
        session.flush()


def _restore_placeholder_title(
    session: Session, paper: Paper, job: IngestionJob
) -> None:
    """Keep a usable title when nothing better was found (``title`` is NOT NULL)."""
    if (paper.title or "").strip():
        return
    paper.title = _placeholder_title(job) or "untitled"
    session.flush()


def _backfill_metadata(session: Session, paper: Paper, pages, pdf_bytes: bytes | None = None) -> None:
    """Fill the paper's metadata from the PDF, layer 1 then layer 2.

    Two sources are recorded, in this order:

    1. ``pdf_embedded`` -- the Info dictionary / XMP packet (no network, no
       guessing). Structured, so it may correct a heuristic value.
    2. ``pdf_heuristic`` -- the first-page heuristics that have always run. They
       only fill what is still blank (rule R2).

    Every value goes through the merge engine and lands in
    ``paper_field_provenance``, so ``GET /api/papers/{id}/metadata`` can say where
    each field came from.
    """
    embedded = extract_embedded_metadata(pdf_bytes) if pdf_bytes else EmbeddedMetadata(raw={})
    embedded_values = metadata_service.embedded_claim_values(embedded)
    if embedded_values:
        source = metadata_sources.upsert_source(
            session,
            source_type=metadata_sources.SOURCE_TYPE_PDF_EMBEDDED,
            source_ref=metadata_sources.paper_embedded_ref(paper.id),
            raw=embedded.raw or {},
            paper_id=paper.id,
            match_status=metadata_sources.MATCH_STATUS_MATCHED,
            match_method="embedded",
            match_confidence=1.0,
            importer="pipeline",
        )
        metadata_merge.merge_values(
            session,
            paper,
            embedded_values,
            source_type=metadata_sources.SOURCE_TYPE_PDF_EMBEDDED,
            source_id=source.id,
            confidence=1.0,
        )

    heuristics = metadata_service.extract_metadata(
        pages, url=paper.url, pdf_bytes=pdf_bytes
    )
    values = metadata_service.heuristic_claim_values(heuristics)
    if values:
        source = metadata_sources.upsert_source(
            session,
            source_type=metadata_sources.SOURCE_TYPE_PDF_HEURISTIC,
            source_ref=metadata_sources.paper_heuristic_ref(paper.id),
            raw=dict(heuristics),
            paper_id=paper.id,
            match_status=metadata_sources.MATCH_STATUS_MATCHED,
            match_method="heuristic",
            match_confidence=0.5,
            importer="pipeline",
        )
        metadata_merge.merge_values(
            session,
            paper,
            values,
            source_type=metadata_sources.SOURCE_TYPE_PDF_HEURISTIC,
            source_id=source.id,
            confidence=0.5,
        )

    metadata_identifiers.mirror_legacy_columns(session, paper)
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


def _tag_names_by_kind(paper: Paper) -> dict[str, list[str]]:
    """Backwards-compatible alias for :func:`app.search.snapshot.tag_names_by_kind`."""
    return snapshot.tag_names_by_kind(paper)


def _identifier_strings(paper: Paper) -> list[str]:
    """Backwards-compatible alias for :func:`app.search.snapshot.identifier_strings`."""
    return snapshot.identifier_strings(paper)


def _index_rows(
    paper: Paper, rows: list[PaperChunk], vectors: list[list[float]]
) -> list[dict]:
    """Build one OpenSearch document per persisted chunk (metadata + vector).

    The metadata part is :func:`app.search.snapshot.paper_metadata_snapshot` - the
    *filter snapshot* of ``POST /api/search``: venue name plus edition year, paper
    type, citation fields, identifiers and one tag list per kind. It is written at
    index time, so a metadata change only reaches filtering once the paper is
    reindexed (or ``scripts/refresh_index_metadata.py`` rewrites the snapshot).
    """
    authors = paper_service.paper_author_names(paper)
    tags = [
        link.tag.name
        for link in getattr(paper, "tag_links", [])
        if getattr(link, "tag", None) is not None
    ]
    venue = paper.venue.name if paper.venue is not None else None
    metadata = snapshot.paper_metadata_snapshot(paper)
    documents: list[dict] = []
    for row, vector in zip(rows, vectors):
        document: dict = {
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
        # --- metadata snapshot: the fields POST /api/search filters read ---
        document.update(metadata)
        documents.append(document)
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
    "remove_local_file",
    "run_ingestion_job",
    "run_reindex_job",
    "run_retry_job",
]
