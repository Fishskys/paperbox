"""Retrieval metrics for the evaluation loop (SPEC-P1 section E).

Everything here is a pure function over plain Python data so the whole module
is unit-testable without a service, a database or an index:

``relevance``
    ``{paper_id: grade}`` where ``grade`` is ``0`` (not relevant), ``1``
    (relevant) or ``2`` (highly relevant); an absent paper is not relevant.
``ranked_ids``
    Paper ids in retrieval order, best first. Chunk-level hits are usually
    collapsed into this list by the caller, keeping the best rank per paper.

Conventions shared by every metric:

* ``k`` must be positive; anything ``<= 0`` is rejected with ``ValueError``
  so a bad CLI flag fails loudly instead of silently scoring 0.
* Only the top ``k`` entries of ``ranked_ids`` are inspected, so a ranking
  shorter than ``k`` is fine (the missing slots count as misses).
* Metrics are defined even when ``relevance`` is empty: there is nothing to
  find, so every metric is ``0.0`` (``ndcg_at_k`` avoids a 0/0).
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "METRIC_NAMES",
    "aggregate",
    "hit_rate_at_k",
    "mrr",
    "ndcg_at_k",
    "recall_at_k",
]

#: Metrics reported by ``scripts/eval.py``, in report order.
METRIC_NAMES: tuple[str, ...] = ("hit_rate", "recall", "mrr", "ndcg")


def _check_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, int):
        raise TypeError(f"k must be an int, got {type(k).__name__}")
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    return k


def _relevant(relevance: Mapping[str, int] | None) -> dict[str, int]:
    """Normalize a relevance mapping into ``{id: grade}`` with ``grade > 0``."""
    if not relevance:
        return {}
    graded: dict[str, int] = {}
    for raw_id, raw_grade in relevance.items():
        try:
            grade = int(raw_grade)
        except (TypeError, ValueError):
            continue
        if grade > 0:
            graded[str(raw_id)] = grade
    return graded


def _top_k(ranked_ids: Iterable[str], k: int) -> list[str]:
    return [str(item) for item in list(ranked_ids)[:k]]


def hit_rate_at_k(
    ranked_ids: Sequence[str], relevance: Mapping[str, int], k: int
) -> float:
    """``1.0`` when at least one relevant paper is in the top ``k`` else ``0.0``.

    The denominator is the query, not the label set: a query with no relevant
    paper at all cannot be hit and scores ``0.0``.
    """
    _check_k(k)
    relevant = _relevant(relevance)
    if not relevant:
        return 0.0
    return 1.0 if any(item in relevant for item in _top_k(ranked_ids, k)) else 0.0


def recall_at_k(
    ranked_ids: Sequence[str], relevance: Mapping[str, int], k: int
) -> float:
    """Fraction of the labelled relevant papers that appear in the top ``k``.

    The denominator is the number of papers with ``grade > 0`` in
    ``relevance`` (not ``k``), so a query with a single label can still reach
    ``1.0``. Repeated ids in ``ranked_ids`` are counted once.
    """
    _check_k(k)
    relevant = _relevant(relevance)
    if not relevant:
        return 0.0
    found = {item for item in _top_k(ranked_ids, k) if item in relevant}
    return len(found) / len(relevant)


def mrr(ranked_ids: Sequence[str], relevance: Mapping[str, int]) -> float:
    """Reciprocal rank of the first relevant paper (``0.0`` when none).

    The whole ranking is considered, not just the top ``k``.
    """
    relevant = _relevant(relevance)
    if not relevant:
        return 0.0
    for position, item in enumerate(ranked_ids, start=1):
        if str(item) in relevant:
            return 1.0 / position
    return 0.0


def _dcg(grades: Iterable[int]) -> float:
    """Discounted cumulative gain with ``gain = 2 ** grade - 1`` (grade 0/1/2)."""
    total = 0.0
    for position, grade in enumerate(grades, start=1):
        if grade <= 0:
            continue
        total += (2.0**grade - 1.0) / math.log2(position + 1.0)
    return total


def ndcg_at_k(
    ranked_ids: Sequence[str], relevance: Mapping[str, int], k: int
) -> float:
    """Normalized DCG at ``k`` using ``2 ** grade - 1`` as the gain.

    ``ranked_ids`` is truncated to ``k`` for the actual DCG and the labels are
    sorted by grade descending (padded to at most ``k`` entries) for the ideal
    DCG. Returns ``0.0`` when there is nothing relevant (IDCG is zero) so a
    query without labels never contributes a division by zero.
    """
    _check_k(k)
    relevant = _relevant(relevance)
    if not relevant:
        return 0.0

    actual = _dcg(relevant.get(item, 0) for item in _top_k(ranked_ids, k))
    ideal_grades = sorted(relevant.values(), reverse=True)[:k]
    ideal = _dcg(ideal_grades)
    if ideal <= 0:
        return 0.0
    return actual / ideal


def aggregate(per_query: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Mean of every metric across ``per_query`` rows.

    ``per_query`` is an iterable of mappings that either hold a ``metrics``
    mapping (the shape used in the eval report) or the metric keys directly.
    Missing/non-numeric values are skipped; a metric with no observations is
    reported as ``{"mean": 0.0, "n": 0}``.
    """
    totals: dict[str, float] = {name: 0.0 for name in METRIC_NAMES}
    counts: dict[str, int] = {name: 0 for name in METRIC_NAMES}

    for row in per_query:
        metrics = row.get("metrics") if isinstance(row, Mapping) else None
        source = metrics if isinstance(metrics, Mapping) else row
        for name in METRIC_NAMES:
            value = source.get(name) if isinstance(source, Mapping) else None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            totals[name] += float(value)
            counts[name] += 1

    return {
        name: {
            "mean": (totals[name] / counts[name]) if counts[name] else 0.0,
            "n": counts[name],
        }
        for name in METRIC_NAMES
    }
