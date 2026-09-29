"""paper parser stamp

Records which parser produced the chunks that are indexed for a paper, so a
library that mixes docling-parsed and pypdf-parsed papers stays visible instead
of silent (plan §6.1 step 2).

``NULL`` means "unknown": rows indexed before the stamp existed, which is not the
same as "parsed by the wrong backend". Backend counts and per-paper mismatches
against the index documents are reported by ``GET /api/consistency``.

Revision ID: 81a04251abfa
Revises: c3f1a7d94e02
Create Date: 2026-09-29 20:30:13.031686
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '81a04251abfa'
down_revision: str | None = 'c3f1a7d94e02'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('papers', sa.Column('parser_backend', sa.String(length=16), nullable=True))
    op.add_column('papers', sa.Column('parser_version', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_papers_parser_backend'), 'papers', ['parser_backend'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_papers_parser_backend'), table_name='papers')
    op.drop_column('papers', 'parser_version')
    op.drop_column('papers', 'parser_backend')
