"""Search logging: pure helpers, degradation and row serialization (SPEC-P1 B).

No database is touched: ``serialize_results`` is a pure function and
``log_search`` is exercised against fake sessions that fail on purpose.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.db.models import SearchQuery
from app.services import search_log_service
from app.services.search_log_service import (
    RESULT_FIELDS,
    log_search,
    serialize_results,
    serialize_search_log,
)


def result(
    paper_id: str,
    score: float,
    *,
    title: str | None = None,
    evidence: list | None = None,
    evidence_count: int | None = None,
    matched_chunks: int | None = None,
    **extra,
) -> dict:
    payload = {"paper_id": paper_id, "title": title or f"Paper {paper_id}", "score": score}
    if evidence is not None:
        payload["evidence"] = evidence
    if evidence_count is not None:
        payload["evidence_count"] = evidence_count
    if matched_chunks is not None:
        payload["matched_chunks"] = matched_chunks
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------- #
# serialize_results
# --------------------------------------------------------------------------- #
def test_rank_starts_at_one_and_keeps_input_order() -> None:
    results = [result("p1", 0.9), result("p2", 0.7), result("p3", 0.5)]

    summary = serialize_results(results, 10)

    assert [item["paper_id"] for item in summary] == ["p1", "p2", "p3"]
    assert [item["rank"] for item in summary] == [1, 2, 3]


def test_truncates_to_limit() -> None:
    results = [result(f"p{index}", 1 - index / 10) for index in range(5)]

    assert len(serialize_results(results, 2)) == 2
    assert [item["paper_id"] for item in serialize_results(results, 2)] == ["p0", "p1"]


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_non_positive_limit_logs_nothing(limit: int) -> None:
    assert serialize_results([result("p1", 1.0)], limit) == []


def test_skips_non_dict_entries() -> None:
    results = [result("p1", 1.0), "not-a-dict", None, result("p2", 0.5)]

    summary = serialize_results(results, 10)

    assert [item["paper_id"] for item in summary] == ["p1", "p2"]
    assert [item["rank"] for item in summary] == [1, 2]


def test_field_set_matches_result_fields() -> None:
    summary = serialize_results([result("p1", 1.0)], 10)

    assert set(summary[0]) == set(RESULT_FIELDS)
    assert set(RESULT_FIELDS) == {
        "paper_id",
        "title",
        "rank",
        "score",
        "retrieval_score",
        "rerank_score",
        "evidence_count",
    }


def test_evidence_count_from_evidence_list() -> None:
    summary = serialize_results([result("p1", 1.0, evidence=[{}, {}, {}])], 10)

    assert summary[0]["evidence_count"] == 3


def test_evidence_count_from_explicit_field() -> None:
    summary = serialize_results([result("p1", 1.0, evidence_count=7)], 10)

    assert summary[0]["evidence_count"] == 7


def test_evidence_count_from_matched_chunks() -> None:
    summary = serialize_results([result("p1", 1.0, matched_chunks=4)], 10)

    assert summary[0]["evidence_count"] == 4


def test_evidence_count_defaults_to_zero() -> None:
    assert serialize_results([result("p1", 1.0)], 10)[0]["evidence_count"] == 0


def test_retrieval_and_rerank_scores_are_carried_through() -> None:
    summary = serialize_results(
        [result("p1", 0.8, retrieval_score=0.3, rerank_score=0.95)], 10
    )

    assert summary[0]["retrieval_score"] == 0.3
    assert summary[0]["rerank_score"] == 0.95
    assert summary[0]["score"] == 0.8


def test_retrieval_score_falls_back_to_score() -> None:
    assert serialize_results([result("p1", 0.42)], 10)[0]["retrieval_score"] == 0.42


def test_empty_input_is_empty_output() -> None:
    assert serialize_results([], 10) == []


# --------------------------------------------------------------------------- #
# log_search degradation
# --------------------------------------------------------------------------- #
class FailingSession:
    """Session stub whose write path blows up like a broken database."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or OperationalError("INSERT", {}, Exception("db down"))
        self.rolled_back = False
        self.added: list = []

    def add(self, row) -> None:
        self.added.append(row)

    def commit(self) -> None:
        raise self.error

    def rollback(self) -> None:
        self.rolled_back = True


def call_log_search(session, **overrides) -> None:
    payload = {
        "request_id": "req-1",
        "query": "sram leakage",
        "mode": "hybrid",
        "top_k": 5,
        "rerank": False,
        "filters": {"year_from": 2015},
        "candidates": 25,
        "returned": 2,
        "took_ms": 12.5,
        "results": [{"paper_id": "p1", "title": "T", "score": 1.0, "evidence": [{}]}],
    }
    payload.update(overrides)
    log_search(session, **payload)


def test_log_search_swallows_write_failures_and_rolls_back() -> None:
    session = FailingSession()

    call_log_search(session)  # must not raise

    assert session.added, "the row was never even created"
    assert session.rolled_back is True


def test_log_search_swallows_unexpected_errors() -> None:
    session = FailingSession(RuntimeError("something else"))

    call_log_search(session)

    assert session.rolled_back is True


def test_log_search_does_not_write_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "search_log_enabled", False)
    session = FailingSession()

    call_log_search(session)

    assert session.added == []
    assert session.rolled_back is False


class RecordingSession:
    """Session stub that records the row it was asked to persist."""

    def __init__(self) -> None:
        self.added: list = []
        self.commits = 0

    def add(self, row) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:  # pragma: no cover - not expected
        raise AssertionError("rollback should not be needed")


def test_log_search_persists_the_expected_row() -> None:
    session = RecordingSession()

    call_log_search(session, took_ms=12.5)

    assert session.commits == 1
    row = session.added[0]
    assert isinstance(row, SearchQuery)
    assert row.query == "sram leakage"
    assert row.mode == "hybrid"
    assert row.top_k == 5
    assert row.rerank is False
    assert row.filters == {"year_from": 2015}
    assert row.candidates == 25
    assert row.returned == 2
    assert row.took_ms == 13  # rounded to whole milliseconds
    assert row.request_id == "req-1"
    assert row.results[0]["rank"] == 1


def test_log_search_truncates_results_to_the_configured_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "search_log_results_limit", 2)
    session = RecordingSession()

    call_log_search(
        session,
        results=[result(f"p{index}", 1.0) for index in range(5)],
    )

    assert len(session.added[0].results) == 2


# --------------------------------------------------------------------------- #
# serialize_search_log
# --------------------------------------------------------------------------- #
def test_serialize_search_log_converts_datetime_and_passes_none_through() -> None:
    created = datetime(2026, 9, 12, 9, 30, tzinfo=timezone.utc)
    row = SearchQuery(
        id="11111111-1111-1111-1111-111111111111",
        request_id=None,
        query="low power sram",
        mode="keyword",
        top_k=10,
        rerank=False,
        filters=None,
        candidates=None,
        returned=0,
        took_ms=None,
        results=None,
    )
    row.created_at = created

    payload = serialize_search_log(row)

    assert payload["created_at"] == created.isoformat()
    assert payload["request_id"] is None
    assert payload["filters"] is None
    assert payload["candidates"] is None
    assert payload["took_ms"] is None
    assert payload["results"] is None
    assert payload["id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["rerank"] is False


def test_serialize_search_log_keeps_results() -> None:
    row = SearchQuery(
        id="2",
        request_id="req-9",
        query="q",
        mode="semantic",
        top_k=3,
        rerank=True,
        filters={"authors": ["a"]},
        candidates=15,
        returned=1,
        took_ms=42,
        results=[{"paper_id": "p1", "rank": 1, "score": 1.0}],
    )
    row.created_at = datetime(2026, 9, 12, tzinfo=timezone.utc)

    payload = serialize_search_log(row)

    assert payload["results"] == [{"paper_id": "p1", "rank": 1, "score": 1.0}]
    assert payload["filters"] == {"authors": ["a"]}
    assert payload["rerank"] is True
