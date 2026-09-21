"""Shared test fixtures.

The unit suite never touches the live PostgreSQL/OpenSearch/MinIO stack: the
services are exercised either as pure functions or against SQLite. ``db_session``
below builds an in-memory SQLite copy of the ORM schema (PostgreSQL-only types
translated) so the metadata services can be driven with real SQLAlchemy sessions.

Two deliberate differences from production, both harmless for the assertions
these tests make:

* partial indexes (``postgresql_where``) are skipped -- SQLite has no equivalent,
  and the real behaviour is verified against PostgreSQL in the acceptance run;
* ``JSONB`` becomes ``JSON`` and ``UUID`` becomes ``CHAR(36)``.
"""

from __future__ import annotations

import pytest
import sqlalchemy
from sqlalchemy import Table, UniqueConstraint, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import JSON

from app.db.models import Base

_DIALECT_TYPES = ((JSONB, JSON()), (UUID, sqlalchemy.String(36)))


def _translate(column) -> None:
    for source, replacement in _DIALECT_TYPES:
        if isinstance(column.type, source):
            column.type = replacement


def _sqlite_table(table, metadata) -> Table:
    """Copy ``table`` into ``metadata`` with SQLite-friendly types.

    Primary keys come along with the copied columns; unique constraints are
    rebuilt explicitly (they are what most of these tests are about), everything
    else (partial indexes, check constraints) is dropped.
    """
    columns = [column._copy() for column in table.columns]
    for column in columns:
        _translate(column)
    constraints = [
        UniqueConstraint(*[column.name for column in item.columns], name=item.name)
        for item in table.constraints
        if isinstance(item, UniqueConstraint)
    ]
    return Table(table.name, metadata, *columns, *constraints)


def build_session_factory():
    """An in-memory SQLite session factory holding every paperbox table.

    Returns ``(factory, engine)``; the caller disposes the engine. Used by the API
    tests, which need a factory to override ``get_db`` with.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    metadata = sqlalchemy.MetaData()
    for table in Base.metadata.sorted_tables:
        _sqlite_table(table, metadata)
    metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return factory, engine


@pytest.fixture()
def session_factory():
    """``build_session_factory`` as a fixture."""
    factory, engine = build_session_factory()
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def db_session():
    """An in-memory SQLite session holding every paperbox table."""
    factory, engine = build_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def sqlite_engine():
    """Raw engine for tests that need to assert on the DDL itself."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    yield engine
    engine.dispose()


def table_names(session) -> set[str]:
    """Names of the tables that exist in the current session's database."""
    rows = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")
    ).all()
    return {row[0] for row in rows}