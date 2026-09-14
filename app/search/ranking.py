"""Reciprocal rank fusion (MVP-SPEC section 8).

The ranker is deliberately a pure function: the hybrid search path hands it the
ranked id lists produced by the BM25 and kNN legs and it returns one fused list
of ``(chunk_id, score)`` tuples sorted by descending score. Nothing here talks
to OpenSearch, which keeps it trivially unit-testable.

Each leg can be weighted (SPEC-P1 section H2): its contribution becomes
``weight / (k + rank)``. The default of ``1.0`` per leg reproduces the classic
unweighted RRF exactly, so weighted fusion only kicks in when
``RRF_KEYWORD_WEIGHT`` / ``RRF_SEMANTIC_WEIGHT`` are retuned.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

#: Default RRF constant (from the spec, also the value used by OpenSearch).
DEFAULT_RRF_K = 60


def rrf_score(rank: int, k: int = DEFAULT_RRF_K) -> float:
    """Score of a document sitting at ``rank`` (0-based) in one ranked list."""
    if k <= 0:
        raise ValueError("k must be positive")
    if rank < 0:
        raise ValueError("rank must be non-negative")
    return 1.0 / (k + rank + 1)


def rrf_fuse(
    rank_lists: Iterable[Sequence[str]],
    k: int = DEFAULT_RRF_K,
    weights: Sequence[float] | None = None,
) -> list[tuple[str, float]]:
    """Fuse ranked id lists into ``[(id, score), ...]`` ordered by score desc.

    ``rank_lists`` holds one ordered list of ids per retrieval leg, best first.
    A document appearing in several legs accumulates the sum of its per-leg
    reciprocal-rank contributions. Ties are broken by the best (lowest) rank
    the document reached, then by id so the result is deterministic.

    ``weights`` (one per leg, same order as ``rank_lists``) scales each leg's
    contribution to ``weight / (k + rank + 1)``. ``None`` -- or a list of ones
    -- gives exactly the classic unweighted RRF, and a weight of ``0.0`` makes
    that leg contribute nothing (useful when the keyword leg is dead weight for
    a language, see SPEC-P1 section H2).

    Ids are compared as strings, so repeated ids inside a single list are
    counted once (the best rank wins).
    """
    if k <= 0:
        raise ValueError("k must be positive")

    lists = list(rank_lists)
    if weights is None:
        leg_weights = [1.0] * len(lists)
    else:
        leg_weights = [float(weight) for weight in weights]
        if len(leg_weights) != len(lists):
            raise ValueError("weights must have one entry per rank list")
        if any(weight < 0 for weight in leg_weights):
            raise ValueError("weights must be non-negative")

    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    for leg_weight, ranks in zip(leg_weights, lists):
        seen: set[str] = set()
        for position, raw_id in enumerate(ranks):
            document_id = str(raw_id)
            if document_id in seen:
                continue
            seen.add(document_id)
            scores[document_id] = scores.get(document_id, 0.0) + (
                leg_weight * rrf_score(position, k)
            )
            previous = best_rank.get(document_id)
            if previous is None or position < previous:
                best_rank[document_id] = position

    ordered = sorted(
        scores.items(),
        key=lambda item: (-item[1], best_rank.get(item[0], 0), item[0]),
    )
    return ordered


def ids_only(ranked: Iterable[tuple[str, float]]) -> list[str]:
    """Convenience helper: strip scores off a fused list."""
    return [document_id for document_id, _ in ranked]


__all__ = ["DEFAULT_RRF_K", "ids_only", "rrf_fuse", "rrf_score"]
