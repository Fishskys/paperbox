"""add search query log

Append-only telemetry for ``POST /api/search`` (SPEC-P1 section B): every search
records the query, mode, filters, candidate/returned counts, latency and the
per-paper result summary, so Bad Cases can be reviewed without re-running them.

Revision ID: e3e713268eeb
Revises: 661e7239b263
Create Date: 2026-09-12 17:48:01.393993
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e3e713268eeb'
down_revision: str | None = '661e7239b263'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_queries",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column(
            "rerank", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("candidates", sa.Integer(), nullable=True),
        sa.Column("returned", sa.Integer(), nullable=False),
        sa.Column("took_ms", sa.Integer(), nullable=True),
        sa.Column("results", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_search_queries_created_at", "search_queries", ["created_at"], unique=False
    )
    op.create_index("ix_search_queries_mode", "search_queries", ["mode"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_search_queries_mode", table_name="search_queries")
    op.drop_index("ix_search_queries_created_at", table_name="search_queries")
    op.drop_table("search_queries")
