"""``paper_sources``: where a description of a paper came from.

A source row is the anchor of the three-layer model -- provenance rows point at it
to say *who* claimed a value, and ``raw`` keeps the payload verbatim so a future
parser can be replayed without re-fetching anything.

``UNIQUE(source_type, source_ref)`` is what makes every import idempotent: the
same IEEE record imported twice updates nothing and creates no second row.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper, PaperSource, new_uuid

logger = get_logger(__name__)

#: Source types, in the order the design lists them (section 5 of the design doc).
SOURCE_TYPE_IEEE_API = "ieee_api"
SOURCE_TYPE_ARXIV_API = "arxiv_api"
SOURCE_TYPE_CROSSREF = "crossref"
SOURCE_TYPE_PDF_EMBEDDED = "pdf_embedded"
SOURCE_TYPE_PDF_HEURISTIC = "pdf_heuristic"
#: The file name a human handed us (``1706.03762__topic.pdf``). Weakest layer:
#: a name says *which* paper this is, never what its metadata is.
SOURCE_TYPE_FILENAME = "filename"
SOURCE_TYPE_IMPORT_FILE = "import_file"
SOURCE_TYPE_MANUAL = "manual"

SOURCE_TYPES: tuple[str, ...] = (
    SOURCE_TYPE_IEEE_API,
    SOURCE_TYPE_ARXIV_API,
    SOURCE_TYPE_CROSSREF,
    SOURCE_TYPE_PDF_EMBEDDED,
    SOURCE_TYPE_PDF_HEURISTIC,
    SOURCE_TYPE_FILENAME,
    SOURCE_TYPE_IMPORT_FILE,
    SOURCE_TYPE_MANUAL,
)

MATCH_STATUS_MATCHED = "matched"
MATCH_STATUS_PENDING = "pending"
MATCH_STATUS_AMBIGUOUS = "ambiguous"
MATCH_STATUS_REJECTED = "rejected"

MATCH_STATUSES: tuple[str, ...] = (
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_PENDING,
    MATCH_STATUS_AMBIGUOUS,
    MATCH_STATUS_REJECTED,
)

#: Statuses that need a human decision (``GET /api/metadata/review``).
REVIEW_STATUSES: tuple[str, ...] = (MATCH_STATUS_PENDING, MATCH_STATUS_AMBIGUOUS)


@dataclass(frozen=True)
class SourceRef:
    """The stable identity of a source record inside its source type."""

    source_type: str
    source_ref: str


def paper_heuristic_ref(paper_id: str) -> str:
    """``paper:<id>:heuristic`` -- one heuristic source per paper."""
    return f"paper:{paper_id}:heuristic"


def paper_filename_ref(paper_id: str) -> str:
    """``paper:<id>:filename`` -- one file-name source per paper."""
    return f"paper:{paper_id}:filename"


def paper_embedded_ref(paper_id: str) -> str:
    """``paper:<id>:embedded`` -- one embedded-metadata source per paper."""
    return f"paper:{paper_id}:embedded"


def manual_ref(paper_id: str) -> str:
    """``manual:<paper_id>`` -- one manual source per paper (all hand edits share it)."""
    return f"manual:{paper_id}"


def file_ref(path: str, sha256: str | None) -> str:
    """``file:<path>:<sha256>`` -- a local file that was imported."""
    digest = (sha256 or "unknown").strip()
    return f"file:{path}:{digest}"


def doi_ref(doi: str | None) -> str | None:
    """``doi:<normalized>`` when there is a DOI, else ``None``."""
    from app.services import metadata_identifiers as identifiers

    normalized = identifiers.normalize_identifier(identifiers.SCHEME_DOI, doi)
    return f"doi:{normalized}" if normalized else None


def ieee_ref(article_number: str | int | None) -> str | None:
    """``ieee:<article_number>`` when the record carries one."""
    if article_number in (None, ""):
        return None
    digits = str(article_number).strip()
    return f"ieee:{digits}" if digits else None


def arxiv_ref(arxiv_id: str | None) -> str | None:
    """``arxiv:<normalized id>`` when the record carries one."""
    from app.services import metadata_identifiers as identifiers

    normalized = identifiers.normalize_identifier(identifiers.SCHEME_ARXIV, arxiv_id)
    return f"arxiv:{normalized}" if normalized else None


def find_source(
    session: Session, source_type: str, source_ref: str
) -> PaperSource | None:
    """Look a source record up by its identity (the idempotency probe)."""
    if not source_type or not source_ref:
        return None
    statement = select(PaperSource).where(
        PaperSource.source_type == (source_type or "").strip().lower(),
        PaperSource.source_ref == source_ref,
    )
    return session.execute(statement).scalars().first()


def upsert_source(
    session: Session,
    *,
    source_type: str,
    source_ref: str,
    raw: dict | None = None,
    paper_id: str | None = None,
    content_type: str | None = None,
    match_status: str = MATCH_STATUS_PENDING,
    match_method: str | None = None,
    match_confidence: float | None = None,
    fetched_at: datetime | None = None,
    importer: str | None = None,
) -> PaperSource:
    """Create a source record, or return the existing one for the same identity.

    Re-importing the same record is a no-op except for filling in fields that were
    unknown the first time (a later run may know which paper it belongs to).
    """
    source_type = (source_type or "").strip().lower()
    existing = find_source(session, source_type, source_ref)
    if existing is not None:
        if paper_id and not existing.paper_id:
            existing.paper_id = paper_id
        if raw and not existing.raw:
            existing.raw = raw
        if content_type and not existing.content_type:
            existing.content_type = content_type
        if match_status and existing.match_status != match_status:
            existing.match_status = match_status
        if match_method and not existing.match_method:
            existing.match_method = match_method
        if match_confidence is not None and existing.match_confidence is None:
            existing.match_confidence = match_confidence
        if importer and not existing.importer:
            existing.importer = importer
        session.flush()
        return existing

    source = PaperSource(
        id=new_uuid(),
        paper_id=paper_id,
        source_type=source_type,
        source_ref=source_ref,
        content_type=content_type,
        raw=raw or {},
        match_status=match_status,
        match_method=match_method,
        match_confidence=match_confidence,
        fetched_at=fetched_at,
        importer=importer,
    )
    session.add(source)
    session.flush()
    return source


def sources_for_paper(session: Session, paper_id: str) -> list[PaperSource]:
    """Every source record attached to a paper, oldest first."""
    statement = (
        select(PaperSource)
        .where(PaperSource.paper_id == paper_id)
        .order_by(PaperSource.imported_at.asc())
    )
    return list(session.execute(statement).scalars().all())


def source_type_for_paper(session: Session, paper_id: str, source_type: str) -> PaperSource | None:
    """The paper's source row of one type (used by the manual-edit path)."""
    for source in sources_for_paper(session, paper_id):
        if source.source_type == source_type:
            return source
    return None


def attach_source(
    session: Session,
    source: PaperSource,
    paper: Paper | str,
    *,
    method: str = "manual",
    confidence: float = 1.0,
) -> PaperSource:
    """Attach a source record to a paper (human attribution, section 7)."""
    paper_id = paper if isinstance(paper, str) else paper.id
    source.paper_id = paper_id
    source.match_status = MATCH_STATUS_MATCHED
    source.match_method = method
    source.match_confidence = confidence
    session.flush()
    logger.info(
        "source attached",
        extra={
            "extra_fields": {
                "source_id": source.id,
                "paper_id": paper_id,
                "method": method,
            }
        },
    )
    return source


def review_queue(
    session: Session,
    *,
    statuses: Sequence[str] = REVIEW_STATUSES,
    limit: int = 50,
) -> list[PaperSource]:
    """Sources waiting for a human decision, newest first."""
    wanted = [status for status in statuses if status]
    if not wanted:
        return []
    statement = (
        select(PaperSource)
        .where(PaperSource.match_status.in_(wanted))
        .order_by(PaperSource.imported_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    return list(session.execute(statement).scalars().all())


def serialize_source(source: PaperSource) -> dict[str, Any]:
    """Shape a source row for the API (``raw`` is omitted: it can be large)."""
    return {
        "source_id": source.id,
        "paper_id": source.paper_id,
        "source_type": source.source_type,
        "source_ref": source.source_ref,
        "content_type": source.content_type,
        "match_status": source.match_status,
        "match_method": source.match_method,
        "match_confidence": source.match_confidence,
        "importer": source.importer,
        "fetched_at": source.fetched_at,
        "imported_at": source.imported_at,
    }


def count_by_status(session: Session, statuses: Iterable[str] | None = None) -> dict[str, int]:
    """``{match_status: count}`` for reporting (optionally filtered)."""
    statement = select(PaperSource)
    wanted = [status for status in (statuses or ()) if status]
    if wanted:
        statement = statement.where(PaperSource.match_status.in_(wanted))
    counts: dict[str, int] = {}
    for source in session.execute(statement).scalars().all():
        counts[source.match_status] = counts.get(source.match_status, 0) + 1
    return counts


__all__ = [
    "MATCH_STATUSES",
    "MATCH_STATUS_AMBIGUOUS",
    "MATCH_STATUS_MATCHED",
    "MATCH_STATUS_PENDING",
    "MATCH_STATUS_REJECTED",
    "REVIEW_STATUSES",
    "SOURCE_TYPES",
    "SOURCE_TYPE_ARXIV_API",
    "SOURCE_TYPE_CROSSREF",
    "SOURCE_TYPE_IEEE_API",
    "SOURCE_TYPE_IMPORT_FILE",
    "SOURCE_TYPE_MANUAL",
    "SOURCE_TYPE_PDF_EMBEDDED",
    "SOURCE_TYPE_PDF_HEURISTIC",
    "SourceRef",
    "arxiv_ref",
    "attach_source",
    "count_by_status",
    "doi_ref",
    "file_ref",
    "find_source",
    "ieee_ref",
    "manual_ref",
    "paper_embedded_ref",
    "paper_heuristic_ref",
    "review_queue",
    "serialize_source",
    "source_type_for_paper",
    "sources_for_paper",
    "upsert_source",
]