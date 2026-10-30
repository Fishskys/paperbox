"""The shipped status index supports live counts and stable list pagination."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.db.models import Paper
from tests.test_job_progress import factory  # noqa: F401
from tests.test_paper_list_filters import add_paper


INDEX = "ix_papers_live_status_created_id"


def migration():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/d49b2a6e710c_live_paper_status_index.py"
    spec = importlib.util.spec_from_file_location("live_paper_status_index", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_declares_the_postgresql_live_status_and_order_index():
    index = next(index for index in Paper.__table__.indexes if index.name == INDEX)
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "(status, created_at DESC, id)" in ddl
    assert "WHERE deleted_at IS NULL" in ddl


def test_migration_covers_counts_and_order_and_preserves_data(factory):  # noqa: F811
    ids = sorted(add_paper(factory, title="Indexed", status="INDEXED") for _ in range(3))
    deleted = add_paper(factory, status="INDEXED")
    add_paper(factory, status="AWAITING_FILE")
    with factory() as session:
        session.execute(text("UPDATE papers SET created_at = '2026-10-30 10:00:00'"))
        session.execute(update(Paper).where(Paper.id == deleted).values(
            deleted_at=datetime(2026, 10, 30, 10, tzinfo=timezone.utc),
        ))
        session.commit()
    module = migration()
    count = "SELECT count(*) FROM papers WHERE deleted_at IS NULL AND status = 'INDEXED'"
    page = "SELECT id FROM papers WHERE deleted_at IS NULL AND status = 'INDEXED' ORDER BY created_at DESC, id ASC LIMIT 2 OFFSET 1"
    with factory.kw["bind"].connect() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            for sql in (count, page):
                plan = " ".join(str(row) for row in connection.execute(text("EXPLAIN QUERY PLAN " + sql)))
                assert f"INDEX {INDEX}" in plan
                assert "TEMP B-TREE" not in plan
            assert connection.scalar(text(count)) == 3
            assert [str(UUID(value)) for value in connection.scalars(text(page))] == ids[1:]
            module.downgrade()
            assert connection.scalar(text("SELECT count(*) FROM papers")) == 5
            assert connection.scalar(text(count)) == 3
            module.upgrade()
            assert [str(UUID(value)) for value in connection.scalars(text(page))] == ids[1:]