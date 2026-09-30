"""``facets=true``: what the metadata filters leave in the library, and how much.

The response used to be one-way -- you could *send* ``venue``/``year_from``/``tag``
but nothing told you which values exist or how many papers each holds. One extra
``size: 0`` aggregation answers that (``hybrid.build_facet_body`` / ``facet_counts``).

Two decisions these tests pin, because both are easy to break by accident:

* every bucket counts **papers** (``cardinality(paper_id)``), never chunks -- the
  index holds one document per chunk, so ``doc_count`` would answer "how many
  chunks mention this venue";
* the body is **query independent** (no BM25 leg, no kNN leg). A facet describes
  the library under the filters; it must not move when a caller changes ``top_k``,
  turns rerank on, or rewrites the query -- and it is the only reading that can be
  compared with ``GET /api/papers``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.search import hybrid
from app.services import search_service


class FakeClient:
    """Answers the facet aggregation, recording what it was asked."""

    def __init__(self, aggregations: dict[str, Any] | None = None) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.aggregations = aggregations if aggregations is not None else default_aggregations()

    def search(self, index: str | None = None, body: dict | None = None):  # noqa: ANN001
        self.bodies.append(body or {})
        return {"hits": {"hits": []}, "aggregations": self.aggregations}


def bucket(key: Any, papers: int, *, key_as_string: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"key": key, "doc_count": papers * 3, "papers": {"value": papers}}
    if key_as_string is not None:
        item["key_as_string"] = key_as_string
    return item


def default_aggregations() -> dict[str, Any]:
    return {
        "venue": {"buckets": [bucket("ISSCC", 4), bucket("IEEE J. Solid-State Circuits", 2)]},
        "paper_type": {"buckets": [bucket("conference", 4), bucket("journal", 2)]},
        "year": {"buckets": [bucket(2017.0, 2, key_as_string="2017"), bucket(2020.0, 4, key_as_string="2020")]},
        "ieee_terms": {"buckets": [bucket("SRAM", 3)]},
        "author_terms": {"buckets": []},
        "dynamic_index_terms": {"buckets": [bucket("low-power", 1)]},
        "source_tags": {"buckets": []},
    }


# --------------------------------------------------------------------------- #
# build_facet_body (pure)
# --------------------------------------------------------------------------- #
def test_the_body_asks_for_paper_counts_not_chunk_counts() -> None:
    body = hybrid.build_facet_body()
    assert body["size"] == 0
    assert body["track_total_hits"] is False
    for name in hybrid.FACET_TERM_FIELDS:
        aggregation = body["aggs"][name]
        assert aggregation["terms"]["field"] == name
        assert aggregation["aggs"]["papers"]["cardinality"]["field"] == "paper_id"


def test_the_year_facet_is_a_histogram_not_a_terms_bucket() -> None:
    """A year is an integer: terms would return the 20 most common years only."""
    year = hybrid.build_facet_body()["aggs"]["year"]
    assert year["histogram"] == {"field": "year", "interval": 1, "min_doc_count": 1}
    assert year["aggs"]["papers"]["cardinality"]["field"] == "paper_id"


def test_every_facet_the_response_promises_is_in_the_body() -> None:
    assert set(hybrid.build_facet_body()["aggs"]) == set(hybrid.FACET_NAMES)
    assert hub_names() == set(hybrid.FACET_NAMES)


def hub_names() -> set[str]:
    """The facet keys the API schema declares (kept in step with the body)."""
    from app.schemas.search import SearchFacets

    return set(SearchFacets.model_fields)


def test_the_body_has_no_relevance_leg() -> None:
    """Query independent: no BM25 ``match``, no ``knn`` -- only the filters."""
    body = hybrid.build_facet_body({"year_from": 2020, "venue": ["ISSCC"]})
    text = repr(body["query"])
    assert "multi_match" not in text
    assert "knn" not in text
    assert body["query"]["bool"]["filter"], "the filters must be there"


def test_an_unfiltered_body_matches_everything() -> None:
    assert hybrid.build_facet_body()["query"] == {"match_all": {}}


def test_the_bucket_ceiling_is_configurable() -> None:
    body = hybrid.build_facet_body(size=5)
    assert body["aggs"]["venue"]["terms"]["size"] == 5


# --------------------------------------------------------------------------- #
# facet_counts (read-out)
# --------------------------------------------------------------------------- #
def test_counts_are_papers_and_keys_are_strings() -> None:
    client = FakeClient()
    counts = hybrid.facet_counts(client=client)
    assert counts["venue"] == [
        {"key": "ISSCC", "count": 4},
        {"key": "IEEE J. Solid-State Circuits", "count": 2},
    ]
    # doc_count (3x the papers) must not leak into the answer.
    assert counts["venue"][0]["count"] != 12


def test_the_year_facet_comes_back_as_a_filter_ready_string() -> None:
    counts = hybrid.facet_counts(client=FakeClient())
    assert counts["year"] == [{"key": "2017", "count": 2}, {"key": "2020", "count": 4}]


def test_facets_absent_from_the_answer_are_empty_lists() -> None:
    client = FakeClient(aggregations={})
    counts = hybrid.facet_counts(client=client)
    assert set(counts) == set(hybrid.FACET_NAMES)
    assert all(value == [] for value in counts.values())


def test_empty_buckets_are_dropped() -> None:
    client = FakeClient(
        aggregations={"venue": {"buckets": [bucket("ISSCC", 0, key_as_string=None)]}}
    )
    assert hybrid.facet_counts(client=client)["venue"] == []


# --------------------------------------------------------------------------- #
# service wiring
# --------------------------------------------------------------------------- #
def _stub_search(monkeypatch, hits: list[Any], calls: list[dict[str, Any]]) -> None:
    """Replace the two hot spots so the service path runs without a cluster."""

    def fake_search_chunks(query, mode, top_k, filters, **kwargs):  # noqa: ANN001
        calls.append({"query": query, "kind": "chunks"})
        return hits

    def fake_count_cards(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append({"args": args, "kind": "count"})
        return 3

    def fake_facets(filters=None, **kwargs):  # noqa: ANN001
        calls.append({"filters": filters, "kind": "facets", **kwargs})
        return {"venue": [{"key": "ISSCC", "count": 4}]}

    import app.search.hybrid as hybrid_module

    monkeypatch.setattr(hybrid_module, "search_chunks", fake_search_chunks)
    monkeypatch.setattr(hybrid_module, "count_papers", fake_count_cards)
    monkeypatch.setattr(hybrid_module, "facet_counts", fake_facets)


def test_facets_are_not_requested_by_default(monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict[str, Any]] = []
    _stub_search(monkeypatch, [], calls)

    outcome = search_service.search_papers("sram", client=object())

    assert outcome.facets is None
    assert not [call for call in calls if call["kind"] == "facets"]


def test_facets_true_reports_the_counts_and_passes_the_filters(monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict[str, Any]] = []
    _stub_search(monkeypatch, [], calls)
    filters = {"year_from": 2020}

    outcome = search_service.search_papers("sram", filters=filters, facets=True, client=object())

    assert outcome.facets == {"venue": [{"key": "ISSCC", "count": 4}]}
    facet_calls = [call for call in calls if call["kind"] == "facets"]
    assert len(facet_calls) == 1 and facet_calls[0]["filters"] == filters


def test_a_broken_aggregation_does_not_break_the_search(monkeypatch) -> None:  # noqa: ANN001
    """Same discipline as ``total``: a courtesy query may fail, the search may not."""
    calls: list[dict[str, Any]] = []
    _stub_search(monkeypatch, [], calls)

    def explode(filters=None, **kwargs):  # noqa: ANN001
        raise search_service.SearchError("cluster said no")

    import app.search.hybrid as hybrid_module

    monkeypatch.setattr(hybrid_module, "facet_counts", explode)

    outcome = search_service.search_papers("sram", facets=True, client=object())

    assert outcome.facets == {}
    assert outcome.total == 3


def test_the_outcome_takes_facets_with_count_total_disabled(monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict[str, Any]] = []
    _stub_search(monkeypatch, [], calls)

    outcome = search_service.search_papers(
        "sram", facets=True, count_total=False, client=object()
    )

    assert outcome.facets == {"venue": [{"key": "ISSCC", "count": 4}]}


# --------------------------------------------------------------------------- #
# API contract
# --------------------------------------------------------------------------- #
def test_the_request_schema_defaults_to_no_facets() -> None:
    from app.schemas.search import SearchRequest

    assert SearchRequest(query="x").facets is False


def test_the_response_schema_carries_the_buckets() -> None:
    from app.schemas.search import SearchResponse

    payload = SearchResponse(
        query="x",
        mode="hybrid",
        total=1,
        candidates=1,
        took_ms=1.0,
        facets={"venue": [{"key": "ISSCC", "count": 4}], "year": [{"key": "2017", "count": 1}]},
    )
    dumped = payload.model_dump()
    assert dumped["facets"]["venue"] == [{"key": "ISSCC", "count": 4}]
    assert dumped["facets"]["paper_type"] == []


@pytest.mark.parametrize("key", ["2017", "ISSCC"])
def test_bucket_keys_stay_strings(key: str) -> None:
    from app.schemas.search import FacetBucket

    assert FacetBucket(key=key, count=1).key == key
