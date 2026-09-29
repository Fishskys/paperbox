"""metadata multi-source storage

Adds the multi-source metadata layer described in ``docs/architecture/metadata-architecture.md``
and ``.hermes/plans/2026-09-21_201622-metadata-discovery-storage.md``:

* four new tables -- ``paper_sources`` (verbatim source snapshots),
  ``paper_identifiers`` (dedupe skeleton), ``paper_field_provenance``
  (field-level ledger) and ``venue_editions`` (venue + year apart);
* bibliographic columns on ``papers`` (volume/issue/pages/publication_date/
  paper_type/venue_edition_id/venue_year);
* ``paper_files.source_id``/``is_primary`` (+ the "one live primary per paper"
  partial unique index);
* ``papers_tags.kind`` (IEEE terms vs. author terms vs. dynamic index terms);
* ``venues.issn``.

Purely additive: no column is dropped or rewritten, so existing rows keep their
values and ``papers.fingerprint`` is untouched.

Revision ID: 7a2f4c9d51be
Revises: 5b4d5f6d215d
Create Date: 2026-09-21 20:31:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "7a2f4c9d51be"
down_revision: str | None = "5b4d5f6d215d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- paper_sources ------------------------------------------------------
    op.create_table(
        "paper_sources",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("paper_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=True),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "match_status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("match_method", sa.String(length=32), nullable=True),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("importer", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_type", "source_ref", name="uq_paper_sources_type_ref"),
    )
    op.create_index("ix_paper_sources_paper_id", "paper_sources", ["paper_id"])
    op.create_index("ix_paper_sources_match_status", "paper_sources", ["match_status"])

    # --- paper_identifiers --------------------------------------------------
    op.create_table(
        "paper_identifiers",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("paper_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("scheme", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("normalized_value", sa.Text(), nullable=False),
        sa.Column("first_source_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column(
            "is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["first_source_id"], ["paper_sources.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "paper_id",
            "scheme",
            "normalized_value",
            name="uq_paper_identifiers_paper_scheme_value",
        ),
    )
    op.create_index("ix_paper_identifiers_paper_id", "paper_identifiers", ["paper_id"])
    op.create_index(
        "uq_paper_identifiers_scheme_value",
        "paper_identifiers",
        ["scheme", "normalized_value"],
        unique=True,
        postgresql_where=sa.text("paper_id IS NOT NULL"),
    )

    # --- paper_field_provenance --------------------------------------------
    op.create_table(
        "paper_field_provenance",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("paper_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("source_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "is_current", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "decided_by",
            sa.String(length=32),
            server_default=sa.text("'initial'"),
            nullable=False,
        ),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("identifier_id", sa.UUID(as_uuid=False), nullable=True),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["paper_sources.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["identifier_id"], ["paper_identifiers.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_paper_field_provenance_paper_field",
        "paper_field_provenance",
        ["paper_id", "field"],
    )
    op.create_index(
        "uq_paper_field_provenance_current",
        "paper_field_provenance",
        ["paper_id", "field"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    # --- venue_editions -----------------------------------------------------
    op.create_table(
        "venue_editions",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("venue_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("dates", sa.Text(), nullable=True),
        sa.Column("publication_number", sa.String(length=64), nullable=True),
        sa.Column("is_number", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["venue_id"], ["venues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("venue_id", "year", name="uq_venue_editions_venue_year"),
    )

    # --- additions to existing tables --------------------------------------
    op.add_column("venues", sa.Column("issn", sa.String(length=64), nullable=True))

    op.add_column("papers", sa.Column("volume", sa.String(length=32), nullable=True))
    op.add_column("papers", sa.Column("issue", sa.String(length=32), nullable=True))
    op.add_column("papers", sa.Column("pages", sa.String(length=64), nullable=True))
    op.add_column("papers", sa.Column("publication_date", sa.Date(), nullable=True))
    op.add_column("papers", sa.Column("paper_type", sa.String(length=32), nullable=True))
    op.add_column(
        "papers", sa.Column("venue_edition_id", sa.UUID(as_uuid=False), nullable=True)
    )
    op.add_column("papers", sa.Column("venue_year", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_papers_venue_edition_id",
        "papers",
        "venue_editions",
        ["venue_edition_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_papers_venue_year", "papers", ["venue_year"])

    op.add_column(
        "paper_files", sa.Column("source_id", sa.UUID(as_uuid=False), nullable=True)
    )
    op.add_column(
        "paper_files",
        sa.Column(
            "is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.create_foreign_key(
        "fk_paper_files_source_id",
        "paper_files",
        "paper_sources",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_paper_files_primary",
        "paper_files",
        ["paper_id"],
        unique=True,
        postgresql_where=sa.text("is_primary AND deleted_at IS NULL"),
    )

    op.add_column(
        "papers_tags",
        sa.Column(
            "kind",
            sa.String(length=32),
            server_default=sa.text("'source_tag'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("papers_tags", "kind")

    op.drop_index("uq_paper_files_primary", table_name="paper_files")
    op.drop_constraint("fk_paper_files_source_id", "paper_files", type_="foreignkey")
    op.drop_column("paper_files", "is_primary")
    op.drop_column("paper_files", "source_id")

    op.drop_index("ix_papers_venue_year", table_name="papers")
    op.drop_constraint("fk_papers_venue_edition_id", "papers", type_="foreignkey")
    op.drop_column("papers", "venue_year")
    op.drop_column("papers", "venue_edition_id")
    op.drop_column("papers", "paper_type")
    op.drop_column("papers", "publication_date")
    op.drop_column("papers", "pages")
    op.drop_column("papers", "issue")
    op.drop_column("papers", "volume")

    op.drop_column("venues", "issn")

    op.drop_table("venue_editions")

    op.drop_index("uq_paper_field_provenance_current", table_name="paper_field_provenance")
    op.drop_index(
        "ix_paper_field_provenance_paper_field", table_name="paper_field_provenance"
    )
    op.drop_table("paper_field_provenance")

    op.drop_index("uq_paper_identifiers_scheme_value", table_name="paper_identifiers")
    op.drop_index("ix_paper_identifiers_paper_id", table_name="paper_identifiers")
    op.drop_table("paper_identifiers")

    op.drop_index("ix_paper_sources_match_status", table_name="paper_sources")
    op.drop_index("ix_paper_sources_paper_id", table_name="paper_sources")
    op.drop_table("paper_sources")