"""ORM models for paperbox (plan section 5).

Nine tables form the minimum viable model:

    papers, authors, venues, paper_authors, paper_tags, papers_tags,
    paper_files, paper_chunks, ingestion_jobs

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
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    venue: Mapped["Venue | None"] = relationship(back_populates="papers")
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
    __table_args__ = (Index("ix_authors_name", "name"),)

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

    papers: Mapped[list["Paper"]] = relationship(back_populates="venue")

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
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=new_uuid
    )
    paper_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'original'")
    )
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    paper: Mapped["Paper"] = relationship(back_populates="files")

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
    section: Mapped[str | None] = mapped_column(String(255))
    subsection: Mapped[str | None] = mapped_column(String(255))

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
    "PaperFile",
    "PaperTag",
    "SearchQuery",
    "PapersTag",
    "Venue",
    "new_uuid",
]
