"""Unit tests for the evaluation metrics (SPEC-P1 section E).

Everything is a pure function over plain data, so no service is contacted.
Hand-computed expectations are spelled out next to the assertion they belong
to; NDCG uses ``gain = 2 ** grade - 1`` and ``discount = log2(rank + 1)``.
"""

from __future__ import annotations

import math

import pytest

from app.eval import metrics


# --------------------------------------------------------------------------- #
# hit_rate_at_k
# --------------------------------------------------------------------------- #
def test_hit_rate_is_one_for_a_perfect_ranking() -> None:
    relevance = {"a": 2, "b": 1}
    assert metrics.hit_rate_at_k(["a", "b", "c"], relevance, 3) == 1.0


def test_hit_rate_counts_a_single_hit_anywhere_in_the_window() -> None:
    relevance = {"c": 2}
    assert metrics.hit_rate_at_k(["a", "b", "c"], relevance, 3) == 1.0


def test_hit_rate_is_zero_when_the_hit_sits_just_outside_the_window() -> None:
    relevance = {"c": 2}
    assert metrics.hit_rate_at_k(["a", "b", "c"], relevance, 2) == 0.0


def test_hit_rate_is_zero_for_a_fully_reversed_ranking_when_k_is_tight() -> None:
    relevance = {"a": 2}
    assert metrics.hit_rate_at_k(["z", "y", "x"], relevance, 3) == 0.0


def test_hit_rate_is_zero_without_labels() -> None:
    assert metrics.hit_rate_at_k(["a", "b"], {}, 5) == 0.0


def test_hit_rate_ignores_zero_grades() -> None:
    # grade 0 means "not relevant", so it must not count as a hit.
    assert metrics.hit_rate_at_k(["a"], {"a": 0}, 5) == 0.0


# --------------------------------------------------------------------------- #
# recall_at_k
# --------------------------------------------------------------------------- #
def test_recall_is_one_when_every_labelled_paper_is_found() -> None:
    relevance = {"a": 2, "b": 1, "c": 2}
    assert metrics.recall_at_k(["a", "b", "c"], relevance, 3) == 1.0


def test_recall_is_partial_when_half_of_the_labels_are_missing() -> None:
    relevance = {"a": 2, "b": 1}
    # 1 of the 2 relevant papers is inside the top-2.
    assert metrics.recall_at_k(["a", "x"], relevance, 2) == 0.5


def test_recall_denominator_is_the_label_count_not_k() -> None:
    relevance = {"a": 2}
    # A single label with a single slot: finding it is a full recall.
    assert metrics.recall_at_k(["a", "b", "c"], relevance, 1) == 1.0


def test_recall_is_zero_for_a_fully_reversed_ranking_when_k_is_tight() -> None:
    relevance = {"a": 2}
    assert metrics.recall_at_k(["c", "b", "a"], relevance, 2) == 0.0


def test_recall_counts_duplicate_ids_once() -> None:
    relevance = {"a": 2, "b": 1}
    assert metrics.recall_at_k(["a", "a", "a"], relevance, 3) == 0.5


def test_recall_is_zero_without_labels() -> None:
    assert metrics.recall_at_k(["a", "b"], {}, 3) == 0.0


# --------------------------------------------------------------------------- #
# mrr
# --------------------------------------------------------------------------- #
def test_mrr_is_one_for_a_perfect_ranking() -> None:
    assert metrics.mrr(["a", "b"], {"a": 2, "b": 1}) == 1.0


def test_mrr_uses_the_reciprocal_rank_of_the_first_hit() -> None:
    assert metrics.mrr(["x", "y", "a"], {"a": 2}) == pytest.approx(1 / 3)


def test_mrr_is_zero_for_a_fully_reversed_ranking() -> None:
    assert metrics.mrr(["z", "y", "x"], {"a": 2}) == 0.0


def test_mrr_is_zero_without_labels() -> None:
    assert metrics.mrr(["a"], {}) == 0.0


def test_mrr_skips_zero_grades() -> None:
    assert metrics.mrr(["a", "b"], {"a": 0, "b": 2}) == 0.5


def test_mrr_looks_beyond_k() -> None:
    # MRR has no k: the first hit at rank 12 still scores.
    assert metrics.mrr([*(f"x{i}" for i in range(11)), "a"], {"a": 2}) == pytest.approx(1 / 12)


# --------------------------------------------------------------------------- #
# ndcg_at_k — hand-computed
# --------------------------------------------------------------------------- #
def test_ndcg_is_one_for_the_ideal_ranking() -> None:
    # DCG = 3/log2(2) + 1/log2(3) ; IDCG is the same list sorted by grade.
    relevance = {"a": 2, "b": 1}
    assert metrics.ndcg_at_k(["a", "b"], relevance, 2) == pytest.approx(1.0)


def test_ndcg_hand_computed_single_grade_two_at_rank_two() -> None:
    # grade 2 -> gain 3, rank 2 -> discount log2(3)
    # DCG  = 3 / log2(3) = 1.892789...
    # IDCG = 3 / log2(2) = 3.0
    expected = (3 / math.log2(3)) / 3.0
    assert expected == pytest.approx(0.6309297535714574)
    assert metrics.ndcg_at_k(["x", "a"], {"a": 2}, 2) == pytest.approx(expected)


def test_ndcg_hand_computed_mixed_grades() -> None:
    # grades 2,1,1 at ranks 1,2,3:
    # DCG = 3/log2(2) + 1/log2(3) + 1/log2(4) = 3 + 0.630929... + 0.5
    # IDCG (ideal 2,1,1) is identical because the order already matches.
    dcg = 3 / math.log2(2) + 1 / math.log2(3) + 1 / math.log2(4)
    assert dcg == pytest.approx(4.130929753571457)
    assert metrics.ndcg_at_k(["a", "b", "c"], {"a": 2, "b": 1, "c": 1}, 3) == pytest.approx(1.0)


def test_ndcg_hand_computed_penalizes_a_swapped_pair() -> None:
    # Relevant pair a(2), b(1) but the ranking puts b first:
    # DCG  = 1/log2(2) + 3/log2(3) = 1 + 1.892789... = 2.892789...
    # IDCG = 3/log2(2) + 1/log2(3) = 3 + 0.630929... = 3.630929...
    dcg = 1 / math.log2(2) + 3 / math.log2(3)
    idcg = 3 / math.log2(2) + 1 / math.log2(3)
    expected = dcg / idcg
    assert dcg == pytest.approx(2.892789260714372)
    assert idcg == pytest.approx(3.6309297535714575)
    assert expected == pytest.approx(0.7967075809905066)
    assert metrics.ndcg_at_k(["b", "a"], {"a": 2, "b": 1}, 2) == pytest.approx(expected)


def test_ndcg_is_zero_for_a_fully_reversed_ranking_when_k_is_tight() -> None:
    assert metrics.ndcg_at_k(["z", "y", "x"], {"a": 2}, 3) == 0.0


def test_ndcg_is_zero_without_labels() -> None:
    # IDCG would be 0 -> guard against a division by zero.
    assert metrics.ndcg_at_k(["a", "b"], {}, 2) == 0.0


def test_ndcg_ignores_hits_beyond_k() -> None:
    relevance = {"a": 2}
    # Rank 3 hit with k=2 contributes nothing.
    assert metrics.ndcg_at_k(["x", "y", "a"], relevance, 2) == 0.0


def test_ndcg_single_relevant_paper_at_the_cut_off() -> None:
    # The only label sits at rank k = 3: DCG = 1/log2(4) = 0.5, IDCG = 1/log2(2).
    expected = (1 / math.log2(4)) / (1 / math.log2(2))
    assert expected == pytest.approx(0.5)
    assert metrics.ndcg_at_k(["x", "y", "a"], {"a": 1}, 3) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# edge cases shared by all metrics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "func",
    [
        lambda ranked, rel: metrics.hit_rate_at_k(ranked, rel, 3),
        lambda ranked, rel: metrics.recall_at_k(ranked, rel, 3),
        lambda ranked, rel: metrics.ndcg_at_k(ranked, rel, 3),
    ],
)
def test_k_larger_than_the_result_count_is_fine(func) -> None:
    # A short ranking is not an error; the missing slots are just misses.
    assert func(["a"], {"a": 2}) == 1.0


@pytest.mark.parametrize(
    "func",
    [
        lambda ranked, rel: metrics.hit_rate_at_k(ranked, rel, 3),
        lambda ranked, rel: metrics.recall_at_k(ranked, rel, 3),
        lambda ranked, rel: metrics.ndcg_at_k(ranked, rel, 3),
    ],
)
def test_empty_ranking_scores_zero(func) -> None:
    assert func([], {"a": 2}) == 0.0


@pytest.mark.parametrize("k", [0, -1])
def test_k_must_be_positive(k: int) -> None:
    with pytest.raises(ValueError):
        metrics.hit_rate_at_k(["a"], {"a": 2}, k)
    with pytest.raises(ValueError):
        metrics.recall_at_k(["a"], {"a": 2}, k)
    with pytest.raises(ValueError):
        metrics.ndcg_at_k(["a"], {"a": 2}, k)


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #
def test_aggregate_means_over_query_rows() -> None:
    rows = [
        {"metrics": {"hit_rate": 1.0, "recall": 1.0, "mrr": 1.0, "ndcg": 1.0}},
        {"metrics": {"hit_rate": 0.0, "recall": 0.5, "mrr": 0.5, "ndcg": 0.25}},
    ]
    summary = metrics.aggregate(rows)

    assert summary["hit_rate"] == {"mean": 0.5, "n": 2}
    assert summary["recall"] == {"mean": 0.75, "n": 2}
    assert summary["mrr"] == {"mean": 0.75, "n": 2}
    assert summary["ndcg"] == {"mean": 0.625, "n": 2}


def test_aggregate_accepts_flat_rows() -> None:
    summary = metrics.aggregate([{"hit_rate": 1.0, "mrr": 0.5}])

    assert summary["hit_rate"] == {"mean": 1.0, "n": 1}
    assert summary["mrr"] == {"mean": 0.5, "n": 1}
    # Metrics nobody reported are still present, with n = 0.
    assert summary["ndcg"] == {"mean": 0.0, "n": 0}


def test_aggregate_of_nothing_is_zero_with_no_observations() -> None:
    summary = metrics.aggregate([])

    assert set(summary) == set(metrics.METRIC_NAMES)
    assert all(entry == {"mean": 0.0, "n": 0} for entry in summary.values())


def test_aggregate_skips_failed_queries() -> None:
    rows = [
        {"metrics": {"hit_rate": 1.0, "mrr": 1.0}, "error": None},
        {"error": "HTTP 500"},  # failed query: no metrics at all
    ]
    summary = metrics.aggregate(rows)

    assert summary["hit_rate"] == {"mean": 1.0, "n": 1}
    assert summary["mrr"] == {"mean": 1.0, "n": 1}
