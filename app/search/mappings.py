"""OpenSearch index mapping for ``paper_chunks_v1`` (MVP-SPEC section 7).

Only dense vectors are used: ``embedding`` is a ``knn_vector`` with 1024
dimensions backed by Lucene HNSW / L2, matching ``intfloat/multilingual-e5-large``.
The index is created once; the read/write alias ``paper_chunks_current`` is
retargeted at it, so reindexing can build ``paper_chunks_v2`` without touching
clients (plan sections 9/15/24).

The free-text fields (``title``, ``text``, ``section_title``) use the built-in
``cjk`` analyzer, which segments Chinese/Japanese/Korean text into bigrams
instead of splitting it per character (``standard`` turned ``低功耗SRAM`` into
``低/功/耗/s/r/a/m``). It is applied at index *and* search time so the query
side segments the same way; ``sram``/``low``/``power`` still match Latin text.
``keyword`` fields and the ``knn_vector`` settings are untouched, so existing
vectors stay valid and a v2 index can be built purely by ``_reindex``.
"""

from __future__ import annotations

from app.core.config import settings

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
)

INTEGER_FIELDS: tuple[str, ...] = (
    "year",
    "page_start",
    "page_end",
    "chunk_index",
)


def build_mapping() -> dict:
    """Return the full index body (settings + mappings) for ``paper_chunks_v1``."""
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
        "created_at": {"type": "date"},
    }
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
        },
        "mappings": {
            "dynamic": True,
            "properties": properties,
        },
    }


__all__ = [
    "ANALYZED_FIELDS",
    "INTEGER_FIELDS",
    "KEYWORD_FIELDS",
    "TEXT_ANALYZER",
    "TEXT_FIELDS",
    "build_mapping",
]
