"""``citations[]``: the reason an agent can say "page 7, section III-B".

Citations are a first-class part of the MCP contract (section 3.2), so building
them is not left to each tool: every reading tool funnels its chunks through here.
A citation carries what a verifiable reference needs -- paper, page, section, the
chunk id to re-open the context, and a short verbatim quote -- and nothing that
could be mistaken for a ranking signal (``quote`` is display only).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.mcp.models import ChunkView, Citation
from app.schemas.search import SearchResult

#: How much of a chunk is quoted in a citation (contract: <= 200 chars).
QUOTE_CHARS = 200


def quote_of(text: str | None) -> str | None:
    """First :data:`QUOTE_CHARS` characters, whitespace-normalised, or ``None``."""
    if not text:
        return None
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    return collapsed[:QUOTE_CHARS]


def from_search_results(results: Sequence[SearchResult]) -> list[Citation]:
    """Expand every paper's evidence into citations, in result order.

    Evidence is already the best chunks of each paper (the search service picks
    them), so the citation list doubles as "where in the paper to look".
    """
    citations: list[Citation] = []
    for item in results:
        for evidence in item.evidence:
            citations.append(
                Citation(
                    paper_id=item.paper_id,
                    title=item.title,
                    page=evidence.page,
                    section=evidence.section,
                    chunk_id=evidence.chunk_id,
                    quote=quote_of(evidence.text),
                )
            )
    return citations


def from_chunks(
    paper_id: str, title: str, chunks: Sequence[ChunkView]
) -> list[Citation]:
    """Cite a paper's chunks (``paper_get_chunks`` / ``paper_get_context``)."""
    return [
        Citation(
            paper_id=paper_id,
            title=title,
            page=chunk.page,
            section=chunk.section,
            chunk_id=chunk.chunk_id,
            quote=quote_of(chunk.text),
        )
        for chunk in chunks
    ]


__all__ = ["QUOTE_CHARS", "from_chunks", "from_search_results", "quote_of"]
