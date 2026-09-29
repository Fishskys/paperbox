"""OpenSearch index mapping for the chunk index (MVP-SPEC section 7).

Only dense vectors are used: ``embedding`` is a ``knn_vector`` with 1024
dimensions backed by Lucene HNSW / L2, matching ``intfloat/multilingual-e5-large``.
The index is created once; the read/write alias ``paper_chunks_current`` is
retargeted at it, so reindexing can build a new physical index without touching
clients (plan sections 9/15/24).

The free-text fields (``title``, ``text``, ``section_title``) use the built-in
``cjk`` analyzer, which segments Chinese/Japanese/Korean text into bigrams
instead of splitting it per character (``standard`` turned ``低功耗SRAM`` into
``低/功/耗/s/r/a/m``). It is applied at index *and* search time so the query
side segments the same way; ``sram``/``low``/``power`` still match Latin text.
``keyword`` fields and the ``knn_vector`` settings are untouched, so existing
vectors stay valid and a new index can be built purely by ``_reindex``.

**Filter fields are an index-time snapshot.** Everything below comes from the
metadata layer (``venues`` / ``venue_editions`` / ``paper_identifiers`` /
``papers_tags``) and is written per chunk by ``tasks._index_rows``. Changing
metadata in PostgreSQL therefore does *not* change filtering until the affected
papers are reindexed.

The snapshot carries more than the legacy ``year``/``venue`` pair:

* ``venue`` stays the venue *name*; ``venue_year`` is the year of the edition the
  paper appeared in, so "the conference" and "the conference in a given year" are
  two different filters (a paper's year and its edition's year are not always the
  same, e.g. early access).
* ``paper_type`` separates journal / conference / preprint / early access.
* ``volume`` / ``issue`` / ``pages`` / ``publication_date`` are the citation
  fields, kept as keywords because they are not always numeric (``S1``, ``12-3``).
* ``identifiers`` holds ``scheme:normalized_value`` strings for every row of
  ``paper_identifiers``, so one field can be searched by DOI, arXiv id *or* IEEE
  article number.
* One keyword field per ``papers_tags.kind`` (:data:`TAG_KIND_FIELDS`) keeps IEEE
  index terms, author terms, dynamic index terms and source tags apart; ``tags``
  remains the flat union of all of them for backward compatibility.
"""

from __future__ import annotations

from app.core.config import settings
from app.services.metadata_tags import (
    KIND_AUTHOR_TERMS,
    KIND_DYNAMIC_INDEX_TERMS,
    KIND_IEEE_TERMS,
    KIND_SOURCE_TAG,
)

#: Text fields searched by BM25 (``title`` is boosted at query time).
TEXT_FIELDS: tuple[str, ...] = ("title", "text")

#: Analyzer used by every free-text field: ``cjk`` handles Chinese bigrams and
#: still tokenizes Latin words, so one analyzer serves the mixed corpus.
TEXT_ANALYZER = "cjk"

#: Free-text properties carrying :data:`TEXT_ANALYZER`.
ANALYZED_FIELDS: tuple[str, ...] = ("title", "section_title", "text")

#: Exact-match filters exposed through ``POST /api/search``.
KEYWORD_FIELDS: tuple[str, ...] = (
    "chunk_id",
    "paper_id",
    "authors",
    "venue",
    "doi",
    "arxiv_id",
    "tags",
    "section",
    # --- metadata snapshot ------------------------------------------------ #
    "paper_type",
    "volume",
    "issue",
    "pages",
    "identifiers",
    "ieee_terms",
    "author_terms",
    "dynamic_index_terms",
    "source_tags",
)

INTEGER_FIELDS: tuple[str, ...] = (
    "year",
    "page_start",
    "page_end",
    "chunk_index",
    #: Year of the venue edition (``venue_editions.year``), not of the paper.
    "venue_year",
)

#: Date properties (ISO-8601 strings in the document).
DATE_FIELDS: tuple[str, ...] = ("publication_date",)

#: One index field per ``papers_tags.kind``; the catch-all kind is plural in the
#: index because it holds every tag that is not an index term.
TAG_KIND_FIELDS: dict[str, str] = {
    KIND_IEEE_TERMS: KIND_IEEE_TERMS,
    KIND_AUTHOR_TERMS: KIND_AUTHOR_TERMS,
    KIND_DYNAMIC_INDEX_TERMS: KIND_DYNAMIC_INDEX_TERMS,
    KIND_SOURCE_TAG: "source_tags",
}


def build_mapping() -> dict:
    """Return the full index body (settings + mappings) for a chunk index."""
    properties: dict[str, dict] = {
        "chunk_id": {"type": "keyword"},
        "paper_id": {"type": "keyword"},
        "title": {"type": "text", "analyzer": TEXT_ANALYZER, "search_analyzer": TEXT_ANALYZER},
        "authors": {"type": "keyword"},
        "year": {"type": "integer"},
        "venue": {"type": "keyword"},
        "doi": {"type": "keyword"},
        "arxiv_id": {"type": "keyword"},
        "tags": {"type": "keyword"},
        "section": {"type": "keyword"},
        "section_title": {"type": "text", "analyzer": TEXT_ANALYZER, "search_analyzer": TEXT_ANALYZER},
        "page_start": {"type": "integer"},
        "page_end": {"type": "integer"},
        "chunk_index": {"type": "integer"},
        "text": {"type": "text", "analyzer": TEXT_ANALYZER, "search_analyzer": TEXT_ANALYZER},
        "embedding": {
            "type": "knn_vector",
            "dimension": settings.embedding_dimension,
            "method": {
                "name": "hnsw",
                "space_type": "l2",
                "engine": "lucene",
                "parameters": {"ef_construction": 128, "m": 16},
            },
        },
        "embedding_model": {"type": "keyword"},
        "embedding_dimension": {"type": "integer"},
        # Which parser produced the chunk text in this document, and its version
        # (plan §6.1 step 2). Keyword, so a mixed library is a filterable fact:
        # ``parser_backend: pypdf`` finds what a backend switch did not reach.
        "parser_backend": {"type": "keyword"},
        "parser_version": {"type": "keyword"},
        "created_at": {"type": "date"},
    }
    # Metadata snapshot: these are what POST /api/search filters read.
    properties.update(
        {
            "venue_year": {"type": "integer"},
            "paper_type": {"type": "keyword"},
            "volume": {"type": "keyword"},
            "issue": {"type": "keyword"},
            "pages": {"type": "keyword"},
            "publication_date": {"type": "date"},
            "identifiers": {"type": "keyword"},
        }
    )
    for field in TAG_KIND_FIELDS.values():
        properties[field] = {"type": "keyword"}
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
        },
        "mappings": {
            # ``strict`` (not ``true``): an undeclared field must fail the write
            # loudly instead of being mapped to ``text`` behind our back. With
            # ``true`` the old rule was "add the field to ``build_mapping()``
            # *before* the first document carries it" -- human discipline that a
            # filter field silently lost the moment someone forgot (AGENTS §3.5).
            # ``tests/test_index_snapshot.py`` pins that every field
            # ``build_chunk_document`` emits is declared, so strict stays safe.
            "dynamic": "strict",
            "properties": properties,
        },
    }


__all__ = [
    "ANALYZED_FIELDS",
    "DATE_FIELDS",
    "INTEGER_FIELDS",
    "KEYWORD_FIELDS",
    "TAG_KIND_FIELDS",
    "TEXT_ANALYZER",
    "TEXT_FIELDS",
    "build_mapping",
]
