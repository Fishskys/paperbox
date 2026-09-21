"""Schema of the multi-source metadata layer (docs/metadata-architecture.md).

These are metadata-only assertions (no PostgreSQL): they pin the table names,
the columns the services rely on, and -- most importantly -- the three partial
unique indexes that enforce the model's invariants:

* one identifier belongs to one paper,
* one current value per field,
* one primary file per live paper.

The real ``alembic upgrade head`` run is part of the acceptance walkthrough
(section 10 of the plan), not of the unit suite.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Date, Integer, String, Text

from app.db.models import (
    Base,
    Paper,
    PaperFieldProvenance,
    PaperFile,
    PaperIdentifier,
    PaperSource,
    PapersTag,
    Venue,
    VenueEdition,
)


def column_names(model) -> set[str]:
    return {column.name for column in model.__table__.columns}


def index_by_name(model, name: str):
    for index in model.__table__.indexes:
        if index.name == name:
            return index
    raise AssertionError(f"{name} is not declared on {model.__tablename__}")


# --------------------------------------------------------------------------- #
# new tables exist
# --------------------------------------------------------------------------- #
def test_the_four_new_tables_are_registered() -> None:
    tables = set(Base.metadata.tables)

    assert {
        "paper_sources",
        "paper_identifiers",
        "paper_field_provenance",
        "venue_editions",
    } <= tables


def test_paper_sources_columns() -> None:
    assert column_names(PaperSource) == {
        "id",
        "paper_id",
        "source_type",
        "source_ref",
        "content_type",
        "raw",
        "match_status",
        "match_method",
        "match_confidence",
        "fetched_at",
        "imported_at",
        "importer",
    }


def test_paper_sources_is_idempotent_per_source_record() -> None:
    """``UNIQUE(source_type, source_ref)`` is what makes a re-import a no-op."""
    unique = {
        tuple(sorted(constraint.columns.keys()))
        for constraint in PaperSource.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert ("source_ref", "source_type") in unique


def test_paper_sources_frees_an_unmatched_record_when_the_paper_dies() -> None:
    assert PaperSource.paper_id.nullable is True
    assert PaperSource.raw.type.__class__.__name__ == "JSONB"


def test_paper_identifiers_columns_and_types() -> None:
    assert column_names(PaperIdentifier) == {
        "id",
        "paper_id",
        "scheme",
        "value",
        "normalized_value",
        "first_source_id",
        "is_primary",
        "created_at",
    }
    assert isinstance(PaperIdentifier.scheme.type, String)
    assert isinstance(PaperIdentifier.normalized_value.type, Text)


def test_one_identifier_belongs_to_exactly_one_paper() -> None:
    """Partial unique index: ``(scheme, normalized_value) WHERE paper_id IS NOT NULL``."""
    index = index_by_name(PaperIdentifier, "uq_paper_identifiers_scheme_value")

    assert index.unique is True
    assert [column.name for column in index.columns] == ["scheme", "normalized_value"]
    assert str(index.dialect_options["postgresql"]["where"]) == "paper_id IS NOT NULL"


def test_paper_identifiers_are_unique_per_paper_too() -> None:
    unique = {
        tuple(sorted(constraint.columns.keys()))
        for constraint in PaperIdentifier.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert ("normalized_value", "paper_id", "scheme") in unique


def test_provenance_keeps_one_current_value_per_field() -> None:
    index = index_by_name(PaperFieldProvenance, "uq_paper_field_provenance_current")

    assert index.unique is True
    assert [column.name for column in index.columns] == ["paper_id", "field"]
    assert str(index.dialect_options["postgresql"]["where"]) == "is_current"


def test_provenance_columns() -> None:
    assert column_names(PaperFieldProvenance) == {
        "id",
        "paper_id",
        "source_id",
        "field",
        "value",
        "confidence",
        "is_current",
        "decided_by",
        "decided_at",
        "identifier_id",
    }
    assert PaperFieldProvenance.value.type.__class__.__name__ == "JSONB"


def test_venue_editions_split_venue_and_year() -> None:
    assert column_names(VenueEdition) == {
        "id",
        "venue_id",
        "year",
        "location",
        "dates",
        "publication_number",
        "is_number",
    }
    unique = {
        tuple(sorted(constraint.columns.keys()))
        for constraint in VenueEdition.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert ("venue_id", "year") in unique
    assert isinstance(VenueEdition.year.type, Integer)


# --------------------------------------------------------------------------- #
# changed tables
# --------------------------------------------------------------------------- #
def test_papers_gained_the_bibliographic_columns() -> None:
    names = column_names(Paper)

    assert {
        "volume",
        "issue",
        "pages",
        "publication_date",
        "paper_type",
        "venue_edition_id",
        "venue_year",
    } <= names
    assert isinstance(Paper.publication_date.type, Date)
    assert Paper.venue_year.nullable is True


def test_papers_still_own_the_fingerprint_partial_unique_index() -> None:
    index = index_by_name(Paper, "uq_papers_fingerprint_live")

    assert index.unique is True
    assert str(index.dialect_options["postgresql"]["where"]) == "deleted_at IS NULL"


def test_paper_files_record_their_source_and_the_primary_flag() -> None:
    names = column_names(PaperFile)

    assert {"source_id", "is_primary"} <= names
    assert isinstance(PaperFile.is_primary.type, Boolean)


def test_only_one_primary_file_per_live_paper() -> None:
    index = index_by_name(PaperFile, "uq_paper_files_primary")

    assert index.unique is True
    assert [column.name for column in index.columns] == ["paper_id"]
    assert (
        str(index.dialect_options["postgresql"]["where"])
        == "is_primary AND deleted_at IS NULL"
    )


def test_papers_tags_distinguish_tag_kinds() -> None:
    assert "kind" in column_names(PapersTag)
    assert PapersTag.kind.nullable is False
    assert PapersTag.kind.server_default is not None


def test_venues_gained_an_issn_column() -> None:
    assert "issn" in column_names(Venue)


# --------------------------------------------------------------------------- #
# relationships used by the services
# --------------------------------------------------------------------------- #
def test_paper_relationships_cover_the_new_layer() -> None:
    assert "sources" in Paper.__mapper__.relationships
    assert "identifiers" in Paper.__mapper__.relationships
    assert "field_provenance" in Paper.__mapper__.relationships
    assert "venue_edition" in Paper.__mapper__.relationships
    assert "editions" in Venue.__mapper__.relationships