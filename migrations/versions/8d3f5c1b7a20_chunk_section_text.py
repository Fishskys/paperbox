"""chunk section/subsection widened to text

A chunk's ``section`` comes from the PDF, and a parser can hand over a "heading"
that is really the paper title plus the author block (docling merges them into
the first H1). Such a line is far longer than any real section title: on
2026-09-30 two papers failed their whole import with
``value too long for type character varying(255)`` on the ``paper_chunks``
INSERT. The columns become ``text`` (lossless) and the markdown adapter now
refuses to treat an over-long line as a heading
(``app/parsing/markdown.py::MAX_SECTION_TITLE_CHARS``).

Revision ID: 8d3f5c1b7a20
Revises: 81a04251abfa
Create Date: 2026-09-30 02:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '8d3f5c1b7a20'
down_revision: str | None = '81a04251abfa'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        'paper_chunks',
        'section',
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        'paper_chunks',
        'subsection',
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Values longer than 255 characters cannot survive the narrowing: the
    # downgrade truncates them, which is exactly the state this revision fixed.
    op.execute("UPDATE paper_chunks SET section = left(section, 255) WHERE length(section) > 255")
    op.execute(
        "UPDATE paper_chunks SET subsection = left(subsection, 255) "
        "WHERE length(subsection) > 255"
    )
    op.alter_column(
        'paper_chunks',
        'subsection',
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.alter_column(
        'paper_chunks',
        'section',
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
