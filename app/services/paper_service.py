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
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.logging import get_logger
from app.db.models import Author, Paper, PaperAuthor, PaperFile, new_uuid

logger = get_logger(__name__)

STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_INDEXED = "INDEXED"
STATUS_FAILED = "FAILED"
STATUS_DELETED = "DELETED"

FILE_KIND_ORIGINAL = "original"

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
        return f"{_TITLE_PREFIX}{normalized_title}|{normalized_author}|{year}"

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
    """Return the author row for ``name``, creating it when needed."""
    normalized = normalize_text(name) or name.strip().casefold()
    author = session.execute(
        select(Author).where(Author.normalized_name == normalized)
    ).scalar_one_or_none()
    if author is None:
        author = Author(
            id=new_uuid(), name=name.strip(), normalized_name=normalized
        )
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


def get_paper(session: Session, paper_id: str) -> Paper | None:
    """Fetch a live (not soft-deleted) paper with authors, venue and files."""
    statement = _paper_query().where(Paper.id == paper_id, Paper.deleted_at.is_(None))
    return session.execute(statement).scalar_one_or_none()


def list_papers(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    query: str | None = None,
) -> tuple[list[Paper], int]:
    """Return ``(papers, total)`` newest first, soft-deleted rows excluded.

    ``status`` filters on the paper status (PENDING/PROCESSING/INDEXED/FAILED)
    and ``query`` is a case-insensitive substring match on the title - both are
    conveniences for browsing UIs.
    """
    from sqlalchemy import func

    statement = _paper_query().where(Paper.deleted_at.is_(None))
    if status:
        statement = statement.where(Paper.status == status.strip().upper())
    if query and query.strip():
        statement = statement.where(Paper.title.ilike(f"%{query.strip()}%"))
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
    """The stored original PDF of a paper (if any)."""
    for record in paper.files:
        if record.deleted_at is None and record.kind == FILE_KIND_ORIGINAL:
            return record
    return None


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
) -> PaperFile:
    """Attach a stored original PDF to a paper."""
    record = PaperFile(
        id=new_uuid(),
        paper_id=paper.id,
        kind=FILE_KIND_ORIGINAL,
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
    return record


def soft_delete_paper(session: Session, paper: Paper) -> Paper:
    """Mark a paper (and its files) deleted; the caller commits."""
    now = datetime.now(timezone.utc)
    paper.deleted_at = now
    paper.status = STATUS_DELETED
    for record in paper.files:
        if record.deleted_at is None:
            record.deleted_at = now
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
        "authors": paper_author_names(paper),
        "status": paper.status,
        "fingerprint": paper.fingerprint,
        "files": files,
        "created_at": paper.created_at,
        "updated_at": paper.updated_at,
    }


__all__ = [
    "FILE_KIND_ORIGINAL",
    "STATUS_DELETED",
    "STATUS_FAILED",
    "STATUS_INDEXED",
    "STATUS_PENDING",
    "STATUS_PROCESSING",
    "build_fingerprint",
    "compute_sha256",
    "compute_sha256_file",
    "create_paper",
    "find_by_fingerprint",
    "find_by_sha256",
    "get_or_create_author",
    "get_paper",
    "list_paper_files",
    "normalize_arxiv_id",
    "normalize_doi",
    "normalize_text",
    "original_file",
    "paper_author_names",
    "register_original_file",
    "serialize_paper",
    "serialize_paper_file",
    "set_paper_authors",
    "soft_delete_paper",
    "title_from_url",
]
