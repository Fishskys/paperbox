"""Retrieval evaluation helpers (SPEC-P1 section E).

Pure, dependency-free metric implementations plus the aggregation used by
``scripts/eval.py`` when it scores a live paperbox instance against
``evals/queries.jsonl`` and ``evals/labels.jsonl``.
"""

from app.eval.metrics import (
    METRIC_NAMES,
    aggregate,
    hit_rate_at_k,
    mrr,
    ndcg_at_k,
    recall_at_k,
)

__all__ = [
    "METRIC_NAMES",
    "aggregate",
    "hit_rate_at_k",
    "mrr",
    "ndcg_at_k",
    "recall_at_k",
]
