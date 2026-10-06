"""Paper metadata service: fingerprinting, dedupe, CRUD and soft delete.

The fingerprint priority follows plan section 5.1:

    DOI > arXiv id > normalized title + first author + year > file SHA256

Fingerprinting and normalization are pure functions so they can be unit tested
without a database. The database helpers operate on a caller supplied
:class:`~sqlalchemy.orm.Session` so the API and the background worker can share
one transaction.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, inspect as sqlalchemy_inspect, select
from sqlalchemy.orm import Session, selectinload

from app.core.logging import get_logger
from app.db.models import (
    Author,
    Paper,
    PaperAuthor,
    PaperFile,
    PaperIdentifier,
    PaperTag,
    PapersTag,
    Venue,
    new_uuid,
)

logger = get_logger(__name__)

STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_INDEXED = "INDEXED"
STATUS_FAILED = "FAILED"
STATUS_DELETED = "DELETED"
#: Metadata-first shell: the record is stored, the PDF has not arrived yet.
STATUS_AWAITING_FILE = "AWAITING_FILE"

FILE_KIND_ORIGINAL = "original"
FILE_KIND_ARXIV_PDF = "arxiv_pdf"
FILE_KIND_PUBLISHED_PDF = "published_pdf"
FILE_KIND_SUPPLEMENT = "supplement"

FILE_KINDS: tuple[str, ...] = (
    FILE_KIND_ORIGINAL,
    FILE_KIND_ARXIV_PDF,
    FILE_KIND_PUBLISHED_PDF,
    FILE_KIND_SUPPLEMENT,
)

#: Which version wins when a paper has several PDFs (decision 14): the published
#: version beats whatever arrived first, the preprint loses to both. A supplement
#: is never parsed, so it ranks last.
PRIMARY_KIND_PRIORITY: dict[str, int] = {
    FILE_KIND_PUBLISHED_PDF: 3,
    FILE_KIND_ORIGINAL: 2,
    FILE_KIND_ARXIV_PDF: 1,
    FILE_KIND_SUPPLEMENT: 0,
}

#: Outcome labels of the primary-version decision.
PRIMARY_ACTION_PRIMARY = "primary"
PRIMARY_ACTION_PROMOTED = "promoted"
PRIMARY_ACTION_NON_PRIMARY = "non_primary"
PRIMARY_ACTION_NONE = "none"

_NON_WORD = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_ARXIV_PREFIX = "arxiv:"
_DOI_PREFIX = "doi:"
_SHA_PREFIX = "sha256:"
_TITLE_PREFIX = "title:"


def normalize_text(value: str | None) -> str:
    """Lower-case, strip accents and collapse punctuation to single spaces."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_ish = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_ish.casefold()
    return _NON_WORD.sub(" ", lowered).strip()


def normalize_doi(doi: str | None) -> str | None:
    """Normalize a DOI (strip URL prefixes and case) or return ``None``."""
    if not doi:
        return None
    candidate = doi.strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    candidate = candidate.strip()
    return candidate or None


def normalize_arxiv_id(arxiv_id: str | None) -> str | None:
    """Normalize an arXiv identifier (strip URL prefixes and version suffixes)."""
    if not arxiv_id:
        return None
    candidate = arxiv_id.strip().casefold()
    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/", "arxiv:"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    candidate = re.sub(r"v\d+$", "", candidate.strip())
    return candidate or None


def compute_sha256(data: bytes) -> str:
    """Hex SHA256 of a byte payload."""
    return hashlib.sha256(data).hexdigest()


def compute_sha256_file(path, *, chunk_size: int = 1024 * 1024) -> str:
    """SHA256 of a file on disk, read in bounded pieces.

    Used by the ``local_path`` source (server-side directory import and archive
    extraction): a 100 MB PDF must be hashed without ever being held in memory.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: ``papers.fingerprint`` is ``String(255)``; cap the title/author segments of
#: the ``title:`` branch so no input can exceed the column (review P2-1).
_TITLE_SEGMENT_CHARS = 200
_AUTHOR_SEGMENT_CHARS = 40


def build_fingerprint(
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
    title: str | None = None,
    first_author: str | None = None,
    year: int | str | None = None,
    sha256: str | None = None,
) -> str:
    """Deterministic fingerprint following the plan section 5.1 priority."""
    normalized_doi = normalize_doi(doi)
    if normalized_doi:
        return f"{_DOI_PREFIX}{normalized_doi}"

    normalized_arxiv = normalize_arxiv_id(arxiv_id)
    if normalized_arxiv:
        return f"{_ARXIV_PREFIX}{normalized_arxiv}"

    normalized_title = normalize_text(title)
    normalized_author = normalize_text(first_author)
    if normalized_title and normalized_author and year:
        # ``papers.fingerprint`` is String(255) (review 2026-10-05, P2-1): cap
        # the title segment so a pathological title cannot blow the column on
        # insert -- the same failure mode as the 2026-09-30 paper_chunks
        # section(255) incident. Only titles beyond the cap are affected.
        return (
            f"{_TITLE_PREFIX}{normalized_title[:_TITLE_SEGMENT_CHARS]}"
            f"|{normalized_author[:_AUTHOR_SEGMENT_CHARS]}|{year}"
        )

    if sha256:
        return f"{_SHA_PREFIX}{sha256.strip().casefold()}"

    fallback_source = normalized_title or "untitled"
    digest = hashlib.sha256(fallback_source.encode("utf-8")).hexdigest()
    return f"{_SHA_PREFIX}{digest}"


def title_from_url(url: str) -> str:
    """Best-effort human title from a source URL (last path segment)."""
    from urllib.parse import unquote, urlparse

    path = urlparse(url).path or ""
    tail = unquote(path.rstrip("/").rsplit("/", 1)[-1]) if path else ""
    tail = re.sub(r"\.pdf$", "", tail, flags=re.IGNORECASE).strip()
    return tail or url


# --------------------------------------------------------------------------- #
# author helpers
# --------------------------------------------------------------------------- #
def get_or_create_author(session: Session, name: str) -> Author:
    """Return the author row for ``name``, creating it when needed.

    ``authors.normalized_name`` is unique (``uq_authors_normalized_name``), so at
    most one row can match. The lookup is written tolerantly anyway - oldest row
    first, a warning when several match - because a database that has not run that
    migration yet can still hold duplicates, and a duplicate must not fail the
    whole paper (it used to raise ``MultipleResultsFound`` out of the ingestion
    pipeline and leave a FAILED job behind).
    """
    normalized = normalize_text(name) or name.strip().casefold()
    matches = (
        session.execute(
            select(Author)
            .where(Author.normalized_name == normalized)
            .order_by(Author.created_at, Author.id)
        )
        .scalars()
        .all()
    )
    if len(matches) > 1:
        logger.warning(
            "duplicate author rows share a normalized name",
            extra={
                "extra_fields": {
                    "normalized_name": normalized,
                    "rows": len(matches),
                    "kept": matches[0].id,
                }
            },
        )
    if matches:
        return matches[0]
    author = Author(id=new_uuid(), name=name.strip(), normalized_name=normalized)
    session.add(author)
    session.flush()
    return author


def set_paper_authors(session: Session, paper: Paper, names: Sequence[str]) -> None:
    """Replace the paper author list (order preserved, duplicates dropped).

    The previous links are removed first: reprocessing a paper (reindex, or a
    re-ingest that reaches the metadata stage twice) would otherwise collide with
    the ``uq_paper_authors_paper_author`` unique constraint on insert.
    """
    session.query(PaperAuthor).filter(PaperAuthor.paper_id == paper.id).delete(
        synchronize_session=False
    )
    session.flush()

    seen: set[str] = set()
    links: list[PaperAuthor] = []
    order = 0
    for raw_name in names:
        name = (raw_name or "").strip()
        if not name:
            continue
        normalized = normalize_text(name) or name.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        author = get_or_create_author(session, name)
        links.append(
            PaperAuthor(
                id=new_uuid(),
                paper_id=paper.id,
                author_id=author.id,
                author_order=order,
            )
        )
        order += 1
    # Drop the stale in-memory collection (its rows are gone) before assigning
    # the fresh links, so SQLAlchemy emits plain inserts.
    session.expire(paper, ["paper_authors"])
    paper.paper_authors = links
    session.flush()


def paper_author_names(paper: Paper) -> list[str]:
    """Author names ordered by ``paper_authors.author_order``."""
    links = sorted(paper.paper_authors, key=lambda link: link.author_order)
    return [link.author.name for link in links if link.author is not None]


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #
def _paper_query():
    return select(Paper).options(
        selectinload(Paper.paper_authors).selectinload(PaperAuthor.author),
        selectinload(Paper.venue),
        selectinload(Paper.files),
    )


#: Ingestion stages after which a job will not progress on its own.
TERMINAL_JOB_STAGES = ("COMPLETED", "FAILED")


@dataclass(slots=True)
class DeletePreview:
    """What deleting a paper would affect (``paper_delete`` dry_run)."""

    paper_id: str
    title: str
    chunks: int
    objects: int
    object_bytes: int
    #: Jobs still running for this paper: deleting under a running pipeline is the
    #: caller's call, but the preview has to say so.
    running_jobs: int


@dataclass(slots=True)
class PurgeOutcome:
    """What deleting a paper actually did."""

    paper_id: str
    chunks_removed: int
    objects_removed: int


def delete_preview(session: Session, paper: Paper) -> DeletePreview:
    """Count what a delete would remove: chunks, stored objects, running jobs.

    The object walk talks to object storage (there is no cheaper truthful answer);
    everything else is a database query.
    """
    from app.db.models import IngestionJob
    from app.services import chunk_service, object_storage

    chunks = chunk_service.count_chunks(session, paper.id)
    objects = 0
    total_bytes = 0
    try:
        for item in object_storage.list_objects(f"papers/{paper.id}/"):
            objects += 1
            total_bytes += int(getattr(item, "size", 0) or 0)
    except object_storage.ObjectStorageError as exc:  # storage down: say "unknown"
        logger.warning("delete preview could not list objects for %s: %s", paper.id, exc)
    running = session.execute(
        select(func.count(IngestionJob.id)).where(
            IngestionJob.paper_id == paper.id,
            IngestionJob.stage.not_in(TERMINAL_JOB_STAGES),
        )
    ).scalar_one()
    return DeletePreview(
        paper_id=paper.id,
        title=paper.title or "",
        chunks=chunks,
        objects=objects,
        object_bytes=total_bytes,
        running_jobs=int(running),
    )


def purge_paper(session: Session, paper: Paper) -> PurgeOutcome:
    """Delete a paper everywhere, in the order the REST endpoint documents.

    Chunks out of OpenSearch -> objects out of object storage -> soft delete in
    PostgreSQL. Both purge steps are idempotent, so a failure leaves the paper
    visible and the caller can simply retry instead of facing a half-deleted paper.
    Exceptions propagate unchanged (:class:`SearchIndexError`,
    :class:`ObjectStorageError`): mapping them to HTTP 503 or an MCP error is the
    surface's job.
    """
    from app.search import opensearch
    from app.services import object_storage

    removed_chunks = opensearch.delete_by_paper_id(paper.id)
    removed_objects = object_storage.delete_prefix(paper.id)
    soft_delete_paper(session, paper)
    session.commit()
    logger.info(
        "paper purged",
        extra={
            "extra_fields": {
                "paper_id": paper.id,
                "chunks_removed": removed_chunks,
                "objects_removed": removed_objects,
            }
        },
    )
    return PurgeOutcome(
        paper_id=paper.id, chunks_removed=removed_chunks, objects_removed=removed_objects
    )


def get_paper(session: Session, paper_id: str) -> Paper | None:
    """Fetch a live (not soft-deleted) paper with authors, venue and files.

    Paper ids are UUIDs, so a malformed id would otherwise reach PostgreSQL as a
    string and come back as ``DataError`` -- a 500 on every endpoint that loads a
    paper from the path. Same fix ``ingestion_service.get_job`` got (2026-10-04):
    reject the id here so both surfaces answer 404 / NOT_FOUND.
    """
    try:
        uuid.UUID(str(paper_id))
    except (ValueError, AttributeError, TypeError):
        return None
    statement = _paper_query().where(Paper.id == paper_id, Paper.deleted_at.is_(None))
    return session.execute(statement).scalar_one_or_none()


def list_papers(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    query: str | None = None,
    venue: Sequence[str] | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    paper_type: Sequence[str] | None = None,
    tag: Sequence[str] | None = None,
) -> tuple[list[Paper], int]:
    """Return ``(papers, total)`` newest first, soft-deleted rows excluded.

    ``status`` filters on the paper status (PENDING/PROCESSING/INDEXED/FAILED)
    and ``query`` is a case-insensitive substring match on the title - both are
    conveniences for browsing UIs.

    The metadata filters mirror what ``POST /api/search`` can filter on, but read
    PostgreSQL directly (and therefore see a metadata change immediately, unlike
    the index-time snapshot the search filters read):

    ``venue``
        Venue names, matched on the normalized key (so case/punctuation variants
        hit the same row).
    ``year_from`` / ``year_to``
        Bounds on ``papers.year`` (inclusive).
    ``paper_type``
        ``journal`` / ``conference`` / ``preprint`` / ``early_access`` / ``standard``.
    ``tag``
        Tag names of any kind, matched through ``papers_tags``.
    """
    from sqlalchemy import func

    # Imported here: ``metadata_tags`` and ``venue_service`` both import this
    # module at module level, so a top-level import would be circular.
    from app.services import metadata_tags, venue_service

    statement = _paper_query().where(Paper.deleted_at.is_(None))
    if status:
        statement = statement.where(Paper.status == status.strip().upper())
    if query and query.strip():
        statement = statement.where(Paper.title.ilike(f"%{query.strip()}%"))
    venue_keys = [
        venue_service.normalize_venue_name(name)
        for name in (venue or [])
        if name and str(name).strip()
    ]
    if venue_keys:
        statement = statement.join(Venue, Paper.venue_id == Venue.id).where(
            Venue.normalized_name.in_(venue_keys)
        )
    if year_from is not None:
        statement = statement.where(Paper.year >= int(year_from))
    if year_to is not None:
        statement = statement.where(Paper.year <= int(year_to))
    types = [value.strip().lower() for value in (paper_type or []) if value and str(value).strip()]
    if types:
        statement = statement.where(Paper.paper_type.in_(types))
    tag_keys = [
        metadata_tags.normalize_tag(name)
        for name in (tag or [])
        if name and str(name).strip()
    ]
    if tag_keys:
        # A subquery keeps a paper with two matching tags from being listed twice.
        statement = statement.where(
            Paper.id.in_(
                select(PapersTag.paper_id)
                .join(PaperTag, PapersTag.tag_id == PaperTag.id)
                .where(PaperTag.normalized_name.in_(tag_keys))
            )
        )
    total = session.execute(
        select(func.count()).select_from(statement.order_by(None).subquery())
    ).scalar_one()
    rows = (
        session.execute(
            statement.order_by(Paper.created_at.desc())
            .limit(max(1, min(limit, 200)))
            .offset(max(0, offset))
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


def find_by_fingerprint(session: Session, fingerprint: str) -> Paper | None:
    statement = _paper_query().where(
        Paper.fingerprint == fingerprint, Paper.deleted_at.is_(None)
    )
    return session.execute(statement).scalar_one_or_none()


def find_by_sha256(session: Session, sha256: str) -> Paper | None:
    """Find a live paper whose original file matches ``sha256``."""
    statement = (
        _paper_query()
        .join(PaperFile, PaperFile.paper_id == Paper.id)
        .where(
            PaperFile.sha256 == sha256,
            PaperFile.deleted_at.is_(None),
            Paper.deleted_at.is_(None),
        )
    )
    return session.execute(statement).scalars().first()


def list_paper_files(session: Session, paper_id: str) -> list[PaperFile]:
    statement = (
        select(PaperFile)
        .where(PaperFile.paper_id == paper_id, PaperFile.deleted_at.is_(None))
        .order_by(PaperFile.created_at.asc())
    )
    return list(session.execute(statement).scalars().all())


def original_file(paper: Paper) -> PaperFile | None:
    """The stored PDF of a paper: the primary version when one is marked.

    Falls back to the first live ``original`` file so rows written before the
    primary-version column existed (and the backfill) behave exactly as before.
    """
    live = [record for record in paper.files if record.deleted_at is None]
    for record in live:
        if record.is_primary:
            return record
    for record in live:
        if record.kind == FILE_KIND_ORIGINAL:
            return record
    return live[0] if live else None


def live_files(paper: Paper) -> list[PaperFile]:
    """Every file of a paper that has not been soft-deleted."""
    return [record for record in paper.files if record.deleted_at is None]


def primary_priority(kind: str | None) -> int:
    """Rank of a file kind for the primary-version rule (unknown kinds rank 0)."""
    return PRIMARY_KIND_PRIORITY.get((kind or "").strip().lower(), 0)


def select_primary_file(files: Sequence[PaperFile]) -> PaperFile | None:
    """Which of ``files`` should be the parsed/indexed version.

    Highest priority kind wins; ties go to the file that arrived first, so an
    existing primary is never displaced by an equally ranked newcomer.
    """
    live = [record for record in files if record.deleted_at is None]
    if not live:
        return None
    return max(
        live,
        key=lambda record: (
            primary_priority(record.kind),
            -(record.created_at.timestamp() if record.created_at else 0.0),
        ),
    )


@dataclass(frozen=True)
class PrimaryOutcome:
    """Result of applying the primary-version rule to one paper."""

    action: str
    primary: PaperFile | None = None
    previous: PaperFile | None = None

    @property
    def needs_reindex(self) -> bool:
        """A higher-priority version took over: the index must be rebuilt."""
        return self.action == PRIMARY_ACTION_PROMOTED

    @property
    def indexed(self) -> bool:
        """Whether the incoming file is the one that gets parsed and indexed."""
        return self.action in (PRIMARY_ACTION_PRIMARY, PRIMARY_ACTION_PROMOTED)


def apply_primary_selection(
    session: Session, paper: Paper, *, incoming: PaperFile | None = None
) -> PrimaryOutcome:
    """Mark the winning file of a paper as ``is_primary``.

    Returns what happened: ``primary`` (the incoming file is the one to index and
    there was no primary before), ``promoted`` (the incoming file displaced an
    existing primary -- the caller must reindex) or ``non_primary`` (the incoming
    file is only stored, the existing primary stays).
    """
    files = list_paper_files(session, paper.id)
    if not files:
        return PrimaryOutcome(action=PRIMARY_ACTION_NONE)
    current = next((record for record in files if record.is_primary), None)
    winner = select_primary_file(files)
    if winner is None:  # pragma: no cover - defensive, files is non-empty
        return PrimaryOutcome(action=PRIMARY_ACTION_NONE)

    if current is not None and current.id == winner.id:
        if incoming is None or incoming.id == current.id:
            return PrimaryOutcome(action=PRIMARY_ACTION_PRIMARY, primary=current)

    # Two flushes, demote first (same shape as ``remove_file``): the partial
    # unique index ``uq_paper_files_primary`` is checked row by row on
    # PostgreSQL, and the UPDATE order inside one flush is unspecified (random
    # UUID pk) -- promoting before demoting would transiently hold two primary
    # rows and fail the whole import with an IntegrityError.
    demoted = False
    for record in files:
        if record.id != winner.id and record.is_primary:
            record.is_primary = False
            demoted = True
    if demoted:
        session.flush()

    promoted = False
    for record in files:
        if record.id == winner.id and not record.is_primary:
            record.is_primary = True
            promoted = True
    if promoted:
        session.flush()

    if incoming is not None and incoming.id != winner.id:
        return PrimaryOutcome(
            action=PRIMARY_ACTION_NON_PRIMARY, primary=winner, previous=current
        )
    if current is not None and current.id != winner.id:
        return PrimaryOutcome(
            action=PRIMARY_ACTION_PROMOTED, primary=winner, previous=current
        )
    return PrimaryOutcome(action=PRIMARY_ACTION_PRIMARY, primary=winner, previous=current)


def remove_file(session: Session, paper: Paper, record: PaperFile) -> PrimaryOutcome:
    """Soft-delete one file and repair the primary selection (section 7.1).

    * a non-primary file disappears: nothing else happens (``none``);
    * the primary file disappears and another one is left: the next one by
      priority is promoted and the caller must reindex (``promoted``);
    * the last file disappears: the paper is marked ``FAILED`` and its index
      documents are left alone for a human to decide (``none``).
    """
    record.deleted_at = datetime.now(timezone.utc)
    was_primary = bool(record.is_primary)
    record.is_primary = False
    session.flush()

    remaining = list_paper_files(session, paper.id)
    if not remaining:
        paper.status = STATUS_FAILED
        session.flush()
        logger.warning(
            "paper %s lost its last file", paper.id, extra={"extra_fields": {"paper_id": paper.id}}
        )
        return PrimaryOutcome(action=PRIMARY_ACTION_NONE, previous=record)

    if not was_primary:
        return PrimaryOutcome(action=PRIMARY_ACTION_NONE, primary=primary_file(paper))

    winner = select_primary_file(remaining)
    for item in remaining:
        item.is_primary = item.id == (winner.id if winner else None)
    session.flush()
    return PrimaryOutcome(action=PRIMARY_ACTION_PROMOTED, primary=winner, previous=record)


def primary_file(paper: Paper) -> PaperFile | None:
    """The file currently marked as the paper's primary version."""
    for record in live_files(paper):
        if record.is_primary:
            return record
    return None


def is_shell(paper: Paper) -> bool:
    """Whether the paper is waiting for its PDF (metadata-first import)."""
    return (paper.status or "").upper() == STATUS_AWAITING_FILE


# --------------------------------------------------------------------------- #
# mutations
# --------------------------------------------------------------------------- #
def create_paper(
    session: Session,
    *,
    title: str,
    fingerprint: str,
    paper_id: str | None = None,
    abstract: str | None = None,
    language: str | None = None,
    year: int | None = None,
    doi: str | None = None,
    arxiv_id: str | None = None,
    url: str | None = None,
    status: str = STATUS_PENDING,
    authors: Iterable[str] | None = None,
) -> Paper:
    """Insert a paper row (and its authors) without committing."""
    paper = Paper(
        id=paper_id or new_uuid(),
        title=title.strip() or "untitled",
        fingerprint=fingerprint,
        abstract=abstract,
        language=language,
        year=year,
        doi=normalize_doi(doi),
        arxiv_id=normalize_arxiv_id(arxiv_id),
        url=url,
        status=status,
    )
    session.add(paper)
    session.flush()
    if authors:
        set_paper_authors(session, paper, list(authors))
    return paper


def register_original_file(
    session: Session,
    paper: Paper,
    *,
    object_key: str,
    bucket: str,
    url: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
    filename: str | None = None,
    content_type: str | None = None,
    kind: str = FILE_KIND_ORIGINAL,
    source_id: str | None = None,
) -> PaperFile:
    """Attach a stored PDF to a paper.

    The row starts with ``is_primary=False``: which file is *the* version is
    decided by :func:`apply_primary_selection` once every candidate is known.
    """
    record = PaperFile(
        id=new_uuid(),
        paper_id=paper.id,
        kind=kind or FILE_KIND_ORIGINAL,
        source_id=source_id,
        object_key=object_key,
        bucket=bucket,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
    )
    session.add(record)
    if url and not paper.url:
        paper.url = url
    session.flush()
    # The relationship may already be loaded (``original_file`` reads it), so it
    # has to be dropped for the new row to be visible there.
    state = sqlalchemy_inspect(paper)
    if state.persistent:
        session.expire(paper, ["files"])
    return record


def soft_delete_paper(session: Session, paper: Paper) -> Paper:
    """Mark a paper (and its files) deleted; the caller commits.

    The paper's ``paper_identifiers`` rows are *deleted* rather than kept: an
    identifier belongs to exactly one paper (``UNIQUE(scheme, normalized_value)``)
    and a tombstone that still holds a DOI would block the same DOI from ever being
    registered again -- which is precisely what deleting a paper is supposed to
    release, exactly like the partial unique index on ``papers.fingerprint``
    (``WHERE deleted_at IS NULL``). The claim history in
    ``paper_field_provenance`` and the source snapshots in ``paper_sources`` stay,
    so nothing about *what the paper said* is lost.
    """
    now = datetime.now(timezone.utc)
    paper.deleted_at = now
    paper.status = STATUS_DELETED
    for record in paper.files:
        if record.deleted_at is None:
            record.deleted_at = now
    session.query(PaperIdentifier).filter(PaperIdentifier.paper_id == paper.id).delete(
        synchronize_session=False
    )
    session.flush()
    return paper


def serialize_paper_file(record: PaperFile) -> dict:
    """Shape a ``paper_files`` row for :class:`app.schemas.paper.PaperFileOut`."""
    return {
        "storage_key": record.object_key,
        "sha256": record.sha256,
        "size_bytes": record.size_bytes,
        "mime_type": record.content_type,
        "url": None,
    }


def serialize_paper(paper: Paper, *, source_url: str | None = None) -> dict:
    """Shape a paper row for :class:`app.schemas.paper.PaperOut`."""
    files = [
        serialize_paper_file(record)
        for record in sorted(paper.files, key=lambda item: item.created_at or datetime.min)
        if record.deleted_at is None
    ]
    if source_url:
        for entry in files:
            if entry["url"] is None:
                entry["url"] = source_url
    return {
        "paper_id": paper.id,
        "title": paper.title,
        "abstract": paper.abstract,
        "language": paper.language,
        "year": paper.year,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "url": paper.url,
        "venue": paper.venue.name if paper.venue is not None else None,
        # Metadata snapshot of the citation record (mirrors the index snapshot,
        # but always current because it is read from PostgreSQL).
        "venue_year": paper.venue_year,
        "paper_type": paper.paper_type,
        "volume": paper.volume,
        "issue": paper.issue,
        "pages": paper.pages,
        "publication_date": paper.publication_date,
        "authors": paper_author_names(paper),
        "status": paper.status,
        "fingerprint": paper.fingerprint,
        "files": files,
        "created_at": paper.created_at,
        "updated_at": paper.updated_at,
    }


__all__ = [
    "FILE_KIND_ARXIV_PDF",
    "FILE_KIND_ORIGINAL",
    "FILE_KIND_PUBLISHED_PDF",
    "FILE_KIND_SUPPLEMENT",
    "FILE_KINDS",
    "PRIMARY_ACTION_NONE",
    "PRIMARY_ACTION_NON_PRIMARY",
    "PRIMARY_ACTION_PRIMARY",
    "PRIMARY_ACTION_PROMOTED",
    "PRIMARY_KIND_PRIORITY",
    "PrimaryOutcome",
    "STATUS_AWAITING_FILE",
    "STATUS_DELETED",
    "STATUS_FAILED",
    "STATUS_INDEXED",
    "STATUS_PENDING",
    "STATUS_PROCESSING",
    "apply_primary_selection",
    "build_fingerprint",
    "compute_sha256",
    "compute_sha256_file",
    "create_paper",
    "find_by_fingerprint",
    "find_by_sha256",
    "get_or_create_author",
    "get_paper",
    "is_shell",
    "list_paper_files",
    "live_files",
    "normalize_arxiv_id",
    "normalize_doi",
    "normalize_text",
    "original_file",
    "paper_author_names",
    "primary_file",
    "primary_priority",
    "register_original_file",
    "remove_file",
    "select_primary_file",
    "serialize_paper",
    "serialize_paper_file",
    "set_paper_authors",
    "soft_delete_paper",
    "title_from_url",
]
