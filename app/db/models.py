"""ORM models for paperbox (plan section 5).

Nine tables form the minimum viable model:

    papers, authors, venues, paper_authors, paper_tags, papers_tags,
    paper_files, paper_chunks, ingestion_jobs

The multi-source metadata layer (docs/architecture/metadata-architecture.md) adds four more:

    paper_sources, paper_identifiers, paper_field_provenance, venue_editions

so a paper is now ``papers <- paper_sources <- paper_field_provenance`` with
``paper_identifiers`` as the dedupe skeleton and venues split into
``venues`` (the entity) + ``venue_editions`` (one row per year).

Conventions:
* paper primary keys are UUID strings (they appear verbatim in API paths),
* ``papers.fingerprint`` is unique and drives deduplication,
* deletion is soft: ``papers.deleted_at`` /
  ``paper_chunks.deleted_at`` mark records as gone while keeping them for
  auditing (plan section 23),
* timestamps are ``timestamptz`` with server defaults,
* the caller sets ``created_at``/``updated_at`` on update (no DB triggers).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base shared by every model and by Alembic."""


def new_uuid() -> str:
    """Generate a new UUID primary key value (string form)."""
    return str(uuid.uuid4())


class TimestampMixin:
    """``created_at`` / ``updated_at`` columns backed by server defaults."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# --------------------------------------------------------------------------- #
# papers
# --------------------------------------------------------------------------- #
class Paper(TimestampMixin, Base):
    __tablename__ = "papers"
    __table_args__ = (
        Index("ix_papers_doi", "doi"),
        Index("ix_papers_arxiv_id", "arxiv_id"),
        Index("ix_papers_year", "year"),
        Index("ix_papers_status", "status"),
        Index("ix_papers_created_at", "created_at"),
        Index("ix_papers_venue_year", "venue_year"),
        # Only live papers claim a fingerprint: deleting a paper releases it so the
        # same document can be ingested again (plan sections 5.1 and 23). The
        # deleted row keeps its value for audit/recovery.
        Index(
            "uq_papers_fingerprint_live",
            "fingerprint",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    external_id: Mapped[str | None] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(32))
    year: Mapped[int | None] = mapped_column(Integer)

    doi: Mapped[str | None] = mapped_column(String(255))
    arxiv_id: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(Text)
    venue_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("venues.id", ondelete="SET NULL")
    )
    #: Bibliographic detail of the merged current value (docs/architecture/metadata-architecture.md
    #: section 3.5). ``venue_year`` is a redundant copy of ``venue_editions.year``
    #: so "venue + year" can be filtered without a join.
    volume: Mapped[str | None] = mapped_column(String(32))
    issue: Mapped[str | None] = mapped_column(String(32))
    pages: Mapped[str | None] = mapped_column(String(64))
    publication_date: Mapped[date | None] = mapped_column(Date)
    paper_type: Mapped[str | None] = mapped_column(String(32))
    venue_edition_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("venue_editions.id", ondelete="SET NULL")
    )
    venue_year: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)

    #: Which parser produced the chunks that are indexed for this paper, and the
    #: version string that parser reported (plan §6.1 step 2). ``NULL`` means the
    #: paper was indexed before the stamp existed -- unknown, not "wrong".
    #: Reindexing refreshes both; ``GET /api/consistency`` compares them against
    #: the index documents so a mixed library is visible instead of silent.
    parser_backend: Mapped[str | None] = mapped_column(String(16), index=True)
    parser_version: Mapped[str | None] = mapped_column(String(64))

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    venue: Mapped["Venue | None"] = relationship(back_populates="papers")
    venue_edition: Mapped["VenueEdition | None"] = relationship(back_populates="papers")
    sources: Mapped[list["PaperSource"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    identifiers: Mapped[list["PaperIdentifier"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    field_provenance: Mapped[list["PaperFieldProvenance"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    paper_authors: Mapped[list["PaperAuthor"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    paper_tags: Mapped[list["PaperTag"]] = relationship(
        secondary="papers_tags",
        back_populates="papers",
        viewonly=True,
    )
    tag_links: Mapped[list["PapersTag"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    files: Mapped[list["PaperFile"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    chunks: Mapped[list["PaperChunk"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    #: Degraded-but-usable results per stage (T7.3); ``paper_degradations``.
    degradations: Mapped[list["PaperDegradation"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    ingestion_jobs: Mapped[list["IngestionJob"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Paper id={self.id} title={self.title!r} status={self.status}>"


# --------------------------------------------------------------------------- #
# authors
# --------------------------------------------------------------------------- #
class Author(TimestampMixin, Base):
    __tablename__ = "authors"
    __table_args__ = (
        # One row per normalized name: ``get_or_create_author`` looks an author up
        # by it, and two rows would make that lookup ambiguous (migration
        # ``0de3ab5e24dc`` merges the duplicates that existed before the index).
        UniqueConstraint("normalized_name", name="uq_authors_normalized_name"),
        Index("ix_authors_name", "name"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    orcid: Mapped[str | None] = mapped_column(String(64))
    affiliation: Mapped[str | None] = mapped_column(Text)

    paper_authors: Mapped[list["PaperAuthor"]] = relationship(
        back_populates="author", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Author id={self.id} name={self.name!r}>"


# --------------------------------------------------------------------------- #
# venues
# --------------------------------------------------------------------------- #
class Venue(TimestampMixin, Base):
    __tablename__ = "venues"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(512), nullable=False, unique=True
    )
    kind: Mapped[str | None] = mapped_column(String(32))
    publisher: Mapped[str | None] = mapped_column(String(255))
    #: ISSN printed by the source (IEEE publishes it per journal/collection).
    issn: Mapped[str | None] = mapped_column(String(64))

    papers: Mapped[list["Paper"]] = relationship(back_populates="venue")
    editions: Mapped[list["VenueEdition"]] = relationship(
        back_populates="venue", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Venue id={self.id} name={self.name!r}>"


# --------------------------------------------------------------------------- #
# paper_authors (association object: papers <-> authors)
# --------------------------------------------------------------------------- #
class PaperAuthor(Base):
    __tablename__ = "paper_authors"
    __table_args__ = (
        UniqueConstraint("paper_id", "author_id", name="uq_paper_authors_paper_author"),
        UniqueConstraint("paper_id", "author_order", name="uq_paper_authors_order"),
        Index("ix_paper_authors_author_id", "author_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    paper_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("authors.id", ondelete="CASCADE"), nullable=False
    )
    author_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_corresponding: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    paper: Mapped["Paper"] = relationship(back_populates="paper_authors")
    author: Mapped["Author"] = relationship(back_populates="paper_authors")


# --------------------------------------------------------------------------- #
# paper_tags
# --------------------------------------------------------------------------- #
class PaperTag(TimestampMixin, Base):
    __tablename__ = "paper_tags"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )

    paper_tags: Mapped[list["PapersTag"]] = relationship(
        back_populates="tag", cascade="all, delete-orphan"
    )
    papers: Mapped[list["Paper"]] = relationship(
        secondary="papers_tags",
        viewonly=True,
        back_populates="paper_tags",
    )


# --------------------------------------------------------------------------- #
# papers_tags (join table: papers <-> paper_tags)
# --------------------------------------------------------------------------- #
class PapersTag(Base):
    __tablename__ = "papers_tags"
    __table_args__ = (
        UniqueConstraint("paper_id", "tag_id", name="uq_papers_tags_paper_tag"),
        Index("ix_papers_tags_tag_id", "tag_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    paper_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    tag_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("paper_tags.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Which flavour of tag this link is: ``ieee_terms`` / ``author_terms`` /
    #: ``dynamic_index_terms`` / ``source_tag`` (decision 10). Existing links
    #: default to the catch-all ``source_tag``.
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'source_tag'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    paper: Mapped["Paper"] = relationship(back_populates="tag_links")
    tag: Mapped["PaperTag"] = relationship(
        back_populates="paper_tags", foreign_keys=[tag_id]
    )


# --------------------------------------------------------------------------- #
# paper_files
# --------------------------------------------------------------------------- #
class PaperFile(TimestampMixin, Base):
    __tablename__ = "paper_files"
    __table_args__ = (
        Index("ix_paper_files_paper_id", "paper_id"),
        Index("ix_paper_files_sha256", "sha256"),
        # Exactly one live primary version per paper: the only file that is
        # parsed, chunked and indexed (section 7.1 of the plan).
        Index(
            "uq_paper_files_primary",
            "paper_id",
            unique=True,
            postgresql_where=text("is_primary AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    paper_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    #: Which source record brought this PDF (arXiv preprint vs. published version).
    source_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("paper_sources.id", ondelete="SET NULL")
    )
    #: ``original`` / ``arxiv_pdf`` / ``published_pdf`` / ``supplement``.
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'original'")
    )
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    paper: Mapped["Paper"] = relationship(back_populates="files")
    source: Mapped["PaperSource | None"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<PaperFile id={self.id} key={self.object_key!r}>"


# --------------------------------------------------------------------------- #
# paper_chunks
# --------------------------------------------------------------------------- #
class PaperChunk(TimestampMixin, Base):
    __tablename__ = "paper_chunks"
    __table_args__ = (
        UniqueConstraint("paper_id", "chunk_index", name="uq_paper_chunks_paper_index"),
        CheckConstraint(
            "page_end IS NULL OR page_start IS NULL OR page_end >= page_start",
            name="ck_paper_chunks_page_range",
        ),
        Index("ix_paper_chunks_paper_id", "paper_id"),
        Index("ix_paper_chunks_paper_section", "paper_id", "section"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    paper_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    #: ``Text``, not ``String(255)``: a section title comes from the PDF, and a
    #: parser can emit a "heading" that is really the title *plus* the author
    #: block (docling does this for the first H1). 2026-09-30: two papers failed
    #: their whole import on ``value too long for type character varying(255)``
    #: before this was widened; the markdown adapter now also refuses to treat
    #: such a line as a heading (``markdown.MAX_SECTION_TITLE_CHARS``).
    section: Mapped[str | None] = mapped_column(Text)
    subsection: Mapped[str | None] = mapped_column(Text)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer)
    char_count: Mapped[int | None] = mapped_column(Integer)

    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    doc_metadata: Mapped[dict | None] = mapped_column(JSONB)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    paper: Mapped["Paper"] = relationship(back_populates="chunks")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<PaperChunk paper_id={self.paper_id} index={self.chunk_index}>"


# --------------------------------------------------------------------------- #
# ingestion_jobs (state machine: plan section 14)
# --------------------------------------------------------------------------- #
class IngestionJob(TimestampMixin, Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        Index("ix_ingestion_jobs_paper_id", "paper_id"),
        Index("ix_ingestion_jobs_stage", "stage"),
        Index("ix_ingestion_jobs_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    paper_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("papers.id", ondelete="SET NULL")
    )

    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'ingest'")
    )
    stage: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'received'")
    )
    progress: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0")
    )
    #: Stable failure code from ``app.core.errors`` (SPEC-P1 section A2);
    #: ``NULL`` while the job has not failed.
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    paper: Mapped["Paper | None"] = relationship(back_populates="ingestion_jobs")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<IngestionJob id={self.id} stage={self.stage} progress={self.progress}>"


# --------------------------------------------------------------------------- #
# paper_sources (where a description of the paper came from)
# --------------------------------------------------------------------------- #
class PaperSource(Base):
    """One description of a paper: IEEE JSON, arXiv API, PDF embed, manual...

    ``paper_id`` is nullable on purpose -- a record that could not be matched yet
    still lands here (``match_status='pending'``/``'ambiguous'``) so a later PDF
    upload or a human decision can attach it. ``UNIQUE(source_type, source_ref)``
    makes a repeated import idempotent, and ``raw`` keeps the payload verbatim so
    a future parser can be replayed without re-fetching the source.
    """

    __tablename__ = "paper_sources"
    __table_args__ = (
        UniqueConstraint(
            "source_type", "source_ref", name="uq_paper_sources_type_ref"
        ),
        Index("ix_paper_sources_paper_id", "paper_id"),
        Index("ix_paper_sources_match_status", "match_status"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    paper_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("papers.id", ondelete="CASCADE")
    )
    #: ``ieee_api`` / ``arxiv_api`` / ``crossref`` / ``pdf_embedded`` /
    #: ``pdf_heuristic`` / ``import_file`` / ``manual``.
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Stable key inside the source: ``doi:...``, ``arxiv:...``, ``ieee:7065247``,
    #: ``file:<abspath>:<sha256>``, ``paper:<uuid>:heuristic``.
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(32))
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    match_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    match_method: Mapped[str | None] = mapped_column(String(32))
    match_confidence: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    importer: Mapped[str | None] = mapped_column(String(128))

    paper: Mapped["Paper | None"] = relationship(back_populates="sources")
    field_provenance: Mapped[list["PaperFieldProvenance"]] = relationship(
        back_populates="source"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<PaperSource type={self.source_type} ref={self.source_ref!r} "
            f"status={self.match_status}>"
        )


# --------------------------------------------------------------------------- #
# paper_identifiers (dedupe skeleton)
# --------------------------------------------------------------------------- #
class PaperIdentifier(Base):
    """One normalized identifier of a paper (DOI, arXiv id, IEEE article number...).

    The partial unique index ``uq_paper_identifiers_scheme_value`` is the dedupe
    floor: one identifier belongs to exactly one paper, so two sources quoting the
    same DOI cannot end up on two rows. ``is_primary`` marks the identifier that
    feeds ``papers.fingerprint``.
    """

    __tablename__ = "paper_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "paper_id",
            "scheme",
            "normalized_value",
            name="uq_paper_identifiers_paper_scheme_value",
        ),
        Index(
            "uq_paper_identifiers_scheme_value",
            "scheme",
            "normalized_value",
            unique=True,
            postgresql_where=text("paper_id IS NOT NULL"),
        ),
        Index("ix_paper_identifiers_paper_id", "paper_id"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    paper_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    #: ``doi`` / ``arxiv`` / ``ieee_article_number`` / ``issn`` / ``isbn`` /
    #: ``pmid`` / ``openalex`` / ``semantic_scholar`` / ``url`` / ``sha256``.
    scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)
    first_source_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("paper_sources.id", ondelete="SET NULL")
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    paper: Mapped["Paper"] = relationship(back_populates="identifiers")
    first_source: Mapped["PaperSource | None"] = relationship()
    provenance: Mapped[list["PaperFieldProvenance"]] = relationship(
        back_populates="identifier", foreign_keys="PaperFieldProvenance.identifier_id"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<PaperIdentifier scheme={self.scheme} value={self.value!r}>"


# --------------------------------------------------------------------------- #
# paper_field_provenance (who said what, when, and whether it still counts)
# --------------------------------------------------------------------------- #
class PaperFieldProvenance(Base):
    """One field-level claim about a paper.

    History is append-only: a new value flips the previous current row to
    ``is_current=false`` instead of deleting it, which is what makes a rollback
    (section 6 of the plan) and "who overwrote whom" answerable at all.
    """

    __tablename__ = "paper_field_provenance"
    __table_args__ = (
        Index(
            "uq_paper_field_provenance_current",
            "paper_id",
            "field",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_paper_field_provenance_paper_field", "paper_id", "field"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    paper_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    #: ``NULL`` = system/human decision without a source record.
    source_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("paper_sources.id", ondelete="SET NULL")
    )
    #: ``title`` / ``abstract`` / ``year`` / ``venue`` / ``volume`` / ``issue`` /
    #: ``pages`` / ``authors`` / ``publication_date`` / ``paper_type`` /
    #: ``identifier:doi`` / ``tag:ieee_terms`` / ...
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[object] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: ``initial`` / ``structured_override`` / ``manual``.
    decided_by: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'initial'")
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Set when the claim is about a specific identifier row (``identifier:doi``).
    identifier_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("paper_identifiers.id", ondelete="SET NULL")
    )

    paper: Mapped["Paper"] = relationship(back_populates="field_provenance")
    source: Mapped["PaperSource | None"] = relationship(back_populates="field_provenance")
    identifier: Mapped["PaperIdentifier | None"] = relationship(
        back_populates="provenance", foreign_keys=[identifier_id]
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<PaperFieldProvenance field={self.field} current={self.is_current} "
            f"by={self.decided_by}>"
        )


# --------------------------------------------------------------------------- #
# paper_degradations (a stage that gave up on something, but the paper is usable)
# --------------------------------------------------------------------------- #
class PaperDegradation(TimestampMixin, Base):
    """One reason a stage produced a thinner result than it could have.

    Degrading is never an error: a docling outage falls back to pypdf, a failed
    embedding call falls back to length chunking, and the paper is indexed
    anyway. Without a row here that decision would exist only in the log, so
    nobody could tell which papers deserve a re-run once the missing service is
    back (plan T7.3).

    ``(paper_id, stage, code)`` is unique: re-running the pipeline bumps
    ``occurrences``/``last_seen_at`` instead of piling up rows, and a stage that
    comes back clean stamps ``resolved_at`` -- the row stays as the audit trail,
    the paper stops being selected by ``reindex --degraded``.
    """

    __tablename__ = "paper_degradations"
    __table_args__ = (
        UniqueConstraint(
            "paper_id", "stage", "code", name="uq_paper_degradations_paper_stage_code"
        ),
        Index("ix_paper_degradations_paper_id", "paper_id"),
        Index("ix_paper_degradations_stage", "stage"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    paper_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    #: ``parsing`` / ``chunking`` / ``embedding`` / ``indexing`` -- the stage
    #: vocabulary lives in ``app.services.degradation_service``.
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Machine-readable cause, e.g. ``docling_unavailable`` / ``semantic_fallback``.
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Whatever the stage knows: section label, error text, counts, backend.
    detail: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    occurrences: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: ``NULL`` = still true of the current chunks; set when a later run of the
    #: same stage no longer reported this code.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The job that last reported it (``NULL`` for reindex/backfill scripts).
    job_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("ingestion_jobs.id", ondelete="SET NULL")
    )

    paper: Mapped["Paper"] = relationship(back_populates="degradations")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<PaperDegradation paper_id={self.paper_id} stage={self.stage} "
            f"code={self.code} open={self.resolved_at is None}>"
        )


# --------------------------------------------------------------------------- #
# venue_editions (one year of a venue)
# --------------------------------------------------------------------------- #
class VenueEdition(Base):
    """A single year of a venue: the granularity decisions 7 asks for.

    Searching "the conference" matches :class:`Venue`; searching "the conference
    + 2015" matches this row (``papers.venue_year`` mirrors ``year`` so the
    filter needs no join). The year is never glued into the venue name.
    """

    __tablename__ = "venue_editions"
    __table_args__ = (
        UniqueConstraint("venue_id", "year", name="uq_venue_editions_venue_year"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    venue_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("venues.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    location: Mapped[str | None] = mapped_column(Text)
    dates: Mapped[str | None] = mapped_column(Text)
    publication_number: Mapped[str | None] = mapped_column(String(64))
    is_number: Mapped[str | None] = mapped_column(String(64))

    venue: Mapped["Venue"] = relationship(back_populates="editions")
    papers: Mapped[list["Paper"]] = relationship(back_populates="venue_edition")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<VenueEdition venue_id={self.venue_id} year={self.year}>"


class SearchQuery(Base):
    """One logged ``POST /api/search`` call (SPEC-P1 section B).

    Append-only telemetry: it records what was asked, how it was answered and
    how long it took, so Bad Cases can be inspected later. Writes are best
    effort -- a logging failure must never break a search.
    """

    __tablename__ = "search_queries"
    __table_args__ = (
        Index("ix_search_queries_created_at", "created_at"),
        Index("ix_search_queries_mode", "mode"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    request_id: Mapped[str | None] = mapped_column(String(64))

    query: Mapped[str] = mapped_column(Text, nullable=False)
    #: English search expression the query was rewritten into, when that ran
    #: (SPEC-P1 section I1); ``NULL`` when no rewrite happened.
    rewritten_query: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    rerank: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    filters: Mapped[dict | None] = mapped_column(JSONB)
    #: 喂给论文聚合的 chunk 数（2026-09-30 前记的是「请求时的估算池」= top_k×5，
    #: 那是请求值而非实际值；现在记 ``SearchOutcome.candidates``）。
    candidates: Mapped[int | None] = mapped_column(Integer)
    returned: Mapped[int] = mapped_column(Integer, nullable=False)
    took_ms: Mapped[int | None] = mapped_column(Integer)
    results: Mapped[list | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<SearchQuery id={self.id} mode={self.mode} query={self.query!r}>"


__all__ = [
    "Base",
    "Author",
    "IngestionJob",
    "Paper",
    "PaperAuthor",
    "PaperChunk",
    "PaperFieldProvenance",
    "PaperFile",
    "PaperIdentifier",
    "PaperSource",
    "PaperTag",
    "SearchQuery",
    "PapersTag",
    "Venue",
    "VenueEdition",
    "new_uuid",
]
