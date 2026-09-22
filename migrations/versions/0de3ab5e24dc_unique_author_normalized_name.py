"""unique author normalized name

``paper_service.get_or_create_author`` looked an author up with
``scalar_one_or_none()`` while ``authors.normalized_name`` carried only a plain
index. As soon as two rows share a normalized name (two sources spelling the same
person differently, a bulk import, a re-run of the metadata stage) that lookup
raises ``MultipleResultsFound`` and the whole paper fails to import.

This migration makes the invariant explicit - one row per normalized name - and
merges duplicates that already exist instead of aborting: every ``paper_authors``
link of a duplicate is repointed at the surviving row (the oldest one) and the
duplicate is deleted. ``uq_paper_authors_paper_author`` allows one link per
(paper, author), so a paper that linked both duplicates keeps the survivor's link.

Revision ID: 0de3ab5e24dc
Revises: 7a2f4c9d51be
Create Date: 2026-09-22 16:44:53.655280
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0de3ab5e24dc'
down_revision: str | None = '7a2f4c9d51be'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Normalized names carried by more than one row.
_DUPLICATES = sa.text(
    """
    SELECT normalized_name
    FROM authors
    GROUP BY normalized_name
    HAVING count(*) > 1
    """
)

#: The survivor of each group: the oldest row, ties broken by id.
_KEEPERS = sa.text(
    """
    SELECT DISTINCT ON (normalized_name) normalized_name, id
    FROM authors
    ORDER BY normalized_name, created_at, id
    """
)

_LOSER_LINKS = """
    SELECT id FROM authors WHERE normalized_name = :name AND id <> :keeper
"""


def _merge_duplicate_authors(bind) -> None:
    """Repoint every link at the surviving author row, then drop the extras."""
    names = [row[0] for row in bind.execute(_DUPLICATES)]
    if not names:
        return
    keepers = {row[0]: row[1] for row in bind.execute(_KEEPERS)}
    for name in names:
        keeper = keepers[name]
        params = {"name": name, "keeper": keeper}
        # Drop the loser's link where the paper already links the survivor.
        bind.execute(
            sa.text(
                f"""
                DELETE FROM paper_authors
                WHERE author_id IN ({_LOSER_LINKS})
                AND paper_id IN (
                    SELECT paper_id FROM paper_authors WHERE author_id = :keeper
                )
                """
            ),
            params,
        )
        bind.execute(
            sa.text(
                f"UPDATE paper_authors SET author_id = :keeper "
                f"WHERE author_id IN ({_LOSER_LINKS})"
            ),
            params,
        )
        bind.execute(
            sa.text(
                "DELETE FROM authors WHERE normalized_name = :name AND id <> :keeper"
            ),
            params,
        )


def upgrade() -> None:
    _merge_duplicate_authors(op.get_bind())
    op.create_unique_constraint(
        'uq_authors_normalized_name', 'authors', ['normalized_name']
    )


def downgrade() -> None:
    op.drop_constraint('uq_authors_normalized_name', 'authors', type_='unique')
