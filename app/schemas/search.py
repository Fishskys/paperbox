"""Pydantic schemas for ``POST /api/search`` (MVP-SPEC section 8).

Request shape::

    {"query": "...", "mode": "hybrid", "top_k": 10,
     "filters": {"year_from": 2020, "year_to": 2024, "authors": ["..."],
                 "venue": ["..."], "doi": "...", "arxiv_id": "...", "tag": ["..."]},
     "rerank": false}

Response shape::

    {"query": "...", "mode": "hybrid", "total": 12, "took_ms": 84.2,
     "rerank": {"enabled": true, "model": "Xenova/ms-marco-MiniLM-L-6-v2",
                "took_ms": 37},
     "results": [{"paper_id": "...", "title": "...", "authors": ["..."],
                  "year": 2021, "doi": "...", "score": 0.031,
                  "relevance": "high",
                  "retrieval_score": 0.016, "rerank_score": 0.87,
                  "evidence": [{"chunk_id": "...", "page": 3,
                                "section": "2 Method", "text": "..."}]}]}
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.search.hybrid import DEFAULT_MODE, MODES

#: ``top_k`` bounds from the spec.
MIN_TOP_K = 1
MAX_TOP_K = 50

SearchModeLiteral = Literal["keyword", "semantic", "hybrid"]


class SearchFilters(BaseModel):
    """Optional metadata filters; every field is optional."""

    model_config = ConfigDict(extra="ignore")

    year_from: int | None = None
    year_to: int | None = None
    authors: list[str] | None = None
    venue: list[str] | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    tag: list[str] | None = None

    @field_validator("authors", "venue", "tag", mode="before")
    @classmethod
    def _accept_scalar(cls, value):
        """Accept a bare string where a list is documented."""
        if isinstance(value, str):
            return [value]
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
    total: int
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
