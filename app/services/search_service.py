"""Paper-level aggregation of chunk hits (MVP-SPEC section 8).

``POST /api/search`` returns papers, not chunks. This module turns the ranked
:class:`~app.search.hybrid.ChunkHit` list into one result per paper:

* chunks are grouped by ``paper_id``;
* the paper score is the highest score inside the group (RRF scores, so a
  paper hit by both retrieval legs ranks above a single-leg paper);
* ``relevance`` is derived from that score: ``high >= 0.9``, ``medium >= 0.6``,
  otherwise ``low``;
* at most :data:`MAX_EVIDENCE` (3) chunks per paper, each carrying
  ``chunk_id / page / section / text`` with the text truncated to
  :data:`MIN_EVIDENCE_CHARS = 200
EVIDENCE_TEXT_LIMIT` (500) characters.

Both helpers below are pure functions so they can be unit-tested without any
OpenSearch or PostgreSQL connection.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.search.hybrid import ChunkHit, SearchError

#: Maximum number of evidence chunks kept per paper.
MAX_EVIDENCE = 3
#: Evidence text is truncated to this many characters.
EVIDENCE_TEXT_LIMIT = 500
#: Chunks shorter than this are treated as heading/table noise when picking
#: evidence (they still count towards the paper score).
MIN_EVIDENCE_CHARS = 200
#: Sections whose chunks make poor evidence (bibliography, appendix boilerplate).
NOISE_SECTION = re.compile(
    r"^(references?|bibliography|acknowledg|appendix)", re.IGNORECASE
)
logger = get_logger(__name__)

#: Relevance thresholds (spec: high >= 0.9, medium >= 0.6, else low).
HIGH_THRESHOLD = 0.9
MEDIUM_THRESHOLD = 0.6

RELEVANCE_HIGH = "high"
RELEVANCE_MEDIUM = "medium"
RELEVANCE_LOW = "low"


@dataclass(slots=True)
class Evidence:
    """One matched chunk shown under a paper."""

    chunk_id: str
    page: int | None
    section: str | None
    text: str
    page_start: int | None = None
    page_end: int | None = None
    section_title: str | None = None
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "page": self.page,
            "section": self.section,
            "text": self.text,
        }


@dataclass(slots=True)
class PaperResult:
    """One aggregated paper in the ``POST /api/search`` response."""

    paper_id: str
    title: str
    score: float
    relevance: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    #: Metadata snapshot echoed from the best chunk of the group (see
    #: ``app/search/mappings.py``): edition year, literature type, citation fields.
    venue_year: int | None = None
    paper_type: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publication_date: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    matched_chunks: int = 0
    #: Best first-stage (pre-rerank) score inside the group; ``None`` when the
    #: paper was never reranked.
    retrieval_score: float | None = None
    #: Best normalized cross-encoder score inside the group; ``None`` when the
    #: reranker was not used (or was unavailable).
    rerank_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "venue_year": self.venue_year,
            "paper_type": self.paper_type,
            "volume": self.volume,
            "issue": self.issue,
            "pages": self.pages,
            "publication_date": self.publication_date,
            "score": self.score,
            "relevance": self.relevance,
            "retrieval_score": self.retrieval_score,
            "rerank_score": self.rerank_score,
            "evidence": [item.to_dict() for item in self.evidence],
        }


def classify_relevance(
    score: float,
    high: float = HIGH_THRESHOLD,
    medium: float = MEDIUM_THRESHOLD,
) -> str:
    """Map a paper score onto ``high`` / ``medium`` / ``low``."""
    if score >= high:
        return RELEVANCE_HIGH
    if score >= medium:
        return RELEVANCE_MEDIUM
    return RELEVANCE_LOW


def truncate_text(text: str | None, limit: int = EVIDENCE_TEXT_LIMIT) -> str:
    """Trim evidence text to ``limit`` characters, appending an ellipsis."""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def _best_score(group: Sequence[ChunkHit], attribute: str) -> float | None:
    """Highest ``attribute`` inside a group, or ``None`` when unset."""
    values = [
        getattr(hit, attribute, None)
        for hit in group
        if getattr(hit, attribute, None) is not None
    ]
    if not values:
        return None
    return float(max(values))


def _hit_score(hit: ChunkHit) -> float:
    try:
        return float(hit.score)
    except (TypeError, ValueError):
        return 0.0


def _select_evidence(
    ordered_group: Sequence[ChunkHit], max_evidence: int
) -> list[ChunkHit]:
    """Pick the evidence chunks of one paper, best first.

    Section-heading chunks (title blocks, running headers, table captions) and
    bibliography entries rank high on BM25 but are poor evidence, so substantial
    body chunks are preferred and the rest only fill the remaining slots.
    """
    limit = max(0, max_evidence)
    if limit == 0:
        return []

    def is_noise(hit: ChunkHit) -> bool:
        text = (hit.text or "").strip()
        if len(text) < MIN_EVIDENCE_CHARS:
            return True
        label = f"{hit.section or ''} {hit.section_title or ''}"
        return bool(NOISE_SECTION.search(label))

    preferred = [hit for hit in ordered_group if not is_noise(hit)]
    chosen = preferred[:limit]
    if len(chosen) < limit:
        remainder = [hit for hit in ordered_group if hit not in chosen]
        chosen.extend(remainder[: limit - len(chosen)])
    return chosen


def aggregate_papers(
    hits: Iterable[ChunkHit],
    *,
    top_k: int | None = None,
    max_evidence: int = MAX_EVIDENCE,
    text_limit: int = EVIDENCE_TEXT_LIMIT,
) -> list[PaperResult]:
    """Group chunk hits into ranked paper results.

    Args:
        hits: ranked chunk hits (best first) as returned by ``search_chunks``.
        top_k: keep at most this many papers (``None`` keeps every group).
        max_evidence: evidence chunks kept per paper (default 3).
        text_limit: character budget of each evidence text (default 500).

    Returns:
        Paper results ordered by descending paper score; ties fall back to the
        best rank inside the group and then to ``paper_id`` for determinism.
    """
    groups: dict[str, list[ChunkHit]] = {}
    order: dict[str, int] = {}
    for position, hit in enumerate(hits):
        paper_id = str(hit.paper_id or "")
        if not paper_id:
            continue
        groups.setdefault(paper_id, []).append(hit)
        order.setdefault(paper_id, position)

    results: list[PaperResult] = []
    for paper_id, group in groups.items():
        ordered_group = sorted(group, key=_hit_score, reverse=True)
        best = ordered_group[0]
        evidence_hits = _select_evidence(ordered_group, max_evidence)
        evidence = [
            Evidence(
                chunk_id=str(item.chunk_id),
                page=item.page,
                section=item.section_title or item.section,
                text=truncate_text(item.text, text_limit),
                page_start=item.page_start,
                page_end=item.page_end,
                section_title=item.section_title,
                score=_hit_score(item),
            )
            for item in evidence_hits
        ]
        score = _hit_score(best)
        results.append(
            PaperResult(
                paper_id=paper_id,
                title=best.title or "",
                score=score,
                relevance=classify_relevance(score),
                authors=list(best.authors or []),
                year=best.year,
                venue=best.venue,
                doi=best.doi,
                arxiv_id=best.arxiv_id,
                venue_year=best.venue_year,
                paper_type=best.paper_type,
                volume=best.volume,
                issue=best.issue,
                pages=best.pages,
                publication_date=best.publication_date,
                evidence=evidence,
                matched_chunks=len(group),
                retrieval_score=_best_score(group, "retrieval_score"),
                rerank_score=_best_score(group, "rerank_score"),
            )
        )

    results.sort(key=lambda item: (-item.score, order.get(item.paper_id, 0), item.paper_id))
    if top_k is not None:
        results = results[: max(0, top_k)]
    return results


def normalize_scores(results: list[PaperResult]) -> list[PaperResult]:
    """Rescale paper scores relative to the best hit of this query.

    Fusion scores (RRF) are tiny absolute numbers and not comparable across
    queries, so the API exposes a 0..1 score: the top paper of the result set is
    always 1.0 and the others are proportional to it. ``relevance`` is then
    derived from that normalized value.
    """
    if not results:
        return results
    best = max(result.score for result in results)
    if best <= 0:
        for result in results:
            result.score = 0.0
            result.relevance = classify_relevance(0.0)
        return results
    for result in results:
        if result.rerank_score is not None:
            # The reranker already produced a 0..1 score; keep it (and the
            # relevance derived from it) instead of re-normalizing RRF scores.
            result.relevance = classify_relevance(result.score)
            continue
        result.score = round(result.score / best, 4)
        result.relevance = classify_relevance(result.score)
    return results


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """What one search call produced.

    ``results`` are the aggregated papers (bounded by ``top_k``); ``total`` is the
    engine's count of **papers** matching this query under these filters
    (``cardinality(paper_id)``, :func:`app.search.hybrid.count_papers`) and
    ``candidates`` is the chunk candidate pool the aggregation actually ran on.

    Before 2026-09-30 the response reported ``len(results)`` as ``total`` -- the
    page size dressed up as a match count. The two numbers are now separate
    because they answer different questions ("how many papers can I page
    through" vs. "how many chunks fed this page").
    """

    results: list[PaperResult]
    total: int
    candidates: int


def search_papers(
    query: str,
    mode: str = "hybrid",
    top_k: int = 10,
    filters: Mapping[str, Any] | None = None,
    *,
    rerank: bool = False,
    telemetry: dict[str, Any] | None = None,
    client: Any = None,
    index: str | None = None,
    count_total: bool = True,
    **search_kwargs: Any,
) -> SearchOutcome:
    """Run the chunk search and aggregate it into paper-level results.

    ``rerank=True`` switches the chunk retrieval to two stages: the first stage
    over-fetches ``top_k * RERANK_CANDIDATES`` chunks and a cross-encoder
    rescores them before aggregation (see :func:`app.search.hybrid.search_chunks`).

    ``telemetry`` (optional dict, filled in place) reports what the retriever and
    the reranker actually did -- ``rerank_took_ms``, ``reranked``, ``candidates``
    -- so the API can expose the ``rerank`` block without a second call.

    Returns a :class:`SearchOutcome`: ``total`` is the number of *papers* the
    engine matches under the same query and filters (a second, ``size: 0``
    aggregation -- see :func:`app.search.hybrid.count_papers`), ``candidates`` is
    how many chunks fed the aggregation. ``count_total=False`` skips the extra
    engine round trip and reports the candidate pool's paper count as ``total``
    (used by callers that only want the page).
    """
    from app.search.hybrid import ALIAS, count_papers, search_chunks

    hits = search_chunks(
        query,
        mode,
        top_k,
        filters,
        client=client,
        index=index or ALIAS,
        rerank=rerank,
        telemetry=telemetry,
        **search_kwargs,
    )
    results = normalize_scores(aggregate_papers(hits, top_k=top_k))
    if not count_total:
        return SearchOutcome(results, len({hit.paper_id for hit in hits}), len(hits))
    try:
        total = count_papers(query, mode, filters, client=client, index=index or ALIAS)
    except SearchError as exc:
        # Counting is an extra courtesy query: a failure there must not turn a
        # working search into an error. Fall back to the pool and say so.
        logger.warning("paper count failed, reporting the candidate pool: %s", exc)
        total = len({hit.paper_id for hit in hits})
    return SearchOutcome(results, int(total), len(hits))


def results_to_payload(results: Sequence[PaperResult]) -> list[dict[str, Any]]:
    """Serialize aggregated results into JSON-ready dicts."""
    return [item.to_dict() for item in results]


__all__ = [
    "EVIDENCE_TEXT_LIMIT",
    "HIGH_THRESHOLD",
    "MAX_EVIDENCE",
    "MEDIUM_THRESHOLD",
    "RELEVANCE_HIGH",
    "RELEVANCE_LOW",
    "RELEVANCE_MEDIUM",
    "Evidence",
    "PaperResult",
    "SearchError",
    "SearchOutcome",
    "aggregate_papers",
    "classify_relevance",
    "results_to_payload",
    "normalize_scores",
    "search_papers",
    "truncate_text",
]
