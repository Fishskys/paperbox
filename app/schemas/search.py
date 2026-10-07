"""Pydantic schemas for ``POST /api/search`` (MVP-SPEC section 8).

Request shape::

    {"query": "...", "mode": "hybrid", "top_k": 10,
     "filters": {"year_from": 2020, "year_to": 2024, "authors": ["..."],
                 "venue": ["..."], "venue_year": [2021], "paper_type": ["conference"],
                 "doi": "...", "arxiv_id": "...", "tag": ["..."],
                 "identifier": ["ieee_article_number:7065247"],
                 "ieee_terms": ["low power sram"]},
     "rerank": false}

Response shape::

    {"query": "...", "mode": "hybrid", "total": 12, "candidates": 25, "took_ms": 84.2,
     "rerank": {"enabled": true, "model": "Xenova/ms-marco-MiniLM-L-6-v2",
                "took_ms": 37},
     "results": [{"paper_id": "...", "title": "...", "authors": ["..."],
                  "year": 2021, "venue": "ISSCC", "venue_year": 2021,
                  "paper_type": "conference", "volume": "12", "issue": "3",
                  "pages": "1-8", "publication_date": "2021-02-18",
                  "doi": "...", "score": 0.031, "relevance": "high",
                  "retrieval_score": 0.016, "rerank_score": 0.87,
                  "evidence": [{"chunk_id": "...", "page": 3,
                                "section": "2 Method", "text": "..."}]}]}

Every filter and every echoed field comes from the index-time metadata snapshot
(``app/search/mappings.py``), so a metadata change in PostgreSQL only shows up
here after the affected papers are reindexed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.logging import get_logger
from app.search.hybrid import DEFAULT_MODE, MODES
from app.search.native import BACKENDS, DEFAULT_BACKEND
from app.services.metadata_identifiers import SCHEMES

logger = get_logger(__name__)

#: ``top_k`` bounds from the spec.
MIN_TOP_K = 1
MAX_TOP_K = 50

SearchModeLiteral = Literal["keyword", "semantic", "hybrid"]

#: Filters whose value may be written as a bare string instead of a list.
_SCALAR_FRIENDLY = (
    "authors",
    "venue",
    "tag",
    "paper_type",
    "identifier",
    "ieee_terms",
    "author_terms",
    "dynamic_index_terms",
    "source_tags",
)


class SearchFilters(BaseModel):
    """Optional metadata filters; every field is optional."""

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _warn_unknown_keys(cls, value):
        """Log (never reject) filter keys this model does not know.

        ``extra="ignore"`` keeps the API compatible (a typo must not break an
        existing caller); the WARNING makes a silent zero-result visible in the
        logs instead (review 2026-10-05, P3 -- owner kept the ignore behavior).
        """
        if isinstance(value, dict):
            known = cls.model_fields.keys()
            unknown = sorted(str(key) for key in value if str(key) not in known)
            if unknown:
                logger.warning(
                    "unknown filter keys ignored",
                    extra={"extra_fields": {"unknown_filter_keys": unknown}},
                )
        return value

    #: Year of the paper itself.
    year_from: int | None = None
    year_to: int | None = None
    authors: list[str] | None = None
    venue: list[str] | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    #: Any tag, whatever its kind (the flat ``tags`` snapshot field).
    tag: list[str] | None = None
    # --- metadata snapshot (see app/search/mappings.py) -------------------- #
    #: Year of the venue *edition*, i.e. "the conference in 2015" as opposed to
    #: the paper year above; the two differ for early access and late indexing.
    venue_year: list[int] | None = None
    #: journal | conference | preprint | early_access | standard.
    paper_type: list[str] | None = None
    #: ``scheme:value`` pairs, e.g. ``ieee_article_number:7065247`` or
    #: ``doi:10.1109/jssc.2020.1`` (``paper_identifiers`` rows).
    identifier: list[str] | None = None
    #: Tag filters narrowed to one ``papers_tags.kind`` each.
    ieee_terms: list[str] | None = None
    author_terms: list[str] | None = None
    dynamic_index_terms: list[str] | None = None
    source_tags: list[str] | None = None

    @field_validator(*_SCALAR_FRIENDLY, mode="before")
    @classmethod
    def _accept_scalar(cls, value):
        """Accept a bare string where a list is documented."""
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("venue_year", mode="before")
    @classmethod
    def _accept_scalar_year(cls, value):
        """Accept a bare year where a list is documented."""
        if value is None or isinstance(value, (list, tuple, set, frozenset)):
            return value
        return [value]

    @field_validator("identifier", mode="after")
    @classmethod
    def _check_identifier_scheme(cls, value):
        """Require ``scheme:value`` with a known scheme.

        Without this a typo like ``ieee:123`` would quietly return zero results
        instead of a 422.
        """
        if not value:
            return value
        prefixes = tuple(f"{scheme}:" for scheme in SCHEMES)
        for entry in value:
            if not str(entry).strip().casefold().startswith(prefixes):
                raise ValueError(
                    "identifier entries must be scheme:value with a known scheme "
                    f"({', '.join(SCHEMES)})"
                )
        return value

    @field_validator("identifier", mode="after")
    @classmethod
    def _normalize_identifiers(cls, value):
        """Normalize every ``scheme:value`` the way the write side does.

        The index stores ``scheme:normalized_value`` from
        ``paper_identifiers`` (casefolded, version/hyphen-stripped...), so a
        filter value that only passed the scheme check would silently match
        nothing (review 2026-10-05, P1-8). Runs after the scheme check; a value
        that does not survive normalization is a 422, not a quiet zero.
        """
        if not value:
            return value
        from app.services.metadata_identifiers import normalize_identifier

        normalized: list[str] = []
        for entry in value:
            text = str(entry).strip()
            scheme, _, raw = text.partition(":")
            key = scheme.strip().lower()
            usable = normalize_identifier(key, raw)
            if usable is None:
                raise ValueError(
                    f"identifier {entry!r} has no usable value for scheme {key!r}"
                )
            normalized.append(f"{key}:{usable}")
        return normalized

    @field_validator("doi", mode="after")
    @classmethod
    def _normalize_doi(cls, value):
        """Same normalization the mirror column received at write time."""
        if not value:
            return value
        from app.services.paper_service import normalize_doi

        return normalize_doi(value)

    @field_validator("arxiv_id", mode="after")
    @classmethod
    def _normalize_arxiv_id(cls, value):
        """Casefolded, version suffix stripped -- matching the indexed value."""
        if not value:
            return value
        from app.services.paper_service import normalize_arxiv_id

        return normalize_arxiv_id(value)

    @field_validator("paper_type", mode="after")
    @classmethod
    def _lowercase_paper_types(cls, value):
        """The column stores the lowercase vocabulary; accept any casing."""
        if not value:
            return value
        return [str(item).strip().lower() for item in value if str(item).strip()]

    @field_validator("venue", mode="after")
    @classmethod
    def _normalize_venues(cls, value):
        """Match the normalized key the snapshot stores (P1-8)."""
        if not value:
            return value
        from app.services.venue_service import normalize_venue_name

        keys = [normalize_venue_name(str(item)) for item in value if str(item).strip()]
        return keys or None

    @field_validator(
        "tag", "ieee_terms", "author_terms", "dynamic_index_terms", "source_tags",
        mode="after",
    )
    @classmethod
    def _normalize_tag_names(cls, value):
        """Match the normalized keys the snapshot stores (P1-8)."""
        if not value:
            return value
        from app.services.metadata_tags import normalize_tag

        keys = [normalize_tag(str(item)) for item in value if str(item).strip()]
        return keys or None

    def to_query_filters(self) -> dict:
        """Drop unset values so the query builder only sees real filters."""
        return self.model_dump(exclude_none=True, exclude_defaults=False)


class SearchRequest(BaseModel):
    """Body of ``POST /api/search``."""

    model_config = ConfigDict(extra="ignore")

    #: Ceiling keeps a runaway query out of the search log, the OpenSearch
    #: analyzer and the embedding call in one step (review 2026-10-05, P1-16).
    #: Real queries — zh or en — sit well below it.
    query: str = Field(
        min_length=1,
        max_length=1000,
        description="Free-text query, zh or en.",
    )
    mode: SearchModeLiteral = DEFAULT_MODE
    top_k: int = Field(default=10, ge=MIN_TOP_K, le=MAX_TOP_K)
    filters: SearchFilters | None = None
    rerank: bool = Field(
        default=False,
        description=(
            "Two-stage retrieval: over-fetch candidates and rescore them "
            "with the cross-encoder before paper aggregation."
        ),
    )
    backend: str | None = Field(
        default=None,
        description=(
            "Hybrid fusion backend. 'native' (default, from SEARCH_BACKEND) sends "
            "one hybrid request that the search pipeline fuses and "
            "collapse(paper_id) turns into papers; 'python' (baseline/fallback) "
            "runs two queries and fuses them in the app. Only mode=hybrid is "
            "affected, and on the native path "
            "keyword_score/semantic_score come back null (one fused score is all "
            "the engine reports). The response echoes the backend that ran."
        ),
    )
    facets: bool = Field(
        default=False,
        description=(
            "Also report, for every metadata filter, which values exist under the "
            "current filters and how many **papers** each holds (one extra "
            "size:0 aggregation). Query independent: the relevance legs are not "
            "part of it, so the counts do not move with top_k/rerank and match "
            "GET /api/papers."
        ),
    )

    @field_validator("query")
    @classmethod
    def _check_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be blank")
        return normalized

    @field_validator("backend", mode="before")
    @classmethod
    def _check_backend(cls, value):
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized not in BACKENDS:
            raise ValueError(f"backend must be one of {', '.join(BACKENDS)}")
        return normalized

    @field_validator("mode", mode="before")
    @classmethod
    def _check_mode(cls, value):
        if value is None:
            return DEFAULT_MODE
        normalized = str(value).strip().lower()
        if normalized not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        return normalized


class SearchEvidence(BaseModel):
    """One matched chunk backing a paper result."""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str
    page: int | None = None
    section: str | None = None
    text: str = ""


class SearchResult(BaseModel):
    """One aggregated paper in the response."""

    model_config = ConfigDict(extra="ignore")

    paper_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    #: Metadata snapshot echoed back so callers can tell "the conference" from
    #: "the conference in a given year" without a second request.
    venue: str | None = None
    venue_year: int | None = None
    paper_type: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publication_date: str | None = None
    score: float
    relevance: str
    #: First-stage (BM25 / kNN / RRF) score; ``null`` when reranking was off.
    retrieval_score: float | None = None
    #: Raw cross-encoder logit (NOT normalized to 0..1 -- the normalization
    #: lives on the aggregated paper score); ``null`` when the reranker did not
    #: run.
    rerank_score: float | None = None
    evidence: list[SearchEvidence] = Field(default_factory=list)


class SearchRerankInfo(BaseModel):
    """What the reranker did for this request (SPEC-P1 section D2)."""

    model_config = ConfigDict(extra="ignore")

    #: Whether server-side reranking is configured at all.
    enabled: bool = False
    #: Model that reranked the candidates; ``None`` when it did not run.
    model: str | None = None
    #: Rerank duration in milliseconds; ``None`` when it did not run.
    took_ms: int | None = None


class SearchRewriteInfo(BaseModel):
    """What the query rewriter did for this request (SPEC-P1 section I1)."""

    model_config = ConfigDict(extra="ignore")

    #: Whether query rewriting is enabled on the server at all.
    enabled: bool = False
    #: Whether this particular query was actually rewritten.
    applied: bool = False
    #: Model that produced the rewrite; ``None`` when it did not run.
    model: str | None = None
    #: Rewrite duration in milliseconds; ``None`` when it did not run.
    took_ms: int | None = None


class FacetBucket(BaseModel):
    """One facet value and how many papers carry it."""

    model_config = ConfigDict(extra="ignore")

    #: The value to feed back as a filter (``year`` comes back as ``"2017"``).
    key: str
    #: Distinct papers, not chunks -- a plain bucket doc_count would count chunks.
    count: int


class SearchFacets(BaseModel):
    """``facets=true`` read-out: the same keys the filters accept."""

    model_config = ConfigDict(extra="ignore")

    venue: list[FacetBucket] = Field(default_factory=list)
    paper_type: list[FacetBucket] = Field(default_factory=list)
    year: list[FacetBucket] = Field(default_factory=list)
    ieee_terms: list[FacetBucket] = Field(default_factory=list)
    author_terms: list[FacetBucket] = Field(default_factory=list)
    dynamic_index_terms: list[FacetBucket] = Field(default_factory=list)
    source_tags: list[FacetBucket] = Field(default_factory=list)


class SearchResponse(BaseModel):
    """Response body of ``POST /api/search``."""

    model_config = ConfigDict(extra="ignore")

    #: The query the caller sent, always unmodified.
    query: str
    #: English search expression actually used for retrieval, when rewritten.
    rewritten_query: str | None = None
    mode: str
    #: Retrieval backend that produced this page (``python`` | ``native``,
    #: plan §7 M5). Always ``python`` for the single-leg modes.
    backend: str = DEFAULT_BACKEND
    #: 本次查询 + 过滤条件下命中的**论文**数（引擎 ``cardinality(paper_id)``
    #: 真值，见 ``app.search.hybrid.count_papers``）。
    total: int
    #: 喂给论文聚合的 chunk 候选池大小（2026-09-30 前，这个数字被当成 ``total``）。
    candidates: int
    took_ms: float
    rerank: SearchRerankInfo = Field(default_factory=SearchRerankInfo)
    rewrite: SearchRewriteInfo = Field(default_factory=SearchRewriteInfo)
    #: ``null`` when ``facets`` was not requested -- or when the aggregation
    #: failed (logged as a warning; a facet is never worth a 503).
    facets: SearchFacets | None = None
    results: list[SearchResult] = Field(default_factory=list)


__all__ = [
    "MAX_TOP_K",
    "MIN_TOP_K",
    "FacetBucket",
    "SearchEvidence",
    "SearchFacets",
    "SearchFilters",
    "SearchRequest",
    "SearchResponse",
    "SearchRerankInfo",
    "SearchResult",
    "SearchRewriteInfo",
]
