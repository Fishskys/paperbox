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

    {"query": "...", "mode": "hybrid", "total": 12, "took_ms": 84.2,
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

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.search.hybrid import DEFAULT_MODE, MODES
from app.services.metadata_identifiers import SCHEMES

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

    def to_query_filters(self) -> dict:
        """Drop unset values so the query builder only sees real filters."""
        return self.model_dump(exclude_none=True, exclude_defaults=False)


class SearchRequest(BaseModel):
    """Body of ``POST /api/search``."""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, description="Free-text query, zh or en.")
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

    @field_validator("query")
    @classmethod
    def _check_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be blank")
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
    #: Normalized cross-encoder score; ``null`` when the reranker did not run.
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


class SearchResponse(BaseModel):
    """Response body of ``POST /api/search``."""

    model_config = ConfigDict(extra="ignore")

    #: The query the caller sent, always unmodified.
    query: str
    #: English search expression actually used for retrieval, when rewritten.
    rewritten_query: str | None = None
    mode: str
    #: 本次查询 + 过滤条件下命中的**论文**数（引擎 ``cardinality(paper_id)``
    #: 真值，见 ``app.search.hybrid.count_papers``）。
    total: int
    #: 喂给论文聚合的 chunk 候选池大小（2026-09-30 前，这个数字被当成 ``total``）。
    candidates: int
    took_ms: float
    rerank: SearchRerankInfo = Field(default_factory=SearchRerankInfo)
    rewrite: SearchRewriteInfo = Field(default_factory=SearchRewriteInfo)
    results: list[SearchResult] = Field(default_factory=list)


__all__ = [
    "MAX_TOP_K",
    "MIN_TOP_K",
    "SearchEvidence",
    "SearchFilters",
    "SearchRequest",
    "SearchResponse",
    "SearchRerankInfo",
    "SearchResult",
    "SearchRewriteInfo",
]
