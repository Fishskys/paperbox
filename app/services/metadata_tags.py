"""Index terms and tags, kept apart by kind (decision 10).

IEEE reports three different flavours of index terms ("official" IEEE terms,
author keywords, and system-expanded dynamic index terms) and they answer
different questions, so ``papers_tags.kind`` records which one a link came from.
arXiv categories or hand-made tags share the same structure via ``source_tag``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper, PaperTag, PapersTag, new_uuid
from app.services.paper_service import normalize_text

logger = get_logger(__name__)

KIND_IEEE_TERMS = "ieee_terms"
KIND_AUTHOR_TERMS = "author_terms"
KIND_DYNAMIC_INDEX_TERMS = "dynamic_index_terms"
KIND_SOURCE_TAG = "source_tag"

TAG_KINDS: tuple[str, ...] = (
    KIND_IEEE_TERMS,
    KIND_AUTHOR_TERMS,
    KIND_DYNAMIC_INDEX_TERMS,
    KIND_SOURCE_TAG,
)

#: Maximum length of ``paper_tags.name`` (the column is a String(128)).
MAX_TAG_LENGTH = 128


def normalize_tag(name: str | None) -> str:
    """Stable key of a tag name (``normalize_text``, trunced to the column)."""
    if not name:
        return ""
    key = normalize_text(name) or str(name).strip().casefold()
    return key[:MAX_TAG_LENGTH]


def get_or_create_tag(session: Session, name: str) -> PaperTag | None:
    """Return (or create) the ``paper_tags`` row for ``name``."""
    cleaned = str(name or "").strip()
    key = normalize_tag(cleaned)
    if not key or not cleaned:
        return None
    tag = session.execute(
        select(PaperTag).where(PaperTag.normalized_name == key)
    ).scalars().first()
    if tag is None:
        tag = PaperTag(
            id=new_uuid(), name=cleaned[:MAX_TAG_LENGTH], normalized_name=key
        )
        session.add(tag)
        session.flush()
    return tag


def link_tags(
    session: Session,
    paper: Paper,
    names: Iterable[str],
    *,
    kind: str = KIND_SOURCE_TAG,
) -> int:
    """Link ``names`` to ``paper`` with ``kind``; returns how many are new.

    Idempotent: an existing ``(paper, tag)`` link is left alone (re-importing the
    same IEEE record cannot create a second link), which is what keeps the
    importer replayable.
    """
    tag_kind = (kind or KIND_SOURCE_TAG).strip().lower()
    existing = {
        link.tag_id: link
        for link in session.execute(
            select(PapersTag).where(PapersTag.paper_id == paper.id)
        ).scalars().all()
    }
    created = 0
    seen: set[str] = set()
    for raw in names:
        tag = get_or_create_tag(session, str(raw))
        if tag is None or tag.id in seen:
            continue
        seen.add(tag.id)
        link = existing.get(tag.id)
        if link is None:
            session.add(
                PapersTag(
                    id=new_uuid(), paper_id=paper.id, tag_id=tag.id, kind=tag_kind
                )
            )
            created += 1
    if created:
        session.flush()
    return created


def tags_for_paper(
    session: Session, paper: Paper, *, kind: str | None = None
) -> list[str]:
    """Tag names linked to a paper, optionally narrowed to one kind."""
    statement = (
        select(PapersTag, PaperTag)
        .join(PaperTag, PaperTag.id == PapersTag.tag_id)
        .where(PapersTag.paper_id == paper.id)
    )
    if kind:
        statement = statement.where(PapersTag.kind == (kind or "").strip().lower())
    statement = statement.order_by(PaperTag.name.asc())
    rows = session.execute(statement).all()
    return [tag.name for _link, tag in rows]


def tag_links_for_paper(
    session: Session, paper: Paper
) -> list[tuple[PapersTag, PaperTag]]:
    """Raw ``(link, tag)`` pairs of a paper (used when building index documents)."""
    statement = (
        select(PapersTag, PaperTag)
        .join(PaperTag, PaperTag.id == PapersTag.tag_id)
        .where(PapersTag.paper_id == paper.id)
        .order_by(PaperTag.name.asc())
    )
    return [(link, tag) for link, tag in session.execute(statement).all()]


def tags_by_kind(session: Session, paper: Paper) -> dict[str, list[str]]:
    """``{kind: [names]}`` for one paper, stable order."""
    grouped: dict[str, list[str]] = {}
    for link, tag in tag_links_for_paper(session, paper):
        grouped.setdefault(link.kind or KIND_SOURCE_TAG, []).append(tag.name)
    return grouped


def replace_kind(
    session: Session, paper: Paper, kind: str, names: Sequence[str]
) -> int:
    """Drop the links of one kind and link ``names`` instead.

    Used when a source restates its index terms: leaving stale terms behind would
    slowly poison the tag filter. Other kinds are untouched.
    """
    target = (kind or KIND_SOURCE_TAG).strip().lower()
    session.query(PapersTag).filter(
        PapersTag.paper_id == paper.id, PapersTag.kind == target
    ).delete(synchronize_session=False)
    session.flush()
    return link_tags(session, paper, names, kind=target)


__all__ = [
    "KIND_AUTHOR_TERMS",
    "KIND_DYNAMIC_INDEX_TERMS",
    "KIND_IEEE_TERMS",
    "KIND_SOURCE_TAG",
    "MAX_TAG_LENGTH",
    "TAG_KINDS",
    "get_or_create_tag",
    "link_tags",
    "normalize_tag",
    "replace_kind",
    "tag_links_for_paper",
    "tags_by_kind",
    "tags_for_paper",
]