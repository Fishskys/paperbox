"""Hybrid retrieval over the chunk index (MVP-SPEC section 8).

Three modes are supported:

``keyword``
    BM25 ``multi_match`` over ``title`` (boosted 2x) and ``text``.
``semantic``
    ``knn`` on ``embedding`` with the query vector produced by the embedding
    server; filters are applied inside the kNN clause ("filter first, then kNN").
``hybrid``
    both legs, each fetching ``top_k * 5`` candidates, fused with RRF (k=60).
    Each leg's contribution is scaled by ``RRF_KEYWORD_WEIGHT`` /
    ``RRF_SEMANTIC_WEIGHT`` (both default 1.0, i.e. classic equal-weight RRF;
    a weight of 0 disables that leg). This is the default mode of
    ``POST /api/search``.

``rerank=True`` turns retrieval into two stages for every mode: the first stage
over-fetches ``top_k * RERANK_CANDIDATES`` candidates, a cross-encoder scores
them (``app.services.rerank_service``) and the survivors are truncated to
``top_k * 2`` before paper-level aggregation narrows them to ``top_k``. The
rerank score of a hit is min-max normalized into ``0..1`` across the candidate
window; ``ChunkHit.retrieval_score`` keeps the first-stage score and
``ChunkHit.rerank_score`` the normalized cross-encoder score. When the reranker
is unavailable the original order and scores are returned unchanged
(``rerank_score`` stays ``None``) and no exception is raised.

Filters map onto a bool ``filter`` clause so they never influence scoring:
``year_from``/``year_to`` (range on the paper year), ``authors``/``venue``/``tag``
(terms), ``doi``/``arxiv_id`` (term), plus the metadata-snapshot filters
``venue_year`` (edition year), ``paper_type``, ``identifier`` (``scheme:value``
such as ``ieee_article_number:7065247``) and one list per ``papers_tags.kind``
(``ieee_terms`` / ``author_terms`` / ``dynamic_index_terms`` / ``source_tags``).
All of them read fields written at index time, so metadata changes only reach
filtering after a reindex.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from opensearchpy import OpenSearch
from opensearchpy.exceptions import NotFoundError, OpenSearchException

from app.core.config import settings
from app.core.logging import get_logger
from app.search.mappings import TAG_KIND_FIELDS
from app.search.opensearch import ALIAS, SearchIndexError, get_client
from app.search.ranking import DEFAULT_RRF_K, rrf_fuse
from app.services import rerank_service
from app.services.embedding_service import EmbeddingError, embed_text

logger = get_logger(__name__)

SearchMode = Literal["keyword", "semantic", "hybrid"]
MODES: tuple[str, ...] = ("keyword", "semantic", "hybrid")
DEFAULT_MODE: SearchMode = "hybrid"

#: ``title`` is twice as important as ``text`` in the BM25 leg.
TITLE_BOOST = 2.0
#: Both legs over-fetch so the fused list has something to reorder.
CANDIDATE_MULTIPLIER = 5
#: kNN over-fetch factor for the semantic-only mode (spec: ``k = top_k * 3``).
SEMANTIC_K_MULTIPLIER = 3
#: Fields returned for every hit.
SOURCE_FIELDS: tuple[str, ...] = (
    "chunk_id",
    "paper_id",
    "title",
    "authors",
    "year",
    "venue",
    "doi",
    "arxiv_id",
    "tags",
    "section",
    "section_title",
    "page_start",
    "page_end",
    "chunk_index",
    "text",
)


class SearchError(RuntimeError):
    """Raised when a search request cannot be served."""


@dataclass(slots=True)
class ChunkHit:
    """One retrieved chunk plus the scores it earned in each leg."""

    chunk_id: str
    paper_id: str
    score: float
    text: str = ""
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    tags: list[str] = field(default_factory=list)
    section: str | None = None
    section_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    chunk_index: int | None = None
    #: Metadata snapshot carried by the document (see ``app/search/mappings.py``):
    #: edition year of the venue, literature type and citation fields.
    venue_year: int | None = None
    paper_type: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publication_date: str | None = None
    keyword_score: float | None = None
    semantic_score: float | None = None
    rank: int | None = None
    #: First-stage (BM25 / kNN / RRF) score, kept when reranking replaces
    #: ``score`` with the normalized cross-encoder score.
    retrieval_score: float | None = None
    #: Normalized cross-encoder score; ``None`` when reranking did not happen.
    rerank_score: float | None = None

    @property
    def page(self) -> int | None:
        """First page of the chunk (evidence shows a single page number)."""
        return self.page_start

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "paper_id": self.paper_id,
            "score": self.score,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "tags": list(self.tags),
            "section": self.section,
            "section_title": self.section_title,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "chunk_index": self.chunk_index,
            "venue_year": self.venue_year,
            "paper_type": self.paper_type,
            "volume": self.volume,
            "issue": self.issue,
            "pages": self.pages,
            "publication_date": self.publication_date,
            "text": self.text,
            "keyword_score": self.keyword_score,
            "semantic_score": self.semantic_score,
            "rank": self.rank,
            "retrieval_score": self.retrieval_score,
            "rerank_score": self.rerank_score,
        }


# --------------------------------------------------------------------------- #
# filter construction (pure)
# --------------------------------------------------------------------------- #


def _as_list(value: Any) -> list[str]:
    """Normalize a scalar-or-list filter value into a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        items: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = value
    else:
        items = [value]
    result: list[str] = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _as_int(value: Any) -> int | None:
    """Best-effort integer coercion; ``None``/blank/garbage becomes ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001 - defensive, never fails on real input
        return None
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def build_filters(filters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Translate the API filter block into OpenSearch ``filter`` clauses.

    Returns an empty list when nothing usable was supplied, so the caller can
    omit the ``filter`` key entirely and keep the query shape canonical.
    """
    if not filters:
        return []

    clauses: list[dict[str, Any]] = []

    year_from = _as_int(filters.get("year_from"))
    year_to = _as_int(filters.get("year_to"))
    if year_from is not None or year_to is not None:
        bounds: dict[str, int] = {}
        if year_from is not None:
            bounds["gte"] = year_from
        if year_to is not None:
            bounds["lte"] = year_to
        clauses.append({"range": {"year": bounds}})

    authors = _as_list(filters.get("authors"))
    if authors:
        clauses.append({"terms": {"authors": authors}})

    venue = _as_list(filters.get("venue"))
    if venue:
        clauses.append({"terms": {"venue": venue}})

    for field in ("doi", "arxiv_id"):
        values = _as_list(filters.get(field))
        if values:
            clauses.append({"term": {field: values[0]}})

    tags = _as_list(filters.get("tag")) or _as_list(filters.get("tags"))
    if tags:
        clauses.append({"terms": {"tags": tags}})

    # --- metadata snapshot filters ---------------------------------------- #
    venue_years = [
        year
        for year in (_as_int(value) for value in _as_list(filters.get("venue_year")))
        if year is not None
    ]
    if venue_years:
        clauses.append({"terms": {"venue_year": venue_years}})

    paper_types = _as_list(filters.get("paper_type"))
    if paper_types:
        clauses.append({"terms": {"paper_type": paper_types}})

    #: ``scheme:value`` strings, e.g. ``ieee_article_number:7065247``.
    identifiers = _as_list(filters.get("identifier"))
    if identifiers:
        clauses.append({"terms": {"identifiers": identifiers}})

    # One clause per tag kind (``source_tag`` maps to the ``source_tags`` field).
    for field in TAG_KIND_FIELDS.values():
        names = _as_list(filters.get(field))
        if names:
            clauses.append({"terms": {field: names}})

    return clauses


def _with_filters(
    clauses: Sequence[dict[str, Any]], query: dict[str, Any]
) -> dict[str, Any]:
    """Wrap a query in a bool clause carrying the non-scoring filters."""
    if not clauses:
        return query
    return {
        "bool": {
            "must": [query],
            "filter": list(clauses),
        }
    }


# --------------------------------------------------------------------------- #
# query construction (pure)
# --------------------------------------------------------------------------- #


def build_keyword_query(
    query: str, filters: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """BM25 ``multi_match`` over ``title^2`` + ``text`` with filters."""
    multi_match = {
        "multi_match": {
            "query": query,
            "fields": [f"title^{TITLE_BOOST:g}", "text"],
            "type": "best_fields",
        }
    }
    return _with_filters(build_filters(filters), multi_match)


def build_semantic_query(
    embedding: Sequence[float],
    filters: Mapping[str, Any] | None = None,
    k: int = 10,
) -> dict[str, Any]:
    """kNN clause on ``embedding`` with the filters pushed inside the clause."""
    knn: dict[str, Any] = {"vector": [float(value) for value in embedding], "k": int(k)}
    clauses = build_filters(filters)
    if clauses:
        knn["filter"] = {"bool": {"filter": clauses}}
    return {"knn": {"embedding": knn}}


#: ``cardinality`` precision threshold for the paper count. Below this many
#: distinct papers the aggregation is exact; above it the engine switches to a
#: HyperLogLog estimate (OpenSearch default is 3000).
CARDINALITY_PRECISION = 3000
#: How many neighbours the kNN leg offers when counting papers.
COUNT_K = 1000

#: Facets ``facets=true`` reports: the metadata filters a caller can feed back in.
#: Every one of these is a ``keyword`` field on the chunk (see
#: :data:`app.search.mappings.KEYWORD_FIELDS`), except ``year`` -- an ``integer``,
#: which is bucketed by a histogram instead of a terms aggregation.
FACET_TERM_FIELDS: tuple[str, ...] = (
    "venue",
    "paper_type",
    "ieee_terms",
    "author_terms",
    "dynamic_index_terms",
    "source_tags",
)
FACET_YEAR_FIELD = "year"
FACET_NAMES: tuple[str, ...] = FACET_TERM_FIELDS + (FACET_YEAR_FIELD,)

#: Buckets per facet. ``terms`` is top-N by paper count, so this is a ceiling, not
#: a promise: on the 30-paper corpus ``author_terms`` (arXiv categories) already
#: holds 31 distinct values, and the tail beyond 50 would be dropped. Bigger
#: vocabularies need a composite aggregation with paging -- not today.
FACET_SIZE = 50


def _papers_sub_aggregation() -> dict[str, Any]:
    """Count **papers**, not chunks, inside one bucket.

    The index holds one document per chunk, so a plain ``doc_count`` would answer
    "how many chunks mention this venue" -- a number that grows with chunk length
    and cannot be compared with ``GET /api/papers``. Same correction as ``total``.
    """
    return {
        "papers": {
            "cardinality": {
                "field": "paper_id",
                "precision_threshold": CARDINALITY_PRECISION,
            }
        }
    }


def build_facet_body(
    filters: Mapping[str, Any] | None = None, *, size: int = FACET_SIZE
) -> dict[str, Any]:
    """``size: 0`` body with one paper-counting aggregation per facet.

    **Query independent on purpose.** The relevance legs (BM25, kNN) are *not* in
    this body: a facet answers "what does this filter leave in the library", which
    must not move because a caller changed ``top_k``, turned rerank on, or phrased
    the query differently. It is also the only reading that lines up with
    ``GET /api/papers``, whose filters read PostgreSQL. Callers who want the
    facets of the page they got can filter the response themselves.

    Pure: the clause shape is asserted in ``tests/test_search_facets.py`` without
    a cluster.
    """
    clauses = build_filters(filters)
    query: dict[str, Any] = {"bool": {"filter": list(clauses)}} if clauses else {"match_all": {}}
    aggs: dict[str, Any] = {
        field: {
            "terms": {"field": field, "size": int(size)},
            "aggs": _papers_sub_aggregation(),
        }
        for field in FACET_TERM_FIELDS
    }
    aggs[FACET_YEAR_FIELD] = {
        # One bucket per year, count of papers (``min_doc_count`` drops the empty
        # years a histogram would otherwise emit across the span).
        "histogram": {"field": FACET_YEAR_FIELD, "interval": 1, "min_doc_count": 1},
        "aggs": _papers_sub_aggregation(),
    }
    return {
        "size": 0,
        "track_total_hits": False,
        "query": query,
        "aggs": aggs,
    }


def facet_counts(
    filters: Mapping[str, Any] | None = None,
    *,
    client: OpenSearch | None = None,
    index: str = ALIAS,
    size: int = FACET_SIZE,
) -> dict[str, list[dict[str, Any]]]:
    """Read :func:`build_facet_body` back as ``{facet: [{key, count}, ...]}``.

    Buckets keep the engine's order: ``terms`` by descending paper count, the year
    histogram ascending. Zero-paper buckets are dropped (a bucket cannot be empty --
    every document carries a ``paper_id`` -- but a histogram may emit one).
    """
    response = _search(
        build_facet_body(filters, size=size), client=client, index=index
    )
    aggregations = response.get("aggregations") or {}
    return {name: _facet_buckets(aggregations.get(name)) for name in FACET_NAMES}


def _facet_buckets(aggregation: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize one aggregation's buckets into ``{key: str, count: int}``."""
    buckets: list[dict[str, Any]] = []
    for bucket in (aggregation or {}).get("buckets") or []:
        count = int(((bucket.get("papers") or {}).get("value")) or 0)
        if not count:
            continue
        key = bucket.get("key_as_string")
        if key is None:
            raw = bucket.get("key")
            key = str(int(raw)) if isinstance(raw, (int, float)) and float(raw).is_integer() else str(raw)
        buckets.append({"key": str(key), "count": count})
    return buckets


def build_count_body(
    query: str,
    mode: SearchMode | str = DEFAULT_MODE,
    filters: Mapping[str, Any] | None = None,
    *,
    k: int = COUNT_K,
) -> dict[str, Any]:
    """``size: 0`` + ``cardinality(paper_id)`` body for the paper count.

    Pure, so the clause shape can be asserted without a cluster. ``hybrid`` puts
    both legs in a ``should`` so the count is the union of what each leg would
    retrieve (a keyword-only count would hide papers that only the vector leg
    finds). The kNN clause is bounded by ``k``, which is the one part of this
    number that is a pool size rather than a global truth: ANN recall is
    approximate by construction.
    """
    normalized_mode = (mode or DEFAULT_MODE).strip().lower()
    if normalized_mode == "keyword":
        clause: dict[str, Any] = build_keyword_query(query, filters)
    elif normalized_mode == "semantic":
        clause = _semantic_clause(query, filters, k)
    elif normalized_mode == "hybrid":
        clause = {
            "bool": {
                "should": [
                    build_keyword_query(query, filters),
                    _semantic_clause(query, filters, k),
                ],
                "minimum_should_match": 1,
            }
        }
    else:
        raise ValueError(f"unsupported search mode: {mode!r}")
    return {
        "size": 0,
        "track_total_hits": False,
        "query": clause,
        "aggs": {
            "papers": {
                "cardinality": {
                    "field": "paper_id",
                    "precision_threshold": CARDINALITY_PRECISION,
                }
            }
        },
    }


def _semantic_clause(
    query: str, filters: Mapping[str, Any] | None, k: int
) -> dict[str, Any]:
    try:
        vector = embed_text(query)
    except EmbeddingError as exc:
        raise SearchError(f"embedding the query failed: {exc}") from exc
    return build_semantic_query(vector, filters, k=k)


def count_papers(
    query: str,
    mode: SearchMode | str = DEFAULT_MODE,
    filters: Mapping[str, Any] | None = None,
    *,
    client: OpenSearch | None = None,
    index: str = ALIAS,
    k: int = COUNT_K,
) -> int:
    """Distinct papers the query matches under ``filters`` -- the ``total`` truth.

    ``hits.total`` cannot answer this: it counts *chunks*, and once ``top_k``
    truncates the page the paper count is just whatever fitted. This asks the
    engine for ``cardinality(paper_id)`` on the same filters and the same legs
    the search uses. Empty query (or no matches) is ``0``.
    """
    query = (query or "").strip()
    if not query:
        return 0
    response = _search(
        build_count_body(query, mode, filters, k=k), client=client, index=index
    )
    value = ((response.get("aggregations") or {}).get("papers") or {}).get("value") or 0
    return int(value)


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #


def _search(
    body: dict[str, Any],
    *,
    client: OpenSearch | None = None,
    index: str = ALIAS,
) -> dict[str, Any]:
    client = client or get_client()
    try:
        return client.search(index=index, body=body)
    except NotFoundError as exc:
        raise SearchError(f"search index {index!r} does not exist") from exc
    except OpenSearchException as exc:  # pragma: no cover - live cluster only
        raise SearchError(f"search failed: {exc}") from exc


def rank_hits(response: Mapping[str, Any]) -> list[tuple[str, float, dict[str, Any]]]:
    """Flatten an OpenSearch response into ``(chunk_id, score, source)`` tuples.

    Documents without an id fall back to the chunk id in ``_source`` so the
    fusion step never sees an empty key.
    """
    hits = (response.get("hits") or {}).get("hits") or []
    ranked: list[tuple[str, float, dict[str, Any]]] = []
    for hit in hits:
        source = dict(hit.get("_source") or {})
        identifier = hit.get("_id") or source.get("chunk_id")
        if not identifier:
            continue
        ranked.append((str(identifier), float(hit.get("_score") or 0.0), source))
    return ranked


def _hit_from_source(
    chunk_id: str, source: Mapping[str, Any], *, score: float
) -> ChunkHit:
    return ChunkHit(
        chunk_id=chunk_id,
        paper_id=str(source.get("paper_id") or ""),
        score=score,
        text=source.get("text") or "",
        title=source.get("title"),
        authors=[str(name) for name in (source.get("authors") or [])],
        year=_as_int(source.get("year")),
        venue=source.get("venue"),
        doi=source.get("doi"),
        arxiv_id=source.get("arxiv_id"),
        tags=[str(tag) for tag in (source.get("tags") or [])],
        section=source.get("section"),
        section_title=source.get("section_title"),
        page_start=_as_int(source.get("page_start")),
        page_end=_as_int(source.get("page_end")),
        chunk_index=_as_int(source.get("chunk_index")),
        venue_year=_as_int(source.get("venue_year")),
        paper_type=source.get("paper_type"),
        volume=source.get("volume"),
        issue=source.get("issue"),
        pages=source.get("pages"),
        publication_date=source.get("publication_date"),
    )


def _keyword_hits(
    query: str,
    size: int,
    filters: Mapping[str, Any] | None,
    *,
    client: OpenSearch | None,
    index: str,
) -> list[ChunkHit]:
    body = {
        "size": int(size),
        "query": build_keyword_query(query, filters),
        "_source": list(SOURCE_FIELDS),
    }
    ranked = rank_hits(_search(body, client=client, index=index))
    hits: list[ChunkHit] = []
    for chunk_id, score, source in ranked:
        hit = _hit_from_source(chunk_id, source, score=score)
        hit.keyword_score = score
        hits.append(hit)
    return hits


def _semantic_hits(
    query: str,
    size: int,
    filters: Mapping[str, Any] | None,
    *,
    client: OpenSearch | None,
    index: str,
) -> list[ChunkHit]:
    try:
        vector = embed_text(query)
    except EmbeddingError as exc:
        raise SearchError(f"embedding the query failed: {exc}") from exc

    body = {
        "size": int(size),
        "query": build_semantic_query(vector, filters, k=int(size)),
        "_source": list(SOURCE_FIELDS),
    }
    ranked = rank_hits(_search(body, client=client, index=index))
    hits: list[ChunkHit] = []
    for chunk_id, score, source in ranked:
        hit = _hit_from_source(chunk_id, source, score=score)
        hit.semantic_score = score
        hits.append(hit)
    return hits


def search_chunks(
    query: str,
    mode: SearchMode | str = DEFAULT_MODE,
    top_k: int = 10,
    filters: Mapping[str, Any] | None = None,
    *,
    client: OpenSearch | None = None,
    index: str = ALIAS,
    rrf_k: int = DEFAULT_RRF_K,
    rerank: bool = False,
    telemetry: dict[str, Any] | None = None,
) -> list[ChunkHit]:
    """Retrieve chunks for ``query`` with the requested retrieval mode.

    Args:
        query: raw user query (Chinese and English both go through the same
            path; BM25 uses the standard analyzer).
        mode: ``keyword`` | ``semantic`` | ``hybrid`` (default).
        top_k: how many chunks to return.
        filters: filter block from ``POST /api/search``.
        client: optional OpenSearch client (tests/scripts).
        index: index or alias to read from (defaults to the write alias).
        rrf_k: RRF constant for the hybrid leg.
        rerank: when ``True`` the first stage over-fetches
            ``top_k * settings.rerank_candidates`` candidates, a cross-encoder
            rescores them and ``top_k * 2`` survivors are returned. If the
            reranker is unavailable the first-stage result is returned
            untouched (``rerank_score`` stays ``None``).

    Returns:
        At most ``top_k`` (``top_k * 2`` when reranking) :class:`ChunkHit`
        objects ordered by descending score.
    """
    normalized_mode = (mode or DEFAULT_MODE).strip().lower()
    if normalized_mode not in MODES:
        raise ValueError(f"unsupported search mode: {mode!r}")
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    query = (query or "").strip()
    if not query:
        return []

    fetch_k = _first_stage_k(top_k, rerank)

    if normalized_mode == "keyword":
        hits = _keyword_hits(
            query, fetch_k, filters, client=client, index=index
        )
        ordered = sorted(hits, key=lambda hit: hit.score, reverse=True)[:fetch_k]
    elif normalized_mode == "semantic":
        k = fetch_k * SEMANTIC_K_MULTIPLIER
        hits = _semantic_hits(query, k, filters, client=client, index=index)
        ordered = sorted(hits, key=lambda hit: hit.score, reverse=True)[:fetch_k]
    else:
        candidates = fetch_k * CANDIDATE_MULTIPLIER
        keyword_hits = _keyword_hits(
            query, candidates, filters, client=client, index=index
        )
        semantic_hits = _semantic_hits(
            query, candidates, filters, client=client, index=index
        )
        by_id: dict[str, ChunkHit] = {}
        for hit in keyword_hits + semantic_hits:
            by_id.setdefault(hit.chunk_id, hit)
        for hit in keyword_hits:
            by_id[hit.chunk_id].keyword_score = hit.keyword_score
        for hit in semantic_hits:
            by_id[hit.chunk_id].semantic_score = hit.semantic_score

        # Leg order is keyword then semantic; the weights follow that order
        # (RRF_KEYWORD_WEIGHT / RRF_SEMANTIC_WEIGHT, SPEC-P1 section H2).
        fused = rrf_fuse(
            [[hit.chunk_id for hit in keyword_hits], [hit.chunk_id for hit in semantic_hits]],
            k=rrf_k,
            weights=(settings.rrf_keyword_weight, settings.rrf_semantic_weight),
        )
        ordered = []
        for chunk_id, score in fused[:fetch_k]:
            hit = by_id.get(chunk_id)
            if hit is None:
                continue
            hit.score = score
            ordered.append(hit)

    rerank_took_ms: int | None = None
    if rerank:
        ordered, rerank_took_ms = _apply_rerank(query, ordered, top_k)
    elif len(ordered) > top_k:
        ordered = ordered[:top_k]

    if telemetry is not None:
        telemetry["rerank_took_ms"] = rerank_took_ms
        telemetry["reranked"] = rerank_took_ms is not None
        telemetry["candidates"] = len(ordered)

    for position, hit in enumerate(ordered):
        hit.rank = position
    logger.info(
        "chunk search finished",
        extra={
            "extra_fields": {
                "mode": normalized_mode,
                "top_k": top_k,
                "returned": len(ordered),
                "has_filters": bool(filters),
                "rerank": bool(rerank),
                "rerank_took_ms": rerank_took_ms,
            }
        },
    )
    return ordered


def _first_stage_k(top_k: int, rerank: bool) -> int:
    """Candidate window of the first stage (wider when reranking)."""
    if not rerank:
        return top_k
    factor = max(1, int(settings.rerank_candidates or 1))
    return top_k * factor


def _apply_rerank(
    query: str, ordered: list[ChunkHit], top_k: int
) -> tuple[list[ChunkHit], int | None]:
    """Rescore ``ordered`` with the cross-encoder and keep ``top_k * 2``.

    Returns the (possibly unchanged) hits plus the rerank duration in
    milliseconds, or ``None`` when the reranker was unavailable. Retriever
    scores are preserved on ``retrieval_score``; ``score`` becomes the min-max
    normalized cross-encoder score so the existing 0..1 relevance thresholds
    keep working, and ``rerank_score`` carries that same normalized value.
    """
    if not ordered:
        return ordered, None

    started = time.perf_counter()
    scores = rerank_service.rerank_texts(query, [hit.text for hit in ordered])
    took_ms = rerank_service.rerank_took_ms(started)
    if scores is None:
        # Degrade: keep first-stage order and scores untouched.
        return ordered[: top_k * 2], None

    scored: list[ChunkHit] = []
    for item in scores:
        if not 0 <= item.index < len(ordered):
            continue
        hit = ordered[item.index]
        hit.retrieval_score = float(hit.score)
        hit.rerank_score = item.score
        scored.append(hit)

    # The cross-encoder decides the new order (best score first; ties fall back
    # to the first-stage order so the result stays deterministic).
    order = {id(hit): position for position, hit in enumerate(ordered)}
    ranked = sorted(
        scored,
        key=lambda hit: (-float(hit.rerank_score or 0.0), order[id(hit)]),
    )

    # Hits the service did not score keep their first-stage standing, after the
    # reranked ones, so nothing silently disappears from the window.
    seen = {id(hit) for hit in ranked}
    ranked.extend(hit for hit in ordered if id(hit) not in seen)
    for hit in ranked:
        if hit.retrieval_score is None:
            hit.retrieval_score = float(hit.score)

    _normalize_rerank_scores(ranked)
    return ranked[: top_k * 2], took_ms


def _normalize_rerank_scores(hits: list[ChunkHit]) -> None:
    """Min-max normalize cross-encoder scores into ``0..1`` in place.

    Cross-encoder logits are unbounded and not comparable across queries, so
    ``score`` is rescaled over the reranked window: the best hit becomes 1.0 and
    the worst 0.0 (a single hit, or a flat window, becomes 1.0). Hits without a
    rerank score are left alone.
    """
    scored = [hit for hit in hits if hit.rerank_score is not None]
    if not scored:
        return
    values = [float(hit.rerank_score) for hit in scored]
    best = max(values)
    worst = min(values)
    span = best - worst
    for hit in scored:
        if span <= 0:
            hit.score = 1.0
        else:
            hit.score = round((float(hit.rerank_score) - worst) / span, 6)


__all__ = [
    "CANDIDATE_MULTIPLIER",
    "ChunkHit",
    "DEFAULT_MODE",
    "MODES",
    "SEMANTIC_K_MULTIPLIER",
    "SOURCE_FIELDS",
    "SearchError",
    "SearchMode",
    "TITLE_BOOST",
    "build_filters",
    "build_keyword_query",
    "build_semantic_query",
    "rank_hits",
    "search_chunks",
]
