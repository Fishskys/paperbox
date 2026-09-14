"""Two-stage retrieval with the cross-encoder reranker (SPEC-P1 section D2).

``rerank_service`` is exercised against a fake ``httpx`` (no service contact)
and ``search_chunks`` against a stubbed reranker, so the candidate window,
the reordering and the response contract are all pinned without network.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.schemas.search import SearchResponse, SearchResult
from app.search import hybrid
from app.services import rerank_service
from app.services.rerank_service import RerankScore


def make_hit(chunk_id: str, score: float, text: str = "some chunk text") -> hybrid.ChunkHit:
    return hybrid.ChunkHit(
        chunk_id=chunk_id,
        paper_id=f"paper-{chunk_id}",
        score=score,
        text=text,
    )


# --------------------------------------------------------------------------- #
# rerank_service: degradation
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://rerank/rerank"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_rerank_texts_returns_none_when_the_service_raises(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(rerank_service.httpx, "post", boom)

    assert rerank_service.rerank_texts("q", ["a", "b"]) is None


def test_rerank_texts_returns_none_on_http_500(monkeypatch) -> None:
    monkeypatch.setattr(
        rerank_service.httpx, "post", lambda *a, **kw: FakeResponse({}, status_code=500)
    )

    assert rerank_service.rerank_texts("q", ["a", "b"]) is None


def test_rerank_texts_returns_none_on_count_mismatch(monkeypatch) -> None:
    payload = {"results": [{"index": 0, "score": 0.9}], "model": "m", "took_ms": 1}
    monkeypatch.setattr(
        rerank_service.httpx, "post", lambda *a, **kw: FakeResponse(payload)
    )

    assert rerank_service.rerank_texts("q", ["a", "b", "c"]) is None


def test_rerank_texts_returns_none_on_non_json_body(monkeypatch) -> None:
    monkeypatch.setattr(
        rerank_service.httpx,
        "post",
        lambda *a, **kw: FakeResponse(ValueError("not json")),
    )

    assert rerank_service.rerank_texts("q", ["a"]) is None


def test_rerank_texts_returns_none_on_malformed_results(monkeypatch) -> None:
    monkeypatch.setattr(
        rerank_service.httpx,
        "post",
        lambda *a, **kw: FakeResponse({"results": [{"index": "x", "score": "y"}]}),
    )

    assert rerank_service.rerank_texts("q", ["a"]) is None


def test_rerank_texts_is_none_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rerank_enabled", False)

    assert rerank_service.rerank_texts("q", ["a"]) is None


def test_rerank_texts_parses_and_sorts_by_score(monkeypatch) -> None:
    payload = {
        "results": [
            {"index": 2, "score": 0.1},
            {"index": 0, "score": 0.9},
            {"index": 1, "score": 0.5},
        ],
        "model": "Xenova/ms-marco-MiniLM-L-6-v2",
        "took_ms": 12,
    }
    monkeypatch.setattr(
        rerank_service.httpx, "post", lambda *a, **kw: FakeResponse(payload)
    )

    scores = rerank_service.rerank_texts("q", ["a", "b", "c"])

    assert scores == [
        RerankScore(index=0, score=0.9),
        RerankScore(index=1, score=0.5),
        RerankScore(index=2, score=0.1),
    ]


def test_rerank_texts_sends_top_n_and_truncated_documents(monkeypatch) -> None:
    captured: dict = {}

    def capture(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse({"results": [{"index": 0, "score": 1.0}]})

    monkeypatch.setattr(rerank_service.httpx, "post", capture)

    rerank_service.rerank_texts("query", ["x" * 5000], top_n=3)

    assert captured["url"].endswith("/rerank")
    assert captured["json"]["top_n"] == 3
    assert len(captured["json"]["documents"][0]) == rerank_service.MAX_DOCUMENT_CHARS
    assert captured["timeout"] == settings.rerank_timeout


def test_rerank_texts_empty_documents_needs_no_call(monkeypatch) -> None:
    def fail(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("no HTTP call expected for an empty document list")

    monkeypatch.setattr(rerank_service.httpx, "post", fail)

    assert rerank_service.rerank_texts("q", []) == []


def test_is_available_is_false_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rerank_enabled", False)

    assert rerank_service.is_available() is False


def test_is_available_true_on_healthy_service(monkeypatch) -> None:
    monkeypatch.setattr(
        rerank_service.httpx,
        "get",
        lambda *a, **kw: FakeResponse({"status": "ok", "rerank_model": "m"}),
    )

    assert rerank_service.is_available() is True


def test_is_available_false_on_error(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(rerank_service.httpx, "get", boom)

    assert rerank_service.is_available() is False


# --------------------------------------------------------------------------- #
# search_chunks: candidate window, reordering, degradation
# --------------------------------------------------------------------------- #
@pytest.fixture()
def rare_hits(monkeypatch):
    """Replace the three retrieval legs with a deterministic candidate list."""
    state = {"keyword_size": None, "semantic_size": None}

    def fake_keyword(query, size, filters, *, client=None, index=None):
        state["keyword_size"] = size
        return [make_hit(f"k{i}", 1.0 - i / 100) for i in range(size)]

    def fake_semantic(query, size, filters, *, client=None, index=None):
        state["semantic_size"] = size
        return [make_hit(f"s{i}", 0.5 - i / 100) for i in range(size)]

    monkeypatch.setattr(hybrid, "_keyword_hits", fake_keyword)
    monkeypatch.setattr(hybrid, "_semantic_hits", fake_semantic)
    return state


def test_rerank_off_returns_top_k(rare_hits) -> None:
    hits = hybrid.search_chunks("q", "keyword", 3, rerank=False)

    assert len(hits) == 3
    assert rare_hits["keyword_size"] == 3


def test_rerank_on_over_fetches_top_k_times_candidates(rare_hits) -> None:
    hybrid.search_chunks("q", "keyword", 3, rerank=True)

    assert rare_hits["keyword_size"] == 3 * settings.rerank_candidates


def test_rerank_on_returns_top_k_times_two(rare_hits, monkeypatch) -> None:
    monkeypatch.setattr(
        hybrid.rerank_service,
        "rerank_texts",
        lambda query, texts, top_n=None: [
            RerankScore(index=index, score=1.0 - index / 100)
            for index in range(len(texts))
        ],
    )

    hits = hybrid.search_chunks("q", "keyword", 3, rerank=True)

    assert len(hits) == 6  # top_k * 2


def test_rerank_reorders_and_keeps_the_retrieval_score(rare_hits, monkeypatch) -> None:
    def reversed_scores(query, texts, top_n=None):
        # Credit the *later* candidates most, so the BM25 order must flip.
        return [
            RerankScore(index=index, score=float(index))
            for index in range(len(texts))
        ]

    monkeypatch.setattr(hybrid.rerank_service, "rerank_texts", reversed_scores)

    hits = hybrid.search_chunks("q", "keyword", 4, rerank=True)

    # ``top_k * 2`` window, reordered best cross-encoder score first: the
    # candidate window is ``top_k * 5`` wide, so k19..k15 win.
    assert len(hits) == 8
    assert [hit.chunk_id for hit in hits[:3]] == ["k19", "k18", "k17"]
    # The BM25 order is gone: k0 (candidate 0) ranks last in the window...
    assert hits[-1].chunk_id == "k12"
    assert "k0" not in [hit.chunk_id for hit in hits]

    best = hits[0]
    # score carries the normalized cross-encoder value ...
    assert best.score == 1.0
    assert best.rerank_score == pytest.approx(19.0)
    # ... and retrieval_score keeps the first-stage BM25 score of that chunk.
    assert best.retrieval_score == pytest.approx(0.81)  # k19, not k0
    assert hits[1].retrieval_score == pytest.approx(0.82)  # k18
    assert all(hit.rerank_score is not None for hit in hits)


def test_rerank_degrades_to_the_first_stage_order(rare_hits, monkeypatch) -> None:
    monkeypatch.setattr(
        hybrid.rerank_service, "rerank_texts", lambda query, texts, top_n=None: None
    )

    hits = hybrid.search_chunks("q", "keyword", 3, rerank=True)

    # Degraded: first-stage order (inside the ``top_k * 2`` window) is kept.
    assert [hit.chunk_id for hit in hits] == ["k0", "k1", "k2", "k3", "k4", "k5"]
    assert all(hit.rerank_score is None for hit in hits)
    assert all(hit.retrieval_score is None for hit in hits)


def test_rerank_scores_are_normalized_into_zero_one(rare_hits, monkeypatch) -> None:
    monkeypatch.setattr(
        hybrid.rerank_service,
        "rerank_texts",
        lambda query, texts, top_n=None: [
            RerankScore(index=index, score=10.0 - index) for index in range(len(texts))
        ],
    )

    hits = hybrid.search_chunks("q", "keyword", 3, rerank=True)

    assert hits[0].score == 1.0
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)


def test_hybrid_mode_also_reranks(rare_hits, monkeypatch) -> None:
    monkeypatch.setattr(
        hybrid.rerank_service,
        "rerank_texts",
        lambda query, texts, top_n=None: [
            RerankScore(index=index, score=1.0) for index in range(len(texts))
        ],
    )

    hits = hybrid.search_chunks("q", "hybrid", 2, rerank=True)

    assert hits, "hybrid + rerank returned nothing"
    assert all(hit.rerank_score is not None for hit in hits)


# --------------------------------------------------------------------------- #
# response contract
# --------------------------------------------------------------------------- #
def test_search_result_exposes_both_scores() -> None:
    result = SearchResult(
        paper_id="p1",
        title="T",
        score=0.8,
        relevance="medium",
        retrieval_score=0.3,
        rerank_score=0.95,
    )

    payload = result.model_dump()

    assert payload["retrieval_score"] == 0.3
    assert payload["rerank_score"] == 0.95


def test_search_result_scores_default_to_none() -> None:
    result = SearchResult(paper_id="p1", title="T", score=0.5, relevance="low")

    payload = result.model_dump()

    assert payload["retrieval_score"] is None
    assert payload["rerank_score"] is None


def test_response_has_a_rerank_block() -> None:
    response = SearchResponse(query="q", mode="hybrid", total=1, took_ms=1.5)

    payload = response.model_dump()

    assert payload["rerank"] == {"enabled": False, "model": None, "took_ms": None}
    assert set(payload) == {
        "query",
        "rewritten_query",
        "mode",
        "total",
        "took_ms",
        "rerank",
        "rewrite",
        "results",
    }


def test_response_rerank_block_can_report_a_model() -> None:
    from app.schemas.search import SearchRerankInfo

    response = SearchResponse(
        query="q",
        mode="hybrid",
        total=1,
        took_ms=2.0,
        rerank=SearchRerankInfo(
            enabled=True, model="Xenova/ms-marco-MiniLM-L-6-v2", took_ms=37
        ),
    )

    assert response.model_dump()["rerank"] == {
        "enabled": True,
        "model": "Xenova/ms-marco-MiniLM-L-6-v2",
        "took_ms": 37,
    }


def test_logged_result_shape_keeps_both_scores() -> None:
    """The search log stores the same score fields the client sees (P1 B)."""
    from app.api import search as search_api

    payload = [
        SearchResult(
            paper_id="p1",
            title="T",
            score=0.8,
            relevance="medium",
            retrieval_score=0.3,
            rerank_score=0.95,
        )
    ]

    logged = search_api.serialize_results(payload)[0]

    assert logged["retrieval_score"] == 0.3
    assert logged["rerank_score"] == 0.95
    assert logged["evidence_count"] == 0


def test_rerank_request_field_is_honoured() -> None:
    from app.schemas.search import SearchRequest

    assert SearchRequest(query="q").rerank is False
    assert SearchRequest(query="q", rerank=True).rerank is True
