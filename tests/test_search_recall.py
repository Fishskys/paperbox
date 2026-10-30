"""Explicit paper recall windows, pagination and opt-out model boundaries."""

import pytest
from pydantic import ValidationError

from app.schemas.search import SearchRequest
from app.search import hybrid, native
from app.services import embedding_service, search_pipeline, search_service
from tests.test_native_hybrid import FakeClient, make_response
from tests.test_search_api import client, factory  # noqa: F401


def test_candidate_window_validation():
    assert SearchRequest(query="timing", recall_k=100).backend == "native"
    assert SearchRequest(query="timing", recall_k=35, top_k=20, offset=20).offset == 20
    for values in ({"recall_k": 5, "top_k": 10}, {"recall_k": 100, "offset": 100},
                   {"recall_k": 100, "backend": "python"}, {"offset": 20}):
        with pytest.raises(ValidationError):
            SearchRequest(query="timing", **values)


@pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
def test_explicit_recall_counts_papers_and_pages_the_same_pool(monkeypatch, mode):
    response = make_response(*[(f"chunk-{i}", f"paper-{i}", float(10-i), []) for i in range(5)])
    engine = FakeClient(response)
    monkeypatch.setattr(embedding_service, "embed_text", lambda query: [0.1, 0.2])
    monkeypatch.setattr(native, "embed_text", lambda query: pytest.fail("query vector should be reused"))
    telemetry = {}
    outcome = search_service.search_papers("timing", mode, 2, recall_k=5, offset=2,
        client=engine, count_total=False, telemetry=telemetry)
    assert [paper.paper_id for paper in outcome.results] == ["paper-2", "paper-3"]
    assert engine.calls[0]["body"]["size"] == 5
    assert engine.calls[0]["body"]["collapse"]["field"] == "paper_id"
    assert telemetry["recalled_papers"] == 5 and telemetry["backend"] == "native"
    assert outcome.total == 5


def test_explicit_false_bypasses_query_rewriter_and_reports_disabled_rerank(client, monkeypatch):
    monkeypatch.setattr(search_pipeline, "maybe_rewrite", lambda query: pytest.fail("rewrite must be bypassed"))
    monkeypatch.setattr(search_pipeline, "log_search", lambda **kw: None)
    monkeypatch.setattr(search_service, "search_papers", lambda *a, **kw: search_service.SearchOutcome([], 0, 0))
    response = client.post("/api/search", json={"query": "timing", "rewrite": False, "recall_k": 100})
    assert response.status_code == 200
    assert response.json()["rewrite"]["applied"] is False
    assert response.json()["rerank"]["status"] == "not_requested"
