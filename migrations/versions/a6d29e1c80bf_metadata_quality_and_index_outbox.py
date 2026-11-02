"""Persist PDF quality decisions and retryable metadata index updates."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "a6d29e1c80bf"
down_revision = "f69d4c8a932e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("paper_authors", sa.Column("display_name", sa.Text(), nullable=True))
    op.drop_constraint("uq_paper_authors_paper_author", "paper_authors", type_="unique")
    op.create_table("metadata_quality_decisions",
        sa.Column("id", pg.UUID(as_uuid=False), primary_key=True),
        sa.Column("paper_id", pg.UUID(as_uuid=False), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field", sa.String(64), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("fingerprint", name="uq_metadata_quality_fingerprint"))
    op.create_index("ix_metadata_quality_paper", "metadata_quality_decisions", ["paper_id", "created_at"])
    op.create_table("metadata_index_updates",
        sa.Column("paper_id", pg.UUID(as_uuid=False), sa.ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("revision", sa.String(36), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade():
    # Recreating the old constraint refuses duplicate bylines rather than losing them.
    op.create_unique_constraint("uq_paper_authors_paper_author", "paper_authors", ["paper_id", "author_id"])
    op.drop_column("paper_authors", "display_name")
    op.drop_table("metadata_index_updates")
    op.drop_index("ix_metadata_quality_paper", table_name="metadata_quality_decisions")
    op.drop_table("metadata_quality_decisions")
