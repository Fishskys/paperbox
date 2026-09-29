"""``total`` / ``candidates``: the paper count must be a count, not the page size.

Before 2026-09-30 ``POST /api/search`` reported ``len(results)`` as ``total`` --
with ``top_k=3`` the response said "3 matching papers" no matter how many the
library held (``search_service.search_papers`` even documented the value as the
chunk candidate pool). Two separate numbers now:

* ``total`` -- ``cardinality(paper_id)`` over the same query + filters, asked of
  the engine in a ``size: 0`` aggregation (``hybrid.count_papers``);
* ``candidates`` -- the chunk pool the paper aggregation ran on.

These tests pin the body shape (pure), the count read-out (fake client) and the
service wiring (fake legs), so the API contract cannot silently drift back.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.search import hybrid
from app.services import search_service


def make_hit(chunk_id: str, paper_id: str, score: float = 1.0) -> hybrid.ChunkHit:
    return hybrid.ChunkHit(
        chunk_id=chunk_id, paper_id=paper_id, score=score, text="chunk text"
    )


class FakeClient:
    """Records the bodies it is asked to search and answers with canned aggs."""

    def __init__(self, papers: int = 7) -> None:
        self.papers = papers
        self.bodies: list[dict[str, Any]] = []

    def search(self, index: str | None = None, body: dict | None = None):  # noqa: ANN001
        self.bodies.append(body or {})
        return {"hits": {"hits": []}, "aggregations": {"papers": {"value": self.papers}}}


# --------------------------------------------------------------------------- #
# build_count_body (pure)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def no_embed(monkeypatch):
    """Keep the vector leg offline: the embedder is a network service."""
    monkeypatch.setattr(hybrid, "embed_text", lambda text: [0.1, 0.2])


def test_the_count_body_asks_for_a_cardinality_aggregation(no_embed) -> None:
    body = hybrid.build_count_body("analog sizing", "keyword")

    assert body["size"] == 0
    assert body["aggs"]["papers"]["cardinality"]["field"] == "paper_id"
    assert body["query"] == hybrid.build_keyword_query("analog sizing", None)


def test_the_count_body_counts_the_union_of_both_legs(no_embed) -> None:
    """A keyword-only count would hide papers only the vector leg can find."""
    body = hybrid.build_count_body("analog sizing", "hybrid")
    should = body["query"]["bool"]["should"]

    assert body["query"]["bool"]["minimum_should_match"] == 1
    assert len(should) == 2
    assert "multi_match" in should[0]
    assert "knn" in should[1]


def test_the_count_body_uses_the_knn_leg_alone_for_semantic_mode(no_embed) -> None:
    body = hybrid.build_count_body("analog sizing", "semantic", k=42)

    assert body["query"]["knn"]["embedding"]["k"] == 42


def test_the_count_body_carries_the_filters(no_embed) -> None:
    """Same filter block as the search legs (``year_from``/``year_to``)."""
    body = hybrid.build_count_body("sizing", "keyword", {"year_from": 2020, "year_to": 2026})

    assert {"range": {"year": {"gte": 2020, "lte": 2026}}} in body["query"]["bool"]["filter"]


def test_an_unknown_mode_is_rejected(no_embed) -> None:
    with pytest.raises(ValueError, match="unsupported search mode"):
        hybrid.build_count_body("x", "fuzzy")


# --------------------------------------------------------------------------- #
# count_papers
# --------------------------------------------------------------------------- #
def test_the_paper_count_comes_from_the_aggregation(no_embed) -> None:
    client = FakeClient(papers=11)

    assert hybrid.count_papers("analog sizing", "keyword", client=client) == 11
    assert client.bodies[0]["size"] == 0


def test_an_empty_query_costs_no_round_trip() -> None:
    client = FakeClient()

    assert hybrid.count_papers("   ", "hybrid", client=client) == 0
    assert client.bodies == []


# --------------------------------------------------------------------------- #
# search_papers: the outcome carries both numbers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fake_search(monkeypatch):
    """Two chunks of paper-a plus one of paper-b; the count is stubbed."""
    hits = [
        make_hit("c1", "paper-a", 3.0),
        make_hit("c2", "paper-a", 2.0),
        make_hit("c3", "paper-b", 1.0),
    ]
    monkeypatch.setattr(hybrid, "search_chunks", lambda *a, **k: list(hits))
    state: dict[str, Any] = {"count_calls": 0}

    def fake_count(query, mode, filters=None, **kwargs):  # noqa: ANN001
        state["count_calls"] += 1
        return 25

    monkeypatch.setattr(hybrid, "count_papers", fake_count)
    return state


def test_total_is_the_paper_count_and_candidates_is_the_chunk_pool(
    fake_search,
) -> None:
    outcome = search_service.search_papers("sizing", "hybrid", top_k=2)

    assert [item.paper_id for item in outcome.results] == ["paper-a", "paper-b"]
    assert outcome.total == 25  # not 2 ("the page"), not 3 ("the chunks")
    assert outcome.candidates == 3
    assert fake_search["count_calls"] == 1


def test_counting_can_be_skipped_for_callers_that_only_want_the_page(
    fake_search,
) -> None:
    outcome = search_service.search_papers("sizing", "hybrid", count_total=False)

    assert outcome.total == 2  # papers seen in the pool, stated as such
    assert outcome.candidates == 3
    assert fake_search["count_calls"] == 0


def test_a_failed_count_does_not_fail_the_search(monkeypatch) -> None:
    """The extra query is a courtesy: losing it must not 503 a working search."""
    hits = [make_hit("c1", "paper-a", 1.0)]
    monkeypatch.setattr(hybrid, "search_chunks", lambda *a, **k: list(hits))

    def boom(*args, **kwargs):
        raise hybrid.SearchError("count index gone")

    monkeypatch.setattr(hybrid, "count_papers", boom)

    outcome = search_service.search_papers("sizing", "hybrid")

    assert outcome.total == 1  # the pool's paper count, not a lie about 0
    assert [item.paper_id for item in outcome.results] == ["paper-a"]
