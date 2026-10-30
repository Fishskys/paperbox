"""Cover the model census without reading wide chunk rows."""

from alembic import op

revision = "f69d4c8a932e"
down_revision = "e58c3b7f821d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_paper_chunks_embedding_model", "paper_chunks", ["embedding_model"])


def downgrade():
    op.drop_index("ix_paper_chunks_embedding_model", table_name="paper_chunks")