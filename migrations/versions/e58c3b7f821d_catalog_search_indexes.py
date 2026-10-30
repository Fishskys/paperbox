"""Index model-free metadata substring queries and query-scoped facets."""

from alembic import op
import sqlalchemy as sa

revision = "e58c3b7f821d"
down_revision = "d49b2a6e710c"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for table, columns in (("papers", ("title", "abstract", "doi", "arxiv_id")), ("authors", ("name",)), ("venues", ("name",))):
        for column in columns:
            options = {"postgresql_where": sa.text("deleted_at IS NULL")} if table == "papers" else {}
            op.create_index(f"ix_{table}_{column}_trgm", table, [column], postgresql_using="gin",
                postgresql_ops={column: "gin_trgm_ops"}, **options)
    op.create_index("ix_papers_live_catalog", "papers", ["id"],
        postgresql_include=["year", "paper_type", "venue_id"], postgresql_where=sa.text("deleted_at IS NULL"))


def downgrade():
    op.drop_index("ix_papers_live_catalog", table_name="papers")
    for table, columns in (("papers", ("title", "abstract", "doi", "arxiv_id")), ("authors", ("name",)), ("venues", ("name",))):
        for column in columns:
            op.drop_index(f"ix_{table}_{column}_trgm", table_name=table)