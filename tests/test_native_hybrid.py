"""Native hybrid retrieval: engine-side fusion + collapse (plan §7, M5).

Everything here runs without a cluster: the body/response builders are pure
functions, and ``native_search``/``search_chunks`` are driven through a fake
client plus a stubbed embedding call. The live numbers (RRF equivalence with
``rrf_fuse``, the paper-count effect of ``pagination_depth``, the outer
perf/quality A/B) belong to ``scripts/eval.py`` + ``scripts/compare_backends.py``.

What these tests are actually defending:

* the request stays a *single* hybrid request whose ``pagination_depth`` lives
  **inside** the ``hybrid`` clause (as a body key or URL param the cluster
  answers 400 -- cost 20 minutes to find out, see ``app/search/native.py``);
* siblings inherit the winner's fused score instead of their raw BM25/kNN score,
  so ``aggregate_papers`` cannot produce a paper score in foreign units;
* the native path is a drop-in for the Python one: same ``ChunkHit`` stream, same
  rerank/aggregation downstream, same error type;
* the pipeline bodies are the app's, not the cluster's, and the evidence budget
  matches what the aggregation keeps.
"""

from __future__ import annotations

import pytest
from opensearchpy.exceptions import NotFoundError, OpenSearchException
from pydantic import ValidationError

from app.schemas.search import SearchRequest, SearchResponse
from app.search import hybrid, native
from app.services import search_service
from app.services.rerank_service import RerankScore

VECTOR = [0.1, 0.2, 0.3]


def make_response(
    *hits: tuple[str, str, float, list[tuple[str, float]]],
) -> dict:
    """Build an OpenSearch response: ``(chunk_id, paper_id, score, inner[])``."""
    body: list[dict] = []
    for chunk_id, paper_id, score, inner in hits:
        body.append(
            {
                "_id": chunk_id,
                "_score": score,
                "_source": {
                    "chunk_id": chunk_id,
                    "paper_id": paper_id,
                    "text": f"text of {chunk_id}",
                    "title": "A Paper",
                    "authors": ["Ada"],
                    "page_start": 3,
                    "section_title": "2 Method",
                },
                "inner_hits": {
                    native.INNER_HITS_NAME: {
                        "hits": {
                            "hits": [
                                {
                                    "_id": sibling_id,
                                    "_score": sibling_score,
                                    "_source": {
                                        "chunk_id": sibling_id,
                                        "paper_id": paper_id,
                                        "text": f"text of {sibling_id}",
                                        "page_start": 9,
                                    },
                                }
                                for sibling_id, sibling_score in inner
                            ]
                        }
                    }
                },
            }
        )
    return {"hits": {"total": {"value": 99}, "hits": body}}


class FakeClient:
    """Minimal stand-in for ``opensearchpy.OpenSearch``."""

    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self.calls: list[dict] = []
        self.response = response if response is not None else {"hits": {"hits": []}}
        self.error = error

    def search(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeTransport:
    def __init__(self, existing: dict[str, dict] | None = None):
        self.existing = dict(existing or {})
        self.writes: list[tuple[str, str, dict]] = []

    def perform_request(self, method: str, path: str, body=None):
        name = path.rsplit("/", 1)[-1]
        if method == "GET":
            if name not in self.existing:
                raise NotFoundError(404, "pipeline missing")
            return {name: self.existing[name]}
        self.writes.append((method, name, body))
        self.existing[name] = body
        return {"acknowledged": True}


class FakePipelineClient:
    def __init__(self, existing: dict[str, dict] | None = None):
        self.transport = FakeTransport(existing)


# --------------------------------------------------------------------------- #
# body construction
# --------------------------------------------------------------------------- #
def test_pagination_depth_lives_inside_the_hybrid_clause() -> None:
    """A top-level body key or a URL param is a 400 -- it belongs in the clause."""
    body = native.build_native_body("q", VECTOR, size=10, pagination_depth=50)

    assert set(body) == {"size", "query", "_source", "collapse"}
    assert body["query"]["hybrid"]["pagination_depth"] == 50


def test_native_body_carries_both_legs_and_the_boosted_title() -> None:
    clause = native.build_native_body("low power sram", VECTOR, size=5)["query"]["hybrid"]
    keyword, semantic = clause["queries"]

    assert keyword["multi_match"]["query"] == "low power sram"
    assert keyword["multi_match"]["fields"] == ["title^2", "text"]
    assert semantic["knn"]["embedding"]["vector"] == VECTOR


def test_knn_k_follows_pagination_depth() -> None:
    """The vector leg must reach as deep as the fusion window, not just ``size``."""
    clause = native.build_native_body("q", VECTOR, size=10, pagination_depth=50)["query"][
        "hybrid"
    ]

    assert clause["queries"][1]["knn"]["embedding"]["k"] == 50


def test_pagination_depth_defaults_to_the_python_candidate_window() -> None:
    """Same over-fetch factor as the Python path (``top_k * CANDIDATE_MULTIPLIER``)."""
    body = native.build_native_body("q", VECTOR, size=10)

    assert (
        body["query"]["hybrid"]["pagination_depth"]
        == 10 * hybrid.CANDIDATE_MULTIPLIER
    )


def test_filters_are_attached_to_the_clause_not_to_each_leg() -> None:
    clause = native.build_native_body(
        "q", VECTOR, {"year_from": 2020, "year_to": 2024}, size=5
    )["query"]["hybrid"]

    assert clause["filter"] == {
        "bool": {"filter": [{"range": {"year": {"gte": 2020, "lte": 2024}}}]}
    }


def test_without_filters_there_is_no_filter_key() -> None:
    clause = native.build_native_body("q", VECTOR, None, size=5)["query"]["hybrid"]

    assert "filter" not in clause


def test_collapse_targets_paper_id_with_named_inner_hits() -> None:
    collapse = native.build_native_body("q", VECTOR, size=10, inner_hits=3)["collapse"]

    assert collapse["field"] == "paper_id"
    assert collapse["inner_hits"]["name"] == native.INNER_HITS_NAME
    assert collapse["inner_hits"]["size"] == 3


def test_inner_hits_can_be_switched_off() -> None:
    body = native.build_native_body("q", VECTOR, size=10, inner_hits=0)

    assert "collapse" not in body


def test_source_is_filtered_for_hits_and_inner_hits_alike() -> None:
    """Full ``_source`` would ship the 1024-dim vector with every document."""
    body = native.build_native_body("q", VECTOR, size=10)
    includes = body["_source"]["includes"]

    assert includes == list(hybrid.SOURCE_FIELDS)
    assert "embedding" not in includes
    assert body["collapse"]["inner_hits"]["_source"]["includes"] == includes


# --------------------------------------------------------------------------- #
# response parsing
# --------------------------------------------------------------------------- #
def test_winner_and_siblings_become_one_flat_hit_list() -> None:
    response = make_response(("c1", "p1", 0.032787, [("c2", 13.44), ("c3", 11.37)]))

    hits = native.parse_native_response(response)

    assert [hit.chunk_id for hit in hits] == ["c1", "c2", "c3"]
    assert [hit.paper_id for hit in hits] == ["p1", "p1", "p1"]
    assert hits[1].text == "text of c2"
    assert hits[1].page_start == 9


def test_siblings_inherit_the_fused_score_not_their_raw_score() -> None:
    """Raw inner_hits scores are BM25/kNN units; a paper score must stay RRF."""
    response = make_response(("c1", "p1", 0.032787, [("c2", 13.44)]))

    hits = native.parse_native_response(response)

    assert [hit.score for hit in hits] == [0.032787, 0.032787]


def test_native_hits_leave_the_per_leg_scores_empty() -> None:
    """Plan §7 T-E2: one fused score is all the engine reports."""
    hits = native.parse_native_response(
        make_response(("c1", "p1", 0.03, [("c2", 9.9)]))
    )

    assert all(hit.keyword_score is None for hit in hits)
    assert all(hit.semantic_score is None for hit in hits)
    assert all(hit.rerank_score is None for hit in hits)


def test_the_winner_is_never_duplicated_by_its_own_sibling_entry() -> None:
    response = make_response(("c1", "p1", 0.03, [("c1", 13.44)]))

    assert [hit.chunk_id for hit in native.parse_native_response(response)] == ["c1"]


def test_hits_without_an_id_are_skipped() -> None:
    response = {
        "hits": {
            "hits": [
                {"_score": 1.0, "_source": {"paper_id": "p1"}},
                {
                    "_id": "c1",
                    "_score": 0.5,
                    "_source": {"chunk_id": "c1", "paper_id": "p1"},
                    "inner_hits": {
                        native.INNER_HITS_NAME: {
                            "hits": {"hits": [{"_score": 2.0, "_source": {}}]}
                        }
                    },
                },
            ]
        }
    }

    assert [hit.chunk_id for hit in native.parse_native_response(response)] == ["c1"]


def test_an_empty_response_is_an_empty_hit_list() -> None:
    assert native.parse_native_response({"hits": {"hits": []}}) == []
    assert native.parse_native_response({}) == []


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def test_native_search_sends_one_pipelined_request(monkeypatch) -> None:
    monkeypatch.setattr(native, "embed_text", lambda text: VECTOR)
    client = FakeClient(make_response(("c1", "p1", 0.03, [])))

    hits = native.native_search("q", 10, {"year_from": 2020}, client=client, index="idx")

    assert [hit.chunk_id for hit in hits] == ["c1"]
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["index"] == "idx"
    assert call["params"] == {"search_pipeline": native.PIPELINE_RRF}
    assert call["body"]["size"] == 10
    assert call["body"]["query"]["hybrid"]["pagination_depth"] == 50


def test_native_search_does_not_call_the_cluster_for_a_blank_query(monkeypatch) -> None:
    def explode(text):  # pragma: no cover - must not run
        raise AssertionError("embedding must not be attempted")

    monkeypatch.setattr(native, "embed_text", explode)
    client = FakeClient()

    assert native.native_search("   ", 10, client=client) == []
    assert client.calls == []


def test_a_failing_embedding_is_a_search_error(monkeypatch) -> None:
    from app.services.embedding_service import EmbeddingError

    def boom(text):
        raise EmbeddingError("embedding server down")

    monkeypatch.setattr(native, "embed_text", boom)

    with pytest.raises(hybrid.SearchError):
        native.native_search("q", 10, client=FakeClient())


def test_a_missing_index_is_reported_as_a_search_error(monkeypatch) -> None:
    monkeypatch.setattr(native, "embed_text", lambda text: VECTOR)
    client = FakeClient(error=NotFoundError(404, "index_not_found_exception"))

    with pytest.raises(hybrid.SearchError):
        native.native_search("q", 10, client=client)


def test_an_engine_failure_is_reported_as_a_search_error(monkeypatch) -> None:
    monkeypatch.setattr(native, "embed_text", lambda text: VECTOR)
    client = FakeClient(error=OpenSearchException(500, "boom"))

    with pytest.raises(hybrid.SearchError):
        native.native_search("q", 10, client=client)


# --------------------------------------------------------------------------- #
# dispatch from search_chunks
# --------------------------------------------------------------------------- #
def test_search_chunks_dispatches_to_the_native_path(monkeypatch) -> None:
    seen: dict = {}

    def fake_native(query, size, filters, **kwargs):
        seen.update({"query": query, "size": size, "filters": filters, **kwargs})
        return [hybrid.ChunkHit(chunk_id="c1", paper_id="p1", score=0.03)]

    monkeypatch.setattr(native, "native_search", fake_native)
    # The Python path must not run at all: make both legs explode.
    monkeypatch.setattr(hybrid, "_keyword_hits", lambda *a, **k: pytest.fail("keyword leg"))
    monkeypatch.setattr(hybrid, "_semantic_hits", lambda *a, **k: pytest.fail("semantic leg"))

    hits = hybrid.search_chunks("q", "hybrid", 10, backend="native")

    assert [hit.chunk_id for hit in hits] == ["c1"]
    assert seen["size"] == 10
    assert seen["index"] == hybrid.ALIAS


def test_native_over_fetches_inside_a_paper_when_reranking(monkeypatch) -> None:
    """Native spells "over-fetch" as more chunks per paper, not more papers.

    The reranking budget has to be reachable *inside* each collapsed paper: the
    request asks for ``RERANK_CANDIDATES`` chunks per paper while the paper count
    stays ``top_k``, so the cross-encoder sees the same number of candidates the
    Python path gives it (``top_k x RERANK_CANDIDATES``).
    """
    seen: dict = {}

    def fake_native(query, size, filters, **kwargs):
        seen.update({"size": size, **kwargs})
        return []

    monkeypatch.setattr(native, "native_search", fake_native)

    per_paper = hybrid._rerank_chunks_per_paper()
    hybrid.search_chunks("q", "hybrid", 10, rerank=True, backend="native")
    # winner + (per_paper - 1) siblings, never fewer than the evidence budget
    assert seen["size"] == 10
    assert seen["inner_hits"] == max(native.DEFAULT_INNER_HITS, per_paper - 1)

    hybrid.search_chunks("q", "hybrid", 10, backend="native")
    assert seen["size"] == 10
    assert seen["inner_hits"] == native.DEFAULT_INNER_HITS


def test_the_single_leg_modes_ignore_the_backend(monkeypatch) -> None:
    """``SEARCH_BACKEND=native`` must not touch keyword/semantic retrieval."""
    monkeypatch.setattr(hybrid, "_keyword_hits", lambda *a, **k: [])
    telemetry: dict = {}

    hybrid.search_chunks("q", "keyword", 10, backend="native", telemetry=telemetry)

    assert telemetry["backend"] == "python"


def test_telemetry_reports_the_backend_that_ran(monkeypatch) -> None:
    monkeypatch.setattr(native, "native_search", lambda *a, **k: [])
    telemetry: dict = {}

    hybrid.search_chunks("q", "hybrid", 10, backend="native", telemetry=telemetry)

    assert telemetry["backend"] == "native"


def test_an_unknown_backend_is_rejected() -> None:
    with pytest.raises(ValueError):
        hybrid._resolve_backend("opensearch-wizard")


def test_the_request_backend_wins_over_the_configured_one(monkeypatch) -> None:
    monkeypatch.setattr(hybrid.settings, "search_backend", "native")

    assert hybrid._resolve_backend("python") == "python"
    assert hybrid._resolve_backend(None) == "native"


# --------------------------------------------------------------------------- #
# siblings are evidence, not candidates (``ChunkHit.primary``)
# --------------------------------------------------------------------------- #
def collapsed_hits() -> list[hybrid.ChunkHit]:
    """Two papers: ``p1`` won with two siblings, ``p2`` with one."""
    return native.parse_native_response(
        make_response(
            ("c1", "p1", 0.032, [("c1a", 13.4), ("c1b", 11.0)]),
            ("c2", "p2", 0.016, [("c2a", 7.5)]),
        )
    )


def test_siblings_are_marked_as_not_primary() -> None:
    hits = collapsed_hits()

    assert [hit.primary for hit in hits] == [True, False, False, True, False]
    assert [hit.paper_id for hit in hits] == ["p1", "p1", "p1", "p2", "p2"]


def test_truncating_by_paper_keeps_whole_evidence_groups() -> None:
    hits = collapsed_hits()

    kept = hybrid._truncate_hits(hits, 2, by_paper=True)
    assert [hit.chunk_id for hit in kept] == ["c1", "c1a", "c1b", "c2", "c2a"]
    # One paper only: its siblings come along, nothing of the next paper leaks in.
    assert [
        hit.chunk_id for hit in hybrid._truncate_hits(hits, 1, by_paper=True)
    ] == ["c1", "c1a", "c1b"]


def test_truncating_by_paper_does_not_mistake_hits_for_papers() -> None:
    """The bug this guards: ``top_k=10`` came back as 3 papers.

    A collapsed stream spends ``1 + inner_hits`` hits per paper, so a positional
    slice cuts papers in half and under-delivers; the limit must count papers.
    """
    stream = native.parse_native_response(
        make_response(
            *[
                (f"c{n}", f"p{n}", 0.01, [(f"c{n}s", 9.0), (f"c{n}s2", 8.0)])
                for n in range(10)
            ]
        )
    )

    kept = hybrid._truncate_hits(stream, 10, by_paper=True)

    assert len({hit.paper_id for hit in kept}) == 10
    assert len(kept) == 30  # winner + two siblings each
    # Sanity check on the alternative: the plain slice really is shorter.
    assert len(hybrid._truncate_hits(stream, 10, by_paper=False)) == 10


def test_chunk_wise_truncation_is_still_a_plain_slice() -> None:
    """The Python path must keep its historical semantics."""
    hits = collapsed_hits()

    assert [
        hit.chunk_id for hit in hybrid._truncate_hits(hits, 2, by_paper=False)
    ] == ["c1", "c1a"]


def test_the_native_path_keeps_evidence_without_reranking(monkeypatch) -> None:
    monkeypatch.setattr(native, "native_search", lambda *a, **k: collapsed_hits())

    hits = hybrid.search_chunks("q", "hybrid", 10, backend="native")

    assert [hit.paper_id for hit in hits] == ["p1", "p1", "p1", "p2", "p2"]


def test_the_rerank_pool_lets_a_paper_surface_a_better_chunk(monkeypatch) -> None:
    """Scoring only the RRF winner measured as ``ndcg@1 -0.10`` vs the Python path."""
    sent: list[list[str]] = []

    def fake_rerank(query, texts, top_n=None):
        sent.append(list(texts))
        return [
            RerankScore(index=index, score=float(len(texts) - index))
            for index in range(len(texts))
        ]

    monkeypatch.setattr(native, "native_search", lambda *a, **k: collapsed_hits())
    monkeypatch.setattr(hybrid.rerank_service, "rerank_texts", fake_rerank)

    hits = hybrid.search_chunks("q", "hybrid", 10, rerank=True, backend="native")

    assert sent == [
        ["text of c1", "text of c1a", "text of c1b", "text of c2", "text of c2a"]
    ]
    # Each paper stays contiguous: its winner first, its siblings behind it.
    assert [hit.chunk_id for hit in hits] == ["c1", "c1a", "c1b", "c2", "c2a"]
    assert all(hit.rerank_score is not None for hit in hits)


def test_the_rerank_pool_is_capped_per_paper(monkeypatch) -> None:
    """``RERANK_CANDIDATES`` chunks per paper, however many siblings came back."""
    per_paper = hybrid._rerank_chunks_per_paper()
    stream = native.parse_native_response(
        make_response(
            ("c1", "p1", 0.03, [(f"c1s{n}", 9.0) for n in range(per_paper + 5)])
        )
    )
    sent: list[list[str]] = []

    def fake_rerank(query, texts, top_n=None):
        sent.append(list(texts))
        return [RerankScore(index=i, score=-float(i)) for i in range(len(texts))]

    monkeypatch.setattr(native, "native_search", lambda *a, **k: stream)
    monkeypatch.setattr(hybrid.rerank_service, "rerank_texts", fake_rerank)

    hybrid.search_chunks("q", "hybrid", 10, rerank=True, backend="native")

    assert len(sent[0]) == per_paper
    assert sent[0][0] == "text of c1"  # the winner is scored first
    assert sent[0][-1] == f"text of c1s{per_paper - 2}"


def test_siblings_inherit_their_winners_rerank_score(monkeypatch) -> None:
    def fake_rerank(query, texts, top_n=None):
        return [
            RerankScore(index=index, score=-float(index))
            for index in range(len(texts))
        ]

    monkeypatch.setattr(native, "native_search", lambda *a, **k: collapsed_hits())
    monkeypatch.setattr(hybrid.rerank_service, "rerank_texts", fake_rerank)

    hits = hybrid.search_chunks("q", "hybrid", 10, rerank=True, backend="native")

    winners = {hit.paper_id: hit for hit in hits if hit.primary}
    for hit in hits:
        if hit.primary:
            continue
        winner = winners[hit.paper_id]
        # Evidence on the same scale as the ranking, never a foreign-unit score.
        assert hit.score == pytest.approx(winner.score)
        assert hit.rerank_score == pytest.approx(winner.rerank_score)
        assert hit.retrieval_score == pytest.approx(winner.retrieval_score)


def test_extra_evidence_beyond_the_rerank_budget_is_not_scored(monkeypatch) -> None:
    """Evidence richness must not reach the ranking: same pool, same order.

    Siblings past the rerank budget are carried as evidence only, so raising what
    the UI asks for cannot move a paper.
    """
    per_paper = hybrid._rerank_chunks_per_paper()
    sent: list[list[str]] = []

    def fake_rerank(query, texts, top_n=None):
        sent.append(list(texts))
        return [
            RerankScore(index=index, score=-float(index))
            for index in range(len(texts))
        ]

    monkeypatch.setattr(hybrid.rerank_service, "rerank_texts", fake_rerank)

    orders: list[list[str]] = []
    for inner in (per_paper - 1, per_paper + 6):
        stream = native.parse_native_response(
            make_response(
                ("c1", "p1", 0.03, [(f"c1s{n}", 13.0) for n in range(inner)]),
                ("c2", "p2", 0.02, [(f"c2s{n}", 12.0) for n in range(inner)]),
            )
        )
        monkeypatch.setattr(native, "native_search", lambda *a, _s=stream, **k: _s)

        hits = hybrid.search_chunks("q", "hybrid", 10, rerank=True, backend="native")
        orders.append([hit.paper_id for hit in hits if hit.primary])

    assert sent[0] == sent[1]
    assert len(sent[0]) == 2 * per_paper  # both papers filled their budget
    assert orders[0] == orders[1] == ["p1", "p2"]


# --------------------------------------------------------------------------- #
# pipeline objects (deployment state)
# --------------------------------------------------------------------------- #
def test_ensure_pipelines_creates_what_is_missing() -> None:
    client = FakePipelineClient()

    report = native.ensure_pipelines(client=client)

    assert {item["name"] for item in report} == set(native.PIPELINE_BODIES)
    assert all(item["action"] == "created" for item in report)
    assert {name for _, name, _ in client.transport.writes} == set(native.PIPELINE_BODIES)


def test_ensure_pipelines_is_idempotent() -> None:
    client = FakePipelineClient(native.PIPELINE_BODIES)

    report = native.ensure_pipelines(client=client)

    assert all(item["action"] == "kept" for item in report)
    assert client.transport.writes == []


def test_ensure_pipelines_rewrites_a_drifted_body() -> None:
    drifted = dict(native.PIPELINE_BODIES)
    drifted["paperbox-rrf60"] = {"description": "stale", "phase_results_processors": []}
    client = FakePipelineClient(drifted)

    report = native.ensure_pipelines(client=client)

    assert {item["name"]: item["action"] for item in report}["paperbox-rrf60"] == "updated"
    assert [name for _, name, _ in client.transport.writes] == ["paperbox-rrf60"]


def test_ensure_pipelines_dry_run_reports_without_writing() -> None:
    client = FakePipelineClient()

    report = native.ensure_pipelines(client=client, dry_run=True)

    assert all(item["changed"] for item in report)
    assert client.transport.writes == []


def test_the_rrf_pipeline_matches_the_app_side_rank_constant() -> None:
    processor = native.PIPELINE_BODIES[native.PIPELINE_RRF]["phase_results_processors"][0]

    assert processor["score-ranker-processor"]["combination"] == {
        "technique": "rrf",
        "rank_constant": hybrid.DEFAULT_RRF_K,
    }


def test_the_evidence_budget_matches_what_the_aggregation_keeps() -> None:
    """Asking for more siblings than ``MAX_EVIDENCE`` would only grow the payload."""
    assert native.DEFAULT_INNER_HITS == search_service.MAX_EVIDENCE


# --------------------------------------------------------------------------- #
# request/response schema
# --------------------------------------------------------------------------- #
def test_the_request_accepts_and_normalizes_a_backend() -> None:
    assert SearchRequest(query="q", backend=" NATIVE ").backend == "native"
    assert SearchRequest(query="q").backend is None


def test_an_unknown_backend_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="q", backend="fast")


def test_the_response_reports_the_backend_it_used() -> None:
    """The response echoes the backend, defaulting to the deployed one.

    Pinned to ``DEFAULT_BACKEND`` rather than a literal: when the default moved
    from ``python`` to ``native`` (2026-10-01 定档) this assertion was the only
    one in the suite that had to change.
    """
    assert (
        SearchResponse(query="q", mode="hybrid", total=0, candidates=0, took_ms=1.0).backend
        == native.DEFAULT_BACKEND
    )
    assert (
        SearchResponse(
            query="q", mode="hybrid", total=0, candidates=0, took_ms=1.0, backend="native"
        ).backend
        == "native"
    )
    assert (
        SearchResponse(
            query="q", mode="hybrid", total=0, candidates=0, took_ms=1.0, backend="python"
        ).backend
        == "python"
    )
