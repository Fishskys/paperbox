"""Versioned paper figures, assets and publication revision."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "f2a8c7d9e401"
down_revision = "c6e1a4b92f70"
branch_labels = None
depends_on = None


def _timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())]


def upgrade():
    op.add_column("papers", sa.Column("figure_revision", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("figure_extraction_runs",
        sa.Column("id", pg.UUID(as_uuid=False), primary_key=True),
        sa.Column("paper_id", pg.UUID(as_uuid=False), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", pg.UUID(as_uuid=False), sa.ForeignKey("ingestion_jobs.id", ondelete="SET NULL")),
        sa.Column("source_file_id", pg.UUID(as_uuid=False), sa.ForeignKey("paper_files.id", ondelete="SET NULL")),
        sa.Column("source_sha256", sa.String(64)),
        sa.Column("options_hash", sa.String(64), nullable=False),
        sa.Column("options", pg.JSONB(), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cleanup_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("warnings", pg.JSONB(), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)), *_timestamps())
    op.create_index("ix_figure_extraction_runs_paper_id", "figure_extraction_runs", ["paper_id"])
    op.create_index("uq_figure_run_current", "figure_extraction_runs", ["paper_id"], unique=True, postgresql_where=sa.text("is_current"))
    op.create_index("uq_figure_run_active", "figure_extraction_runs", ["paper_id"], unique=True, postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.create_table("paper_figures",
        sa.Column("id", pg.UUID(as_uuid=False), primary_key=True),
        sa.Column("run_id", pg.UUID(as_uuid=False), sa.ForeignKey("figure_extraction_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("paper_id", pg.UUID(as_uuid=False), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("label_raw", sa.String(128)),
        sa.Column("figure_number", sa.String(64)),
        sa.Column("caption_text", sa.Text(), nullable=False),
        sa.Column("parts", pg.JSONB(), nullable=False),
        sa.Column("match_status", sa.String(32), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("cleanup_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_paper_figure_ordinal"), *_timestamps())
    op.create_index("ix_paper_figures_run_id", "paper_figures", ["run_id"])
    op.create_index("ix_paper_figures_paper_id", "paper_figures", ["paper_id"])
    op.create_table("paper_figure_assets",
        sa.Column("id", pg.UUID(as_uuid=False), primary_key=True),
        sa.Column("figure_id", pg.UUID(as_uuid=False), sa.ForeignKey("paper_figures.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer()), sa.Column("height", sa.Integer()), sa.Column("dpi", sa.Float()),
        sa.UniqueConstraint("figure_id", "role", "part_index", name="uq_figure_asset_role_part"), *_timestamps())
    op.create_index("ix_paper_figure_assets_figure_id", "paper_figure_assets", ["figure_id"])


def downgrade():
    op.drop_table("paper_figure_assets")
    op.drop_table("paper_figures")
    op.drop_table("figure_extraction_runs")
    op.drop_column("papers", "figure_revision")
