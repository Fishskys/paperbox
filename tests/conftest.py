"""Shared test fixtures.

The unit suite never touches the live PostgreSQL/OpenSearch/MinIO stack: the
services are exercised either as pure functions or against SQLite. ``db_session``
below builds an in-memory SQLite copy of the ORM schema (PostgreSQL-only types
translated) so the metadata services can be driven with real SQLAlchemy sessions.

Deliberate differences from production, both harmless for the assertions
these tests make:

* ``JSONB`` becomes ``JSON`` and ``UUID`` becomes ``CHAR(36)``;
* check constraints are dropped.

Partial unique indexes, by contrast, are **rebuilt** (``sqlite_where``): the
four partial unique indexes are the dedupe floor of the whole metadata model
(fingerprint, primary file, identifier ownership, current provenance), and the
previous claim that "SQLite has no equivalent" was wrong — SQLite has supported
partial indexes since 3.8. With them enforced, a test that violates a dedupe
invariant now fails on IntegrityError instead of passing silently (review
2026-10-05, P1-17).
"""

from __future__ import annotations

import os

# The unit suite must not inherit the developer's ``.env``. Two switches matter:
#
# * ``MCP_ENABLED=true`` makes the app lifespan enter the SDK session manager, and
#   that manager "can only be called once per instance" -- the second ``TestClient``
#   in a run then dies during startup. Tests that exercise MCP turn it on
#   explicitly (``tests/test_mcp_*.py``), so pin it off here.
# * ``AUTH_ENABLED=true`` would make anonymous requests 401; the suite asserts
#   anonymous behaviour and the auth fixtures set the switch themselves.
#
# Real environment variables take priority over ``.env`` in pydantic-settings, and
# conftest is imported before any test module, so this pins the whole run.
os.environ["MCP_ENABLED"] = "false"
os.environ["AUTH_ENABLED"] = "false"

import pytest
import sqlalchemy
from sqlalchemy import Index, Table, UniqueConstraint, create_engine, text
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
    rebuilt explicitly (they are what most of these tests are about), as are
    the partial unique indexes (``postgresql_where`` -> ``sqlite_where``);
    check constraints are dropped.
    """
    columns = [column._copy() for column in table.columns]
    for column in columns:
        _translate(column)
    constraints = [
        UniqueConstraint(*[column.name for column in item.columns], name=item.name)
        for item in table.constraints
        if isinstance(item, UniqueConstraint)
    ]
    new_table = Table(table.name, metadata, *columns, *constraints)
    for item in table.indexes:
        try:
            where = item.dialect_options["postgresql"]["where"]
        except KeyError:
            continue
        if item.unique:
            Index(
                item.name,
                *[new_table.c[column.name] for column in item.columns],
                unique=True,
                sqlite_where=where,
            )
    return new_table


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


class InMemoryArtifactStore:
    """Stand-in for ``object_storage`` inside the parse-artifact cache."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str | None, str], bytes] = {}

    def download_bytes(self, object_key: str, bucket: str | None = None) -> bytes:
        try:
            return self.objects[(bucket, object_key)]
        except KeyError:
            from app.services.object_storage import ObjectNotFound

            raise ObjectNotFound(f"no such object: {object_key}") from None

    def upload_bytes(
        self,
        object_key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        bucket: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> object:
        self.objects[(bucket, object_key)] = data
        return object()


@pytest.fixture(autouse=True)
def _never_touch_real_object_storage(monkeypatch):
    """Guard: no unit test may reach MinIO through ``parser_service``.

    ``parse_paper_file`` falls back to the real ``object_storage`` module when no
    store is injected. One forgotten ``store=`` already put 16 orphan objects in
    the live bucket (2026-09-29), so that fallback is replaced with an in-memory
    store for *every* test. Tests that assert cache behaviour inject their own
    store and are unaffected.
    """
    from app.services import parser_service

    store = InMemoryArtifactStore()
    monkeypatch.setattr(parser_service, "_default_store", lambda: store)
    return store
