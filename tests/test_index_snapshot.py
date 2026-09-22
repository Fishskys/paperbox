"""The index-time metadata snapshot: mapping, document shape and tag kinds.

``POST /api/search`` filters read fields that are written into every chunk
document at index time, so two things have to hold: the mapping must declare them
with the right type (``venue_year`` has to be an integer to be range-filterable,
``publication_date`` a date), and ``tasks._index_rows`` must fill them from the
metadata layer (venue + edition year, paper type, citation fields, identifiers,
one tag list per kind). No OpenSearch connection is opened here.
"""

from __future__ import annotations

from datetime import date

from app.db.models import (
    Paper,
    PaperChunk,
    PaperIdentifier,
    PaperTag,
    PapersTag,
    Venue,
    new_uuid,
)
from app.search import mappings, opensearch
from app.workers import tasks
from tests.test_job_progress import factory  # noqa: F401 - fixture


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def make_paper(session_factory) -> tuple[str, str]:
    """One paper with a venue, two identifiers, two tag kinds and one chunk."""
    session = session_factory()
    try:
        venue = Venue(
            id=new_uuid(), name="ISSCC", normalized_name="isscc", kind="conference"
        )
        paper = Paper(
            id=new_uuid(),
            title="A Low Power SRAM",
            fingerprint=f"sha256:{new_uuid()}",
            status="PENDING",
            year=2021,
            venue=venue,
            venue_year=2021,
            paper_type="conference",
            volume="64",
            issue="3",
            pages="412-419",
            publication_date=date(2021, 3, 1),
        )
        session.add_all([venue, paper])
        session.flush()
        session.add_all(
            [
                PaperIdentifier(
                    id=new_uuid(),
                    paper_id=paper.id,
                    scheme="doi",
                    value="10.1109/JSSC.2021.1",
                    normalized_value="10.1109/jssc.2021.1",
                    is_primary=True,
                ),
                PaperIdentifier(
                    id=new_uuid(),
                    paper_id=paper.id,
                    scheme="ieee_article_number",
                    value="7065247",
                    normalized_value="7065247",
                ),
            ]
        )
        ieee_term = PaperTag(
            id=new_uuid(), name="Low Power SRAM", normalized_name="low power sram"
        )
        source_tag = PaperTag(id=new_uuid(), name="nlp", normalized_name="nlp")
        session.add_all([ieee_term, source_tag])
        session.flush()
        session.add_all(
            [
                PapersTag(
                    id=new_uuid(),
                    paper_id=paper.id,
                    tag_id=ieee_term.id,
                    kind="ieee_terms",
                ),
                PapersTag(
                    id=new_uuid(),
                    paper_id=paper.id,
                    tag_id=source_tag.id,
                    kind="source_tag",
                ),
            ]
        )
        chunk = PaperChunk(
            id=new_uuid(),
            paper_id=paper.id,
            chunk_index=0,
            page_start=1,
            page_end=1,
            section="body",
            text="some chunk text",
            token_count=3,
            char_count=15,
        )
        session.add(chunk)
        session.commit()
        return paper.id, chunk.id
    finally:
        session.close()


def index_rows(session_factory, paper_id: str, chunk_id: str) -> list[dict]:
    session = session_factory()
    try:
        paper = session.get(Paper, paper_id)
        chunk = session.get(PaperChunk, chunk_id)
        return tasks._index_rows(paper, [chunk], [[0.0, 0.0, 0.0]])
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# the mapping
# --------------------------------------------------------------------------- #


def test_the_mapping_declares_every_filter_field_with_its_type() -> None:
    properties = mappings.build_mapping()["mappings"]["properties"]
    for field in mappings.KEYWORD_FIELDS:
        assert properties[field]["type"] == "keyword", field
    for field in mappings.INTEGER_FIELDS:
        assert properties[field]["type"] == "integer", field
    for field in mappings.DATE_FIELDS:
        assert properties[field]["type"] == "date", field


def test_the_snapshot_fields_are_declared() -> None:
    properties = mappings.build_mapping()["mappings"]["properties"]
    # "the conference in a given year" needs an integer, not a string.
    assert properties["venue_year"] == {"type": "integer"}
    assert properties["publication_date"] == {"type": "date"}
    assert properties["identifiers"] == {"type": "keyword"}
    assert properties["paper_type"] == {"type": "keyword"}
    for field in ("volume", "issue", "pages"):
        assert properties[field] == {"type": "keyword"}
    # One field per tag kind, and the catch-all is plural.
    assert set(mappings.TAG_KIND_FIELDS.values()) == {
        "ieee_terms",
        "author_terms",
        "dynamic_index_terms",
        "source_tags",
    }
    for field in mappings.TAG_KIND_FIELDS.values():
        assert properties[field] == {"type": "keyword"}


def test_the_vector_field_is_still_a_dense_knn_vector() -> None:
    properties = mappings.build_mapping()["mappings"]["properties"]
    assert properties["embedding"]["type"] == "knn_vector"
    assert properties["embedding"]["method"]["engine"] == "lucene"


# --------------------------------------------------------------------------- #
# _index_rows
# --------------------------------------------------------------------------- #


def test_index_rows_carry_the_metadata_snapshot(factory) -> None:  # noqa: F811
    paper_id, chunk_id = make_paper(factory)
    row = index_rows(factory, paper_id, chunk_id)[0]
    assert row["venue"] == "ISSCC"
    assert row["venue_year"] == 2021
    assert row["paper_type"] == "conference"
    assert row["volume"] == "64"
    assert row["issue"] == "3"
    assert row["pages"] == "412-419"
    assert row["publication_date"] == date(2021, 3, 1)
    assert row["identifiers"] == [
        "doi:10.1109/jssc.2021.1",
        "ieee_article_number:7065247",
    ]


def test_index_rows_keep_tag_kinds_apart_and_the_flat_union(factory) -> None:  # noqa: F811
    paper_id, chunk_id = make_paper(factory)
    row = index_rows(factory, paper_id, chunk_id)[0]
    assert row["ieee_terms"] == ["Low Power SRAM"]
    assert row["source_tags"] == ["nlp"]
    assert row["author_terms"] == []
    assert row["dynamic_index_terms"] == []
    # The flat list keeps working for callers that do not care about the kind.
    assert sorted(row["tags"]) == ["Low Power SRAM", "nlp"]


def test_index_rows_survive_a_paper_without_metadata(factory) -> None:  # noqa: F811
    """A bare paper must produce the keys anyway (empty, never missing)."""
    session = factory()
    try:
        paper = Paper(
            id=new_uuid(),
            title="Bare",
            fingerprint=f"sha256:{new_uuid()}",
            status="PENDING",
        )
        chunk = PaperChunk(
            id=new_uuid(),
            paper_id=paper.id,
            chunk_index=0,
            page_start=1,
            page_end=1,
            section="body",
            text="text",
            token_count=1,
            char_count=4,
        )
        session.add_all([paper, chunk])
        session.commit()
        paper_id, chunk_id = paper.id, chunk.id
    finally:
        session.close()

    row = index_rows(factory, paper_id, chunk_id)[0]
    assert row["venue"] is None
    assert row["venue_year"] is None
    assert row["publication_date"] is None
    assert row["identifiers"] == []
    for field in mappings.TAG_KIND_FIELDS.values():
        assert row[field] == []


# --------------------------------------------------------------------------- #
# build_chunk_document
# --------------------------------------------------------------------------- #


def test_the_document_stringifies_lists_and_dates() -> None:
    document = opensearch.build_chunk_document(
        {
            "chunk_id": "c1",
            "paper_id": "p1",
            "text": "body",
            "publication_date": date(2021, 3, 1),
            "identifiers": ["doi:10.1/x"],
            "ieee_terms": ["Low Power SRAM"],
        }
    )
    assert document["publication_date"] == "2021-03-01"
    assert document["identifiers"] == ["doi:10.1/x"]
    assert document["ieee_terms"] == ["Low Power SRAM"]
    assert document["source_tags"] == []
    assert document["venue_year"] is None


def test_the_document_accepts_an_iso_date_string() -> None:
    document = opensearch.build_chunk_document(
        {"chunk_id": "c1", "paper_id": "p1", "publication_date": "2021-03-01"}
    )
    assert document["publication_date"] == "2021-03-01"


def test_the_document_carries_every_mapping_field_the_filters_use() -> None:
    """Every declared filter field must exist in the document (or be empty)."""
    document = opensearch.build_chunk_document({"chunk_id": "c1", "paper_id": "p1"})
    for field in mappings.KEYWORD_FIELDS:
        assert field in document, field
    for field in mappings.INTEGER_FIELDS:
        assert field in document, field