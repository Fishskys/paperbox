"""Preserve source attribution review decisions without modifying raw records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "bc71d92a0f64"
down_revision = "a6d29e1c80bf"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("metadata_source_review_events",
        sa.Column("id", pg.UUID(as_uuid=False), primary_key=True),
        sa.Column("source_id", pg.UUID(as_uuid=False), sa.ForeignKey("paper_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_source_review_history", "metadata_source_review_events", ["source_id", "created_at"])


def downgrade():
    op.drop_index("ix_source_review_history", table_name="metadata_source_review_events")
    op.drop_table("metadata_source_review_events")
