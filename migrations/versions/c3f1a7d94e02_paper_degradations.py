"""paper degradation ledger

Adds ``paper_degradations``: one row per ``(paper, stage, code)`` that records a
result which is *usable but thinner than it could have been* -- docling being
unreachable and falling back to pypdf, an embedding call failing and falling back
to length chunking (plan T7.3).

Why a table instead of a log line: re-running a paper once the missing service is
back is a normal operation, and it needs a queryable set ("which papers have an
open degradation?"). Why not a column on ``papers``: a paper can degrade in
several stages, and a later clean run must be able to resolve one without
touching the others.

The unique constraint ``(paper_id, stage, code)`` makes the writer idempotent:
re-running bumps ``occurrences``/``last_seen_at`` rather than piling up rows, and
``resolved_at`` marks a cause that a later run no longer reported (the row stays
as the audit trail).

Purely additive: nothing existing is dropped or rewritten.

Revision ID: c3f1a7d94e02
Revises: 0de3ab5e24dc
Create Date: 2026-09-30 10:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c3f1a7d94e02"
down_revision: str | None = "0de3ab5e24dc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_degradations",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("paper_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "occurrences", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["papers.id"],
            name="fk_paper_degradations_paper_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ingestion_jobs.id"],
            name="fk_paper_degradations_job_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "paper_id",
            "stage",
            "code",
            name="uq_paper_degradations_paper_stage_code",
        ),
    )
    op.create_index(
        "ix_paper_degradations_paper_id", "paper_degradations", ["paper_id"]
    )
    op.create_index("ix_paper_degradations_stage", "paper_degradations", ["stage"])


def downgrade() -> None:
    op.drop_index("ix_paper_degradations_stage", table_name="paper_degradations")
    op.drop_index("ix_paper_degradations_paper_id", table_name="paper_degradations")
    op.drop_table("paper_degradations")
