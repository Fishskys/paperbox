"""Persistent cleanup candidates, receipts, ownership and serialization lock."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "c419a74e6310"
down_revision = "bc71d92a0f64"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("cleanup_receipts", sa.Column("id", pg.UUID(as_uuid=False), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False), sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False), sa.Column("summary", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("cleanup_candidates", sa.Column("id", pg.UUID(as_uuid=False), primary_key=True),
        sa.Column("job_id", pg.UUID(as_uuid=False), sa.ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(32), nullable=False), sa.Column("resource_key", sa.Text(), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False), sa.Column("detail", pg.JSONB(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger()), sa.Column("state", sa.String(32), nullable=False), sa.Column("error", sa.Text()))
    op.create_index("ix_cleanup_candidates_job", "cleanup_candidates", ["job_id", "state"])
    op.create_table("managed_search_indices", sa.Column("name", sa.String(255), primary_key=True),
        sa.Column("cluster_uuid", sa.String(128), nullable=False), sa.Column("index_uuid", sa.String(128), nullable=False),
        sa.Column("is_rollback", sa.Boolean(), nullable=False), sa.Column("claimed_by", sa.String(128), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("cleanup_locks", sa.Column("name", sa.String(64), primary_key=True))
    op.execute(sa.text("INSERT INTO cleanup_locks (name) VALUES ('system')"))


def downgrade():
    for table in ("cleanup_locks", "managed_search_indices", "cleanup_candidates", "cleanup_receipts"):
        op.drop_table(table)
