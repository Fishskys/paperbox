"""Unit tests for paper-level aggregation (MVP-SPEC section 8).

Only pure functions are exercised: chunk hits are hand-built, no OpenSearch or
PostgreSQL connection is opened.
"""

from __future__ import annotations

import pytest

from app.search.hybrid import ChunkHit
from app.services.search_service import (
    EVIDENCE_TEXT_LIMIT,
    MAX_EVIDENCE,
    aggregate_papers,
    classify_relevance,
    truncate_text,
)


def hit(
    chunk_id: str,
    paper_id: str,
    score: float,
    *,
    text: str = "some evidence text",
    title: str = "Paper Title",
    page: int | None = 1,
    section: str | None = "2 Method",
) -> ChunkHit:
    return ChunkHit(
        chunk_id=chunk_id,
        paper_id=paper_id,
        score=score,
        text=text,
        title=title,
        page_start=page,
        page_end=page,
        section=section,
        section_title=section,
        authors=["Alice"],
        year=2021,
        doi="10.1/x",
    )


# --------------------------------------------------------------------------- #
# classify_relevance
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (1.0, "high"),
        (0.9, "high"),
        (0.899, "medium"),
        (0.6, "medium"),
        (0.599, "low"),
        (0.0, "low"),
    ],
)
def test_relevance_thresholds(score: float, expected: str) -> None:
    assert classify_relevance(score) == expected


# --------------------------------------------------------------------------- #
# truncate_text
# --------------------------------------------------------------------------- #


def test_truncate_leaves_short_text_untouched() -> None:
    assert truncate_text("short") == "short"


def test_truncate_cuts_at_the_limit_and_marks_it() -> None:
    result = truncate_text("x" * 900)
    assert result.startswith("x" * 100)
    assert len(result) <= EVIDENCE_TEXT_LIMIT + 3
    assert result.endswith("...")


def test_truncate_handles_none_and_whitespace() -> None:
    assert truncate_text(None) == ""
    assert truncate_text("   ") == ""


# --------------------------------------------------------------------------- #
# aggregate_papers
# --------------------------------------------------------------------------- #


def test_paper_score_is_the_max_chunk_score() -> None:
    results = aggregate_papers(
        [hit("c1", "p1", 0.02), hit("c2", "p1", 0.05), hit("c3", "p1", 0.03)]
    )
    assert len(results) == 1
    assert results[0].score == pytest.approx(0.05)
    assert results[0].matched_chunks == 3


def test_papers_are_sorted_by_descending_score() -> None:
    results = aggregate_papers(
        [hit("c1", "p1", 0.01), hit("c2", "p2", 0.09), hit("c3", "p3", 0.04)]
    )
    assert [item.paper_id for item in results] == ["p2", "p3", "p1"]


def test_top_k_limits_the_number_of_papers() -> None:
    hits = [hit(f"c{i}", f"p{i}", 0.1 - i * 0.01) for i in range(5)]
    assert len(aggregate_papers(hits, top_k=2)) == 2


def test_evidence_is_capped_at_three_per_paper() -> None:
    hits = [hit(f"c{i}", "p1", 0.1 - i * 0.01) for i in range(6)]
    results = aggregate_papers(hits)
    assert len(results[0].evidence) == MAX_EVIDENCE == 3
    # the strongest chunks are kept
    assert [item.chunk_id for item in results[0].evidence] == ["c0", "c1", "c2"]


def test_evidence_carries_the_documented_fields() -> None:
    results = aggregate_papers([hit("c1", "p1", 0.5, page=7, section="3.1 Encoder")])
    evidence = results[0].evidence[0].to_dict()
    assert evidence == {
        "chunk_id": "c1",
        "page": 7,
        "section": "3.1 Encoder",
        "text": "some evidence text",
    }


def test_evidence_text_is_truncated_to_500_chars() -> None:
    results = aggregate_papers([hit("c1", "p1", 0.5, text="y" * 1200)])
    text = results[0].evidence[0].text
    assert len(text) <= EVIDENCE_TEXT_LIMIT + 3
    assert text.endswith("...")


def test_hits_without_a_paper_id_are_skipped() -> None:
    assert aggregate_papers([hit("c1", "", 0.5)]) == []


def test_empty_input() -> None:
    assert aggregate_papers([]) == []


def test_payload_shape_matches_the_api_contract() -> None:
    payload = aggregate_papers([hit("c1", "p1", 0.95)])[0].to_dict()
    assert set(payload) == {
        "paper_id",
        "title",
        "authors",
        "year",
        "doi",
        "score",
        "relevance",
        "retrieval_score",
        "rerank_score",
        "evidence",
    }
    assert payload["relevance"] == "high"
    # Without reranking both score fields stay null (P1 D2 contract).
    assert payload["retrieval_score"] is None
    assert payload["rerank_score"] is None


def test_paper_metadata_comes_from_the_best_chunk() -> None:
    best = hit("c1", "p1", 0.8, title="Best Paper")
    worse = hit("c2", "p1", 0.2, title="Worse Paper")
    results = aggregate_papers([worse, best])
    assert results[0].title == "Best Paper"


def test_aggregation_is_deterministic_for_equal_scores() -> None:
    hits = [hit("c1", "p2", 0.5), hit("c2", "p1", 0.5)]
    assert [item.paper_id for item in aggregate_papers(hits)] == ["p2", "p1"]
