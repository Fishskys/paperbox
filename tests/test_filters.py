"""Unit tests for the ``POST /api/search`` filter/query construction.

Pure functions only: no OpenSearch and no embedding server are touched.
"""

from __future__ import annotations

import pytest

from app.search.hybrid import (
    build_filters,
    build_keyword_query,
    build_semantic_query,
)


def test_no_filters_returns_no_clauses() -> None:
    assert build_filters(None) == []
    assert build_filters({}) == []


def test_empty_strings_and_blank_entries_are_dropped() -> None:
    assert build_filters({"authors": [], "venue": ["  "], "doi": "   ", "tag": []}) == []


def test_year_range_bounds() -> None:
    assert build_filters({"year_from": 2020, "year_to": 2024}) == [
        {"range": {"year": {"gte": 2020, "lte": 2024}}}
    ]
    assert build_filters({"year_from": 2020}) == [{"range": {"year": {"gte": 2020}}}]
    assert build_filters({"year_to": 2024}) == [{"range": {"year": {"lte": 2024}}}]


def test_year_values_are_coerced_from_strings() -> None:
    assert build_filters({"year_from": "2019"}) == [{"range": {"year": {"gte": 2019}}}]
    assert build_filters({"year_from": "not-a-year"}) == []


def test_authors_and_venue_become_terms_clauses() -> None:
    clauses = build_filters({"authors": ["A. Vaswani"], "venue": ["NeurIPS"]})
    assert {"terms": {"authors": ["A. Vaswani"]}} in clauses
    assert {"terms": {"venue": ["NeurIPS"]}} in clauses


def test_scalar_author_string_is_accepted() -> None:
    assert {"terms": {"authors": ["Alice"]}} in build_filters({"authors": "Alice"})


def test_doi_and_arxiv_id_become_term_clauses() -> None:
    clauses = build_filters({"doi": "10.1/abc", "arxiv_id": "1706.03762"})
    assert {"term": {"doi": "10.1/abc"}} in clauses
    assert {"term": {"arxiv_id": "1706.03762"}} in clauses


def test_tag_and_tags_are_both_supported() -> None:
    assert {"terms": {"tags": ["nlp"]}} in build_filters({"tag": ["nlp"]})
    assert {"terms": {"tags": ["nlp"]}} in build_filters({"tags": ["nlp"]})


def test_every_filter_field_produces_exactly_one_clause() -> None:
    clauses = build_filters(
        {
            "year_from": 2018,
            "year_to": 2024,
            "authors": ["Alice", "Bob"],
            "venue": ["ISCA"],
            "doi": "10.1/x",
            "arxiv_id": "2101.00001",
            "tag": ["sram"],
        }
    )
    assert len(clauses) == 6


def test_unknown_filter_keys_are_ignored() -> None:
    assert build_filters({"nonsense": "x"}) == []


def test_keyword_query_shape_without_filters() -> None:
    query = build_keyword_query("layer normalization")
    assert query == {
        "multi_match": {
            "query": "layer normalization",
            "fields": ["title^2", "text"],
            "type": "best_fields",
        }
    }


def test_keyword_query_wraps_filters_in_a_bool_clause() -> None:
    query = build_keyword_query("sram", {"tag": "low-power", "year_from": 2019})
    assert "bool" in query
    assert query["bool"]["must"][0] == {
        "multi_match": {
            "query": "sram",
            "fields": ["title^2", "text"],
            "type": "best_fields",
        }
    }
    assert {"terms": {"tags": ["low-power"]}} in query["bool"]["filter"]
    assert {"range": {"year": {"gte": 2019}}} in query["bool"]["filter"]


def test_semantic_query_pushes_filters_inside_the_knn_clause() -> None:
    query = build_semantic_query([0.5, 0.25], {"venue": ["ISCA"]}, k=5)
    knn = query["knn"]["embedding"]
    assert knn["vector"] == [0.5, 0.25]
    assert knn["k"] == 5
    assert knn["filter"] == {"bool": {"filter": [{"terms": {"venue": ["ISCA"]}}]}}


def test_semantic_query_without_filters_has_no_filter_key() -> None:
    query = build_semantic_query([0.1, 0.2], None, k=3)
    assert "filter" not in query["knn"]["embedding"]


def test_semantic_query_rejects_non_numeric_vectors() -> None:
    with pytest.raises((TypeError, ValueError)):
        build_semantic_query(["nope"], None, k=1)
