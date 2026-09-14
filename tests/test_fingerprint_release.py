"""The fingerprint claim must only bind live papers (plan sections 5.1 and 23).

Deleting a paper has to release its fingerprint, otherwise the same document can
never be ingested again (the deleted row would keep violating the unique key).
These tests pin the schema shape that makes that work.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint

from app.db.models import Paper

PARTIAL_INDEX = "uq_papers_fingerprint_live"


def test_fingerprint_uses_a_partial_unique_index() -> None:
    indexes = {index.name: index for index in Paper.__table__.indexes}
    assert PARTIAL_INDEX in indexes, "missing partial unique index on fingerprint"

    index = indexes[PARTIAL_INDEX]
    assert index.unique is True
    assert [column.name for column in index.columns] == ["fingerprint"]

    options = index.dialect_options["postgresql"]
    where_clause = options.get("where")
    where = "" if where_clause is None else str(where_clause)
    assert "deleted_at IS NULL" in where, f"index is not partial: {where!r}"


def test_fingerprint_column_is_not_globally_unique() -> None:
    assert Paper.__table__.c.fingerprint.unique is not True

    fingerprint_only = [
        constraint
        for constraint in Paper.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
        and [column.name for column in constraint.columns] == ["fingerprint"]
    ]
    assert fingerprint_only == [], "a global unique constraint would block re-ingest"


def test_papers_still_index_the_lookup_columns() -> None:
    names = {index.name for index in Paper.__table__.indexes}
    for expected in {
        "ix_papers_doi",
        "ix_papers_arxiv_id",
        "ix_papers_year",
        "ix_papers_status",
        "ix_papers_created_at",
    }:
        assert expected in names
