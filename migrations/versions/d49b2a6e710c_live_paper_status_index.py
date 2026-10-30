"""Cover live paper status counts and stable newest-first pagination."""

from alembic import op
import sqlalchemy as sa

revision = "d49b2a6e710c"
down_revision = "f2a8c7d9e401"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_papers_live_status_created_id",
        "papers",
        ["status", sa.text("created_at DESC"), "id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_papers_live_status_created_id", table_name="papers")