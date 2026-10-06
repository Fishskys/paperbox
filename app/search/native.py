"""Native hybrid retrieval: engine-side fusion + paper collapse (plan §7, M5).

The default hybrid path (``app.search.hybrid``) runs **two** queries, fuses the
ranked id lists in Python (``app.search.ranking.rrf_fuse``) and aggregates chunks
into papers after the fact. This module is the alternative: **one** request whose
``hybrid`` clause carries both legs, an OpenSearch **search pipeline** fuses them
with RRF on the engine side, and ``collapse(paper_id)`` returns *papers* directly
(with the paper's other matching chunks attached as ``inner_hits``).

Why it is worth a A/B at all -- what the engine does that the Python path cannot:

* one coordinates round trip instead of two, and no client-side fusion;
* ``collapse`` guarantees the page holds ``size`` **distinct papers**, while the
  Python path fetches ``top_k`` *chunks* and aggregates them into however many
  papers those chunks happen to cover (with ``rerank=false``: often far fewer
  than ``top_k``);
* the score is the engine's RRF value, numerically identical to
  ``rrf_fuse(k=60)`` for equal weights (verified: 2/61 = 0.032787 for a chunk
  first in both legs).

What it costs:

* ``keyword_score`` / ``semantic_score`` cannot be recovered -- one fused
  ``_score`` is all the engine reports, so both are ``None`` on this path
  (decision of 2026-09-30: drop the per-leg scores rather than pay a second
  request). Every other field is produced app-side and is unchanged;
* ``inner_hits`` arrive with the **raw sub-query score** (BM25 / kNN units), not
  the fused score -- see :func:`parse_native_response`;
* the fusion leaves the process, so ``rrf_fuse``/``tests/test_rrf.py`` stop
  covering the live path (they stay as the specification for what the engine is
  expected to do).

Facts that cost real time to establish (probe: ``tmp/probe_native_hybrid.py``):

* ``pagination_depth`` belongs **inside the ``hybrid`` clause** (body key, added
  in OpenSearch 2.19); as a top-level body key *or* as a URL parameter it is a
  400. It bounds how many documents **each sub-query** contributes to the fusion,
  so it is the analogue of the Python path's ``CANDIDATE_MULTIPLIER``;
* with ``size=N`` and a small ``pagination_depth`` the response holds **fewer than
  N** papers: the fused window collapses into however many distinct papers it
  contains (measured: depth 25 -> 7 papers, depth 50 -> 10 for ``size=10``);
* ``hits.total`` counts documents **before** collapse, so it is meaningless as a
  paper count -- ``total`` keeps coming from ``cardinality(paper_id)``
  (:func:`app.search.hybrid.count_papers`), which is also why that count uses the
  union (``bool.should``) of both legs in ``hybrid`` mode.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from opensearchpy import OpenSearch
from opensearchpy.exceptions import NotFoundError, OpenSearchException

from app.core.config import SEARCH_BACKENDS
from app.core.logging import get_logger
from app.search.hybrid import (
    CANDIDATE_MULTIPLIER,
    SOURCE_FIELDS,
    TITLE_BOOST,
    ChunkHit,
    SearchError,
    _hit_from_source,
    build_filters,
)
from app.search.opensearch import ALIAS, get_client
from app.services.embedding_service import EmbeddingError, embed_text

logger = get_logger(__name__)

#: Retrieval backends ``POST /api/search`` understands. ``native`` (this module)
#: is the deployed default since M5; ``python`` (two legs + client-side RRF) is
#: kept as the A/B baseline and the fallback.
#: The accepted names live in ``app.core.config`` so the environment variable,
#: the request field and this module cannot drift apart.
BACKENDS: tuple[str, ...] = tuple(sorted(SEARCH_BACKENDS))
DEFAULT_BACKEND = "native"

#: Search pipeline that fuses the two legs with RRF (``rank_constant`` 60, the
#: twin of ``ranking.DEFAULT_RRF_K``).
PIPELINE_RRF = "paperbox-rrf60"
#: Pipeline the SRW bypass scores against; kept here so both live in one place.
PIPELINE_NORM = "paperbox-norm-minmax"

#: Deployment-state objects (they live in the cluster, not in ``.env``): the
#: single source of truth for both ``scripts/ensure_search_pipelines.py`` and the
#: SRW bypass. Changing a body here means re-running the ensure script -- and a
#: pipeline that is referenced by an experiment can be replaced, never deleted.
PIPELINE_BODIES: dict[str, dict[str, Any]] = {
    PIPELINE_RRF: {
        "description": "paperbox: hybrid rank fusion, rank_constant 60 (app-side rrf_fuse k=60 twin)",
        "phase_results_processors": [
            {
                "score-ranker-processor": {
                    "combination": {"technique": "rrf", "rank_constant": 60}
                }
            }
        ],
    },
    PIPELINE_NORM: {
        "description": "paperbox: hybrid score normalization (arithmetic_mean + min_max)",
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {"weights": [0.5, 0.5]},
                    },
                }
            }
        ],
    },
}

#: ``inner_hits`` name for the collapsed paper's other matching chunks.
INNER_HITS_NAME = "evidence"
#: Chunks attached per collapsed paper (the winner plus this many siblings).
#: Must stay equal to ``app.services.search_service.MAX_EVIDENCE`` -- the paper
#: aggregation keeps that many evidence chunks per paper, so asking for more
#: would only enlarge the payload. ``tests/test_native_hybrid.py`` pins the two
#: together.
DEFAULT_INNER_HITS = 3

#: ``_source`` filter for the main hits and the inner hits alike: full ``_source``
#: would drag the 1024-dimension embedding vector along for every document.
_SOURCE_FILTER: dict[str, Any] = {"includes": list(SOURCE_FIELDS)}


# --------------------------------------------------------------------------- #
# body construction (pure)
# --------------------------------------------------------------------------- #


def build_hybrid_clause(
    query: str,
    vector: Sequence[float],
    filters: Mapping[str, Any] | None = None,
    *,
    pagination_depth: int,
) -> dict[str, Any]:
    """One ``hybrid`` clause: BM25 leg + kNN leg + the shared filter block.

    The filter is attached to the clause (``hybrid.filter``) rather than to each
    sub-query, so it cannot drift between the legs; filters never take part in
    scoring, exactly as in the Python path (where they sit in a bool ``filter``).
    """
    clause: dict[str, Any] = {
        "pagination_depth": int(pagination_depth),
        "queries": [
            {
                "multi_match": {
                    "query": query,
                    "fields": [f"title^{TITLE_BOOST:g}", "text"],
                    "type": "best_fields",
                }
            },
            {
                "knn": {
                    "embedding": {
                        "vector": [float(value) for value in vector],
                        "k": int(pagination_depth),
                    }
                }
            },
        ],
    }
    clauses = build_filters(filters)
    if clauses:
        clause["filter"] = {"bool": {"filter": clauses}}
    return {"hybrid": clause}


def build_native_body(
    query: str,
    vector: Sequence[float],
    filters: Mapping[str, Any] | None = None,
    *,
    size: int,
    pagination_depth: int | None = None,
    inner_hits: int = DEFAULT_INNER_HITS,
) -> dict[str, Any]:
    """Full search body for the native path (no pipeline, which is a request param).

    ``size`` is the number of **papers** requested (collapse happens after the
    fusion), while ``pagination_depth`` bounds each sub-query's contribution and
    therefore how many distinct papers there are to collapse at all. ``None``
    means ``size * CANDIDATE_MULTIPLIER`` -- the same window the Python path
    over-fetches (``top_k * 5`` per leg), which is what makes the two paths
    comparable rather than merely similar.
    """
    depth = int(pagination_depth) if pagination_depth else int(size) * CANDIDATE_MULTIPLIER
    body: dict[str, Any] = {
        "size": int(size),
        "query": build_hybrid_clause(query, vector, filters, pagination_depth=depth),
        "_source": _SOURCE_FILTER,
    }
    if inner_hits > 0:
        body["collapse"] = {
            "field": "paper_id",
            "inner_hits": {
                "name": INNER_HITS_NAME,
                "size": int(inner_hits),
                "_source": _SOURCE_FILTER,
            },
        }
    return body


# --------------------------------------------------------------------------- #
# response parsing (pure)
# --------------------------------------------------------------------------- #


def _inner_hits(hit: Mapping[str, Any], name: str = INNER_HITS_NAME) -> list[dict[str, Any]]:
    """The collapsed hit's sibling chunks, or ``[]`` when there are none."""
    block = (hit.get("inner_hits") or {}).get(name) or {}
    return list((block.get("hits") or {}).get("hits") or [])


def parse_native_response(response: Mapping[str, Any]) -> list[ChunkHit]:
    """Flatten a collapsed native response into ``ChunkHit`` objects.

    Returns, per collapsed paper, its winning chunk followed by the siblings the
    engine attached as ``inner_hits`` -- the same shape the Python path hands to
    :func:`app.services.search_service.aggregate_papers`, so the aggregation,
    reranking, score normalisation and serialisation stay untouched.

    Two deliberate decisions:

    * **siblings inherit the winner's fused score.** ``inner_hits`` are scored
      with the bare sub-query, so they come back in BM25/kNN units (measured:
      13.44 next to the winner's 0.0328). Feeding those numbers in as ``score``
      would let ``aggregate_papers`` pick a *paper* score in foreign units and
      would make the evidence scores incomparable with the ranked list; the
      sibling's own score is therefore dropped.
    * **``keyword_score`` / ``semantic_score`` stay ``None``**: one fused score is
      all the engine reports (plan §7 T-E2).
    """
    hits: list[ChunkHit] = []
    for hit in (response.get("hits") or {}).get("hits") or []:
        source = dict(hit.get("_source") or {})
        score = float(hit.get("_score") or 0.0)
        chunk_id = str(hit.get("_id") or source.get("chunk_id") or "")
        if not chunk_id:
            continue
        winner = _hit_from_source(chunk_id, source, score=score)
        hits.append(winner)
        for inner in _inner_hits(hit):
            inner_source = dict(inner.get("_source") or {})
            inner_id = str(inner.get("_id") or inner_source.get("chunk_id") or "")
            if not inner_id or inner_id == chunk_id:
                continue
            # ``primary=False``: an ``inner_hits`` sibling is evidence of an
            # already collapsed paper, so it neither worsens the reranking pool
            # nor counts against ``top_k`` when the hits are truncated.
            hits.append(
                _hit_from_source(inner_id, inner_source, score=score, primary=False)
            )
    return hits


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #


def native_search(
    query: str,
    size: int,
    filters: Mapping[str, Any] | None = None,
    *,
    client: OpenSearch | None = None,
    index: str = ALIAS,
    inner_hits: int = DEFAULT_INNER_HITS,
    pagination_depth: int | None = None,
    pipeline: str = PIPELINE_RRF,
    query_vector: list[float] | None = None,
) -> list[ChunkHit]:
    """Run one native hybrid request and return its collapsed chunks.

    Raises :class:`app.search.hybrid.SearchError` for every engine-side failure
    (index missing, HTTP error) and for a failed query embedding -- the same
    contract the Python path offers, so ``POST /api/search`` maps both to the
    status codes it already documents.
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        vector = query_vector if query_vector is not None else embed_text(query)
    except EmbeddingError as exc:
        raise SearchError(f"embedding the query failed: {exc}") from exc

    body = build_native_body(
        query,
        vector,
        filters,
        size=size,
        pagination_depth=pagination_depth,
        inner_hits=inner_hits,
    )
    client = client or get_client()
    try:
        response = client.search(
            index=index, body=body, params={"search_pipeline": pipeline}
        )
    except NotFoundError as exc:
        raise SearchError(f"search index {index!r} does not exist") from exc
    except OpenSearchException as exc:  # pragma: no cover - live cluster only
        raise SearchError(f"native hybrid search failed: {exc}") from exc
    if not isinstance(response, Mapping):  # pragma: no cover - defensive
        raise SearchError("native hybrid search returned no body")

    hits = parse_native_response(response)
    logger.info(
        "native hybrid search finished",
        extra={
            "extra_fields": {
                "papers": len({hit.paper_id for hit in hits}),
                "chunks": len(hits),
                "size": int(size),
                "pipeline": pipeline,
                "has_filters": bool(filters),
            }
        },
    )
    return hits


def ensure_pipelines(
    *, client: OpenSearch | None = None, dry_run: bool = False
) -> list[dict[str, Any]]:
    """Create/refresh the pipelines in :data:`PIPELINE_BODIES` (idempotent).

    Returns one ``{"name", "action", "changed"}`` record per pipeline, where
    ``action`` is ``created`` / ``updated`` / ``unchanged``. A pipeline is a
    deployment-state object (it lives in the cluster, not in ``.env``), so this
    is what ``scripts/ensure_search_pipelines.py`` and ``scripts/healthcheck.py``
    both call: writing is the setup path, comparing is the drift check.
    """
    client = client or get_client()
    report: list[dict[str, Any]] = []
    for name, body in PIPELINE_BODIES.items():
        current: dict[str, Any] | None = None
        try:
            existing = client.transport.perform_request("GET", f"/_search/pipeline/{name}")
        except NotFoundError:
            existing = None
        except OpenSearchException as exc:  # pragma: no cover - live cluster only
            raise SearchError(f"reading search pipeline {name!r} failed: {exc}") from exc
        if isinstance(existing, Mapping):
            current = dict(existing.get(name) or {})
        changed = current != body
        action = ("kept" if not changed else "created" if current is None else "updated")
        if not dry_run and changed:
            try:
                client.transport.perform_request(
                    "PUT", f"/_search/pipeline/{name}", body=body
                )
            except OpenSearchException as exc:  # pragma: no cover - live cluster only
                raise SearchError(f"writing search pipeline {name!r} failed: {exc}") from exc
        report.append({"name": name, "action": action, "changed": changed})
    return report


__all__ = [
    "BACKENDS",
    "DEFAULT_BACKEND",
    "DEFAULT_INNER_HITS",
    "INNER_HITS_NAME",
    "PIPELINE_BODIES",
    "PIPELINE_NORM",
    "PIPELINE_RRF",
    "build_hybrid_clause",
    "build_native_body",
    "ensure_pipelines",
    "native_search",
    "parse_native_response",
]
