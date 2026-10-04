"""Chunk reading: the one place that knows a paper's chunks in reading order.

``GET /api/papers/{paper_id}/chunks`` and the MCP reading tools
(``paper_get_chunks`` / ``paper_get_context``) all come through here, so "chunk 7
of this paper" and "the two chunks around this hit" mean exactly the same thing on
both surfaces -- the MCP contract requires REST/MCP equivalence, and an agent that
cites "section III-B, page 7" has to be pointing at the same text the REST client
would get.

Ordering is ``chunk_index``, which the chunker assigns in reading order. Chunk ids
are :class:`app.db.models.PaperChunk` primary keys -- the same value the index
stores as its document id and the same one search evidence carries, which is what
makes ``paper_get_context(chunk_id=...)`` work off a search hit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import PaperChunk
from app.schemas.paper import PaperChunkList, PaperChunkOut

#: Page size of ``GET /api/papers/{id}/chunks`` (and of ``paper_get_chunks``).
DEFAULT_LIMIT = 10
#: Hard cap per call: reading a whole long paper is what paging is for.
MAX_LIMIT = 200
#: Neighbour window bounds for :func:`chunk_context` (a caller asking for 50
#: chunks either side does not want a context call, they want the whole paper).
MAX_NEIGHBOURS = 20


def serialize_chunk(row: PaperChunk) -> PaperChunkOut:
    """One chunk in the shape both surfaces publish."""
    return PaperChunkOut(
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


def count_chunks(session: Session, paper_id: str) -> int:
    """How many chunks the paper has (0 for a paper that was never chunked)."""
    return int(
        session.execute(
            select(func.count(PaperChunk.id)).where(PaperChunk.paper_id == paper_id)
        ).scalar_one()
    )


def list_chunks(
    session: Session, paper_id: str, *, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> PaperChunkList:
    """Page through one paper's chunks in reading order.

    ``limit`` is clamped to ``1..MAX_LIMIT`` and ``offset`` to ``>= 0`` -- this is
    the REST contract (a client asking for 5000 chunks gets 200, not an error), and
    the MCP tool validates its own arguments *before* calling in, so an agent gets
    ``INVALID_ARGUMENT`` rather than a silent clamp.
    """
    total = count_chunks(session, paper_id)
    rows = (
        session.execute(
            select(PaperChunk)
            .where(PaperChunk.paper_id == paper_id)
            .order_by(PaperChunk.chunk_index)
            .limit(max(1, min(limit, MAX_LIMIT)))
            .offset(max(0, offset))
        )
        .scalars()
        .all()
    )
    return PaperChunkList(
        paper_id=paper_id, total=total, chunks=[serialize_chunk(row) for row in rows]
    )


def get_chunk(session: Session, chunk_id: str) -> PaperChunk | None:
    """One chunk by id, or ``None``.

    A malformed id is a miss, not a crash: the id column is a UUID, so an
    arbitrary string used to reach PostgreSQL as text and come back as a
    ``DataError`` (the same trap ``ingestion_service.get_job`` fell into).
    """
    try:
        uuid_value = str(chunk_id)
    except Exception:  # pragma: no cover - defensive, str() on a uuid never fails
        return None
    if not _looks_like_uuid(uuid_value):
        return None
    return session.execute(
        select(PaperChunk).where(PaperChunk.id == chunk_id)
    ).scalar_one_or_none()


def _looks_like_uuid(value: str) -> bool:
    from uuid import UUID

    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


@dataclass(slots=True)
class ContextWindow:
    """A chunk plus its neighbours inside one paper."""

    paper_id: str
    target: PaperChunk
    #: Chunks in reading order, including ``target``.
    chunks: list[PaperChunk] = field(default_factory=list)
    #: How many neighbours the caller asked for but the paper does not have
    #: (the window hit the start or the end of the paper).
    missing_before: int = 0
    missing_after: int = 0

    @property
    def truncated(self) -> bool:
        return bool(self.missing_before or self.missing_after)


def chunk_context(
    session: Session, chunk_id: str, *, before: int = 1, after: int = 1
) -> ContextWindow | None:
    """Read ``before``/``after`` chunks around ``chunk_id``, in reading order.

    Returns ``None`` when the chunk does not exist. ``before``/``after`` are
    clamped to ``0..MAX_NEIGHBOURS``; the counts that could not be served are
    reported in :attr:`ContextWindow.missing_before` / ``missing_after`` so the
    caller can say "this is the first chunk of the paper" instead of silently
    returning a short window.
    """
    target = get_chunk(session, chunk_id)
    if target is None:
        return None
    before = max(0, min(int(before), MAX_NEIGHBOURS))
    after = max(0, min(int(after), MAX_NEIGHBOURS))
    lower = target.chunk_index - before
    upper = target.chunk_index + after
    rows = (
        session.execute(
            select(PaperChunk)
            .where(
                PaperChunk.paper_id == target.paper_id,
                PaperChunk.chunk_index >= lower,
                PaperChunk.chunk_index <= upper,
            )
            .order_by(PaperChunk.chunk_index)
        )
        .scalars()
        .all()
    )
    window = ContextWindow(paper_id=target.paper_id, target=target, chunks=list(rows))
    indexes = {row.chunk_index for row in rows}
    window.missing_before = sum(
        1 for index in range(lower, target.chunk_index) if index not in indexes
    )
    window.missing_after = sum(
        1 for index in range(target.chunk_index + 1, upper + 1) if index not in indexes
    )
    return window


def page_count(session: Session, paper_id: str) -> int | None:
    """Highest page number the paper's chunks mention (``None`` when unknown).

    There is no page-count column: the parser records pages on chunks, so this is
    the honest answer to "how long is this paper" without a new migration.
    """
    highest = session.execute(
        select(func.max(PaperChunk.page_end)).where(PaperChunk.paper_id == paper_id)
    ).scalar_one()
    if highest is None:
        highest = session.execute(
            select(func.max(PaperChunk.page_start)).where(PaperChunk.paper_id == paper_id)
        ).scalar_one()
    return int(highest) if highest is not None else None
