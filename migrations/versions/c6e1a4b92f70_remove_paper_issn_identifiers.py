"""remove paper-level ISSN identifiers

ISSN identifies a serial publication, not an individual paper. The globally
unique ``paper_identifiers`` table therefore made one article monopolize an ISSN
that every article in the same venue should be able to reuse. Venue rows already
carry the value in ``venues.issn``; this migration removes only the invalid
paper-level claims and their ledger history.

Revision ID: c6e1a4b92f70
Revises: 8b4f0c2d6a17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c6e1a4b92f70"
down_revision: str | None = "8b4f0c2d6a17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    provenance = connection.execute(
        sa.text("delete from paper_field_provenance where field = 'identifier:issn'")
    ).rowcount
    identifiers = connection.execute(
        sa.text("delete from paper_identifiers where scheme = 'issn'")
    ).rowcount
    print(
        "metadata: removed "
        f"{identifiers} paper-level ISSN identifier(s) and "
        f"{provenance} provenance claim(s); venues.issn is unchanged",
        flush=True,
    )


def downgrade() -> None:
    # The removed rows were an invalid one-paper representation of reusable venue
    # metadata. Recreating an arbitrary owner would reintroduce the corruption.
    pass
