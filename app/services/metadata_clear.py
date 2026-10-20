"""Service-layer implementation of "clear one paper's metadata".

Scope agreed with the owner (2026-10-20): the paper row and its content (chunks,
index documents, stored PDF) stay untouched. What goes is the *metadata layer*:

* every ``paper_field_provenance`` claim (including tag claims),
* every ``paper_source`` record of the paper,
* every ``paper_identifier`` row (so the dedupe fingerprint is released and the
  paper falls back to the title/content ladder),
* the ``paper_authors`` and ``papers_tags`` links,
* the metadata columns on ``papers`` itself.

``title`` is NOT NULL, so it is cleared to the empty string -- the paper then has
no title until the metadata is imported again, which is exactly what "clear the
metadata, re-import later" means.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import (
    Paper,
    PaperAuthor,
    PaperFieldProvenance,
    PaperIdentifier,
    PaperSource,
    PapersTag,
)
from app.services import metadata_identifiers as identifiers
from app.services import paper_service

logger = get_logger(__name__)

#: ``papers`` metadata columns cleared by :func:`clear_paper_metadata`.
#: ``status`` / ``fingerprint`` / ``parser_backend`` / ``embedding_*`` are NOT
#: metadata: they describe the paper row and its content, not its bibliography.
METADATA_COLUMNS: tuple[str, ...] = (
    "title",
    "abstract",
    "language",
    "year",
    "doi",
    "arxiv_id",
    "url",
    "venue_id",
    "venue_edition_id",
    "venue_year",
    "volume",
    "issue",
    "pages",
    "publication_date",
    "paper_type",
)


@dataclass
class MetadataClearPreview:
    """What clearing one paper's metadata would remove."""

    paper_id: str
    title: str
    provenance_rows: int = 0
    source_rows: int = 0
    identifier_rows: int = 0
    author_links: int = 0
    tag_links: int = 0


@dataclass
class MetadataClearOutcome:
    """What clearing one paper's metadata actually removed."""

    paper_id: str
    provenance_rows: int = 0
    source_rows: int = 0
    identifier_rows: int = 0
    author_links: int = 0
    tag_links: int = 0
    fingerprint: str = ""
    cleared_fields: list[str] = dataclass_field(default_factory=list)
    #: Index documents whose ``title`` was rewritten (0 when nothing was indexed).
    index_documents: int = 0


def preview_clear(session: Session, paper: Paper) -> MetadataClearPreview:
    """Count the rows a clear would delete (read-only)."""

    def rows(model: Any) -> int:
        return len(session.execute(select(model).where(model.paper_id == paper.id)).all())

    return MetadataClearPreview(
        paper_id=paper.id,
        title=paper.title,
        provenance_rows=rows(PaperFieldProvenance),
        source_rows=rows(PaperSource),
        identifier_rows=rows(PaperIdentifier),
        author_links=rows(PaperAuthor),
        tag_links=rows(PapersTag),
    )


def clear_metadata(
    session: Session, paper: Paper, *, commit: bool = True
) -> MetadataClearOutcome:
    """Remove every piece of metadata of ``paper``; keep the paper and content.

    Order matters: claims reference ``paper_sources`` and ``paper_identifiers``
    through ``SET NULL`` foreign keys, so the provenance rows go first, then the
    sources and identifiers. Links and columns come last.
    """
    outcome = MetadataClearOutcome(paper_id=paper.id)

    # 1. Field claims (also carries the tag:* claims).
    result = session.execute(
        delete(PaperFieldProvenance).where(PaperFieldProvenance.paper_id == paper.id)
    )
    outcome.provenance_rows = int(result.rowcount or 0)

    # 2. Author / tag links.
    result = session.execute(
        delete(PaperAuthor).where(PaperAuthor.paper_id == paper.id)
    )
    outcome.author_links = int(result.rowcount or 0)
    result = session.execute(delete(PapersTag).where(PapersTag.paper_id == paper.id))
    outcome.tag_links = int(result.rowcount or 0)

    # 3. Sources, then identifiers (identifiers.first_source_id -> SET NULL).
    result = session.execute(
        delete(PaperSource).where(PaperSource.paper_id == paper.id)
    )
    outcome.source_rows = int(result.rowcount or 0)
    result = session.execute(
        delete(PaperIdentifier).where(PaperIdentifier.paper_id == paper.id)
    )
    outcome.identifier_rows = int(result.rowcount or 0)

    # 4. The columns themselves. ``title`` is NOT NULL -> empty string.
    for column in METADATA_COLUMNS:
        if not hasattr(paper, column):
            continue
        setattr(paper, column, "" if column == "title" else None)
        outcome.cleared_fields.append(column)

    session.flush()

    # 5. Re-derive the fingerprint: with no identifiers it falls back to the
    #    title/author/sha256 ladder, i.e. "this document's content".
    identifiers.refresh_primary(session, paper.id)
    fingerprint, _conflict = identifiers.upgrade_fingerprint(session, paper)
    outcome.fingerprint = fingerprint

    # 6. Collect the chunk ids *before* committing -- after the commit the
    #    session is expired and a lazy walk would re-query anyway.
    from app.db.models import PaperChunk

    chunk_ids = [
        str(row[0])
        for row in session.execute(
            select(PaperChunk.id).where(PaperChunk.paper_id == paper.id)
        ).all()
    ]
    blank_title = paper.title

    if commit:
        session.commit()

    # 7. Push the blanked title into the index *after* the database is committed:
    #    if the commit had failed, an index that already said "" would disagree
    #    with a row that still has the old title. Only the ``title`` field is
    #    rewritten (chunk text and embedding are untouched -- no re-embedding);
    #    a failure here is logged, not raised: the database is the source of truth
    #    and the next reindex/refresh reconciles the index.
    try:
        from app.search import opensearch

        if chunk_ids:
            outcome.index_documents = opensearch.bulk_update_documents(
                [{"chunk_id": cid, "doc": {"title": blank_title}} for cid in chunk_ids]
            )["updated"]
    except Exception as exc:  # noqa: BLE001 - see above
        logger.warning(
            "index title refresh failed for %s: %s",
            paper.id,
            exc,
            extra={"extra_fields": {"paper_id": paper.id}},
        )

    logger.info(
        "paper metadata cleared",
        extra={
            "extra_fields": {
                "paper_id": paper.id,
                "provenance_rows": outcome.provenance_rows,
                "source_rows": outcome.source_rows,
                "identifier_rows": outcome.identifier_rows,
                "author_links": outcome.author_links,
                "tag_links": outcome.tag_links,
                "fingerprint": outcome.fingerprint,
            }
        },
    )
    return outcome
