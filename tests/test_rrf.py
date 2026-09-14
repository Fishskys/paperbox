"""Unit tests for reciprocal rank fusion (MVP-SPEC section 8)."""

from __future__ import annotations

import pytest

from app.search.ranking import DEFAULT_RRF_K, ids_only, rrf_fuse, rrf_score


def test_single_list_keeps_its_order() -> None:
    assert ids_only(rrf_fuse([["a", "b", "c"]])) == ["a", "b", "c"]


def test_scores_follow_the_reciprocal_rank_formula() -> None:
    fused = dict(rrf_fuse([["a", "b"]], k=60))
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 62)


def test_documents_in_both_lists_outrank_single_leg_documents() -> None:
    keyword = ["a", "b", "c"]
    semantic = ["c", "d", "a"]
    fused = ids_only(rrf_fuse([keyword, semantic]))
    # a and c appear twice, b and d once -> both duplicates come first
    assert set(fused[:2]) == {"a", "c"}
    assert set(fused[2:]) == {"b", "d"}


def test_agreement_on_the_top_rank_wins() -> None:
    keyword = ["x", "y"]
    semantic = ["x", "y"]
    assert ids_only(rrf_fuse([keyword, semantic])) == ["x", "y"]


def test_empty_input_and_empty_lists() -> None:
    assert rrf_fuse([]) == []
    assert rrf_fuse([[], []]) == []


def test_duplicate_ids_in_one_list_are_counted_once() -> None:
    fused = dict(rrf_fuse([["a", "a", "b"]]))
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 63)


def test_scores_are_always_positive_and_sorted_descending() -> None:
    fused = rrf_fuse([["a", "b", "c"], ["b", "a"], ["d"]])
    scores = [score for _, score in fused]
    assert scores == sorted(scores, reverse=True)
    assert all(score > 0 for score in scores)


def test_non_string_ids_are_coerced() -> None:
    assert ids_only(rrf_fuse([[1, 2], ["2"]]))[0] == "2"


def test_k_parameter_changes_the_scale() -> None:
    assert rrf_fuse([["a"]], k=1)[0][1] == pytest.approx(0.5)
    assert rrf_score(0, k=1) == pytest.approx(0.5)


def test_default_k_matches_spec() -> None:
    assert DEFAULT_RRF_K == 60


def test_invalid_arguments_rejected() -> None:
    with pytest.raises(ValueError):
        rrf_fuse([["a"]], k=0)
    with pytest.raises(ValueError):
        rrf_score(-1)
