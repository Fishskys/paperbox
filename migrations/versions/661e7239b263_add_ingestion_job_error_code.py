"""add ingestion job error_code

Structured failure attribution for ingestion jobs (SPEC-P1 section A2):
``app.core.errors.classify_failure`` maps a pipeline exception onto one of a
fixed set of codes, which the worker stores here. Nullable because only failed
jobs carry a code, and historical rows cannot be backfilled (their messages
were free text).

Revision ID: 661e7239b263
Revises: 7359b44a3938
Create Date: 2026-09-12 17:40:45.492759
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '661e7239b263'
down_revision: str | None = '7359b44a3938'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("error_code", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "error_code")
