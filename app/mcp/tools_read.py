"""Read-only MCP tools (contract: ``docs/architecture/11-mcp-agent-interface.md`` 5.1).

Every tool follows the same shape:

    typed arguments -> service call -> Envelope(data, meta, warnings, citations)

and nothing here decides retrieval, aggregation or metadata semantics: those live
in ``app/services`` (``search_pipeline``, ``chunk_service``, ``paper_service``,
``degradation_service``, ``provenance_service``) so REST and MCP cannot drift.

Two things are enforced here because they are agent-facing policy rather than
business rules:

* the **character budget** -- one call never dumps a whole paper; the cut is
  reported in ``truncated`` plus a ``next_offset`` to continue from;
* **explicit errors** -- an argument above the ceiling is ``INVALID_ARGUMENT``
  with the allowed maximum in the message, never a silent clamp.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import ValidationError

from app.core.config import settings
from app.db.session import SessionLocal
from app.mcp import citations, errors
from app.mcp.audit import log_call
from app.mcp.models import (
    ChunkPageData,
    ChunkView,
    ContextData,
    Envelope,
    FileData,
    PaperDetailData,
    ProvenanceClaimView,
    ToolMeta,
)
from app.schemas.paper import PaperChunkOut, PaperDegradationOut, PaperOut
from app.schemas.search import (
    SearchFilters,
    SearchModeLiteral,
    SearchRequest,
    SearchResponse,
)
from app.services import (
    chunk_service,
    degradation_service,
    download_signing,
    paper_service,
    provenance_service,
    search_pipeline,
    search_service,
)

#: Stages after which a job will not move on its own.
TERMINAL_STAGES = frozenset({"COMPLETED", "FAILED"})

#: Poll interval and hard ceiling for ``wait_seconds`` on the status tool.
POLL_INTERVAL_SECONDS = 1.0
MAX_STATUS_WAIT_SECONDS = 600


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
class _Session:
    """A short-lived session for one tool call (MCP is stateless by contract)."""

    def __enter__(self):
        self._session = SessionLocal()
        return self._session

    def __exit__(self, *exc_info) -> None:
        self._session.close()


def _budget(max_chars: int | None) -> int:
    """Validate the caller's character budget (never clamp: say no, say why)."""
    value = settings.mcp_max_chars if max_chars is None else int(max_chars)
    ceiling = settings.mcp_max_chars_ceiling
    if value <= 0:
        raise errors.invalid_argument("max_chars must be > 0")
    if value > ceiling:
        raise errors.invalid_argument(
            f"max_chars {value} exceeds the server ceiling of {ceiling}",
            hint=f"ask for at most {ceiling} characters and page with offset/next_offset",
        )
    return value


def _view(
    row: PaperChunkOut, text: str, *, truncated: bool = False, primary: bool = False
) -> ChunkView:
    return ChunkView(
        chunk_id=row.chunk_id,
        chunk_index=row.chunk_index,
        page=row.page_start,
        page_end=row.page_end,
        section=row.section,
        subsection=row.subsection,
        text=text,
        chars=len(text),
        token_count=row.token_count,
        primary=primary,
        truncated=truncated,
    )


def _fit_chunks(
    rows: Sequence[PaperChunkOut], budget: int, *, start_offset: int = 0
) -> tuple[list[ChunkView], bool, int | None]:
    """Fill ``budget`` characters in reading order.

    Returns ``(chunks, truncated, next_offset)``. The last chunk that does not fit
    is cut (``truncated=True``) rather than dropped, and ``next_offset`` names the
    first chunk the caller has not seen yet -- ``None`` when the page is complete.
    """
    used = 0
    views: list[ChunkView] = []
    for position, row in enumerate(rows):
        text = row.text or ""
        remaining = budget - used
        if remaining <= 0:
            return views, True, start_offset + position
        if len(text) <= remaining:
            views.append(_view(row, text))
            used += len(text)
            continue
        views.append(_view(row, text[:remaining], truncated=True))
        return views, True, start_offset + position + 1
    return views, False, None


def _meta(tool: str, took_ms: int) -> ToolMeta:
    return ToolMeta(tool=tool, agent="default", toolset=settings.mcp_toolset, took_ms=took_ms)


def _base_url(ctx: Context | None) -> str:
    """The host the agent reached us on (so the link works from where it was asked).

    Read from the request rather than configuration: the agent already proved this
    host is allowed by getting this far, and a hard-coded public URL breaks on a LAN
    where the same server answers on several addresses.
    """
    request = None
    try:
        request = ctx.request_context.request if ctx is not None else None
    except Exception:  # pragma: no cover - defensive: no HTTP context in stdio mode
        request = None
    if request is not None:
        base = str(getattr(request, "base_url", "") or "")
        if base:
            return base
    if settings.mcp_public_base_url:
        return settings.mcp_public_base_url
    raise errors.ToolFailure(
        code=errors.INTERNAL,
        message="cannot determine the download host",
        hint="set MCP_PUBLIC_BASE_URL (e.g. http://10.0.0.5:8077) on the server",
    )


def _load_paper(session, paper_id: str):
    """Load a live paper or raise ``NOT_FOUND`` (soft-deleted counts as gone)."""
    paper = paper_service.get_paper(session, paper_id)
    if paper is None or paper.deleted_at is not None:
        raise errors.not_found("paper", paper_id)
    return paper


def _chunk_out(row) -> PaperChunkOut:
    """ORM chunk -> published chunk shape (same mapping ``chunk_service`` uses)."""
    return PaperChunkOut(
        chunk_id=row.id,
        chunk_index=row.chunk_index,
        page_start=row.page_start,
        page_end=row.page_end,
        section=row.section,
        subsection=row.subsection,
        text=row.text,
        token_count=row.token_count,
        char_count=row.char_count,
    )


def _first_message(exc: ValidationError) -> str:
    """One readable line out of a pydantic error (the agent sees this verbatim)."""
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = str(first.get("msg", "invalid arguments"))
    return f"{location}: {message}" if location else message


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
def register(server: MCPServer) -> None:
    """Attach every read tool to ``server`` (called once, at import/build time)."""

    # ------------------------------------------------------------------ search
    @server.tool(
        name="paper_search",
        title="Search papers",
        description=(
            "Search the paper corpus in natural language (Chinese or English) and get "
            "papers back, each with evidence chunks that carry page and section. Start "
            "here for any question about the literature: the citations[] it returns "
            "are what you quote ('this is in section III-B, page 7'). Use rerank=false "
            "only for a quick scan -- the cross-encoder is what makes the top results "
            "good and it costs a few seconds. Set facets=true when you need to know "
            "which venues/years/tags exist before filtering. "
            'Example: paper_search(query="low power SRAM leakage", top_k=5)'
        ),
        meta={"toolset": settings.mcp_toolset},
    )
    async def paper_search(
        query: str,
        mode: SearchModeLiteral = "hybrid",
        top_k: int = 10,
        filters: SearchFilters | None = None,
        rerank: bool = True,
        facets: bool = False,
        backend: str | None = None,
    ) -> Envelope[SearchResponse]:
        started = time.perf_counter()
        try:
            request = SearchRequest(
                query=query,
                mode=mode,
                top_k=top_k,
                filters=filters,
                rerank=rerank,
                facets=facets,
                backend=backend,
            )
        except ValidationError as exc:
            raise errors.invalid_argument(_first_message(exc)) from exc

        try:
            response = await search_pipeline.run_search(request)
        except search_service.SearchError as exc:
            raise errors.ToolFailure(
                code=errors.SERVICE_UNAVAILABLE,
                message=f"search backend unavailable: {exc}",
                retryable=True,
                retry_after=2,
                hint="OpenSearch or the embedding server is down; retry shortly",
            ) from exc
        except ValueError as exc:
            raise errors.invalid_argument(str(exc)) from exc

        took_ms = int((time.perf_counter() - started) * 1000)
        warnings: list[str] = []
        if request.rerank and response.rerank.model is None:
            warnings.append("rerank was requested but did not run (see the server log)")
        if response.total > len(response.results):
            warnings.append(
                f"showing {len(response.results)} of {response.total} matching papers"
            )
        if response.rewrite.applied:
            warnings.append(f"query rewritten for retrieval: {response.rewritten_query}")

        found = citations.from_search_results(response.results)
        log_call(
            tool="paper_search",
            agent="default",
            arguments={
                "query": query,
                "mode": request.mode,
                "top_k": request.top_k,
                "rerank": request.rerank,
                "facets": request.facets,
                "filters": (
                    request.filters.model_dump(exclude_none=True) if request.filters else None
                ),
            },
            outcome="ok",
            affected={"papers": len(response.results), "citations": len(found)},
            took_ms=took_ms,
        )
        return Envelope[SearchResponse](
            data=response,
            meta=_meta("paper_search", took_ms),
            warnings=warnings,
            citations=found,
        )

    # --------------------------------------------------------------- paper get
    @server.tool(
        name="paper_get",
        title="Read one paper's metadata",
        description=(
            "Metadata of one paper: title, authors, venue, identifiers, plus where each "
            "field came from (provenance) and any parsing degradation. Use it after "
            "paper_search when you need bibliographic detail or want to know how much "
            "to trust the parse. It does not return the text -- read that with "
            "paper_get_chunks or paper_get_context. "
            'Example: paper_get(paper_id="<paper_id from paper_search>")'
        ),
        meta={"toolset": settings.mcp_toolset},
    )
    async def paper_get(paper_id: str) -> Envelope[PaperDetailData]:
        started = time.perf_counter()
        with _Session() as session:
            paper = _load_paper(session, paper_id)
            payload = paper_service.serialize_paper(paper, source_url=paper.url)
            degradations: list[PaperDegradationOut] = [
                degradation_service.to_out(row)
                for row in degradation_service.list_for_paper(session, paper_id)
            ]
            summary = provenance_service.provenance_summary(session, paper_id)
            claims = {
                field: [
                    ProvenanceClaimView(
                        provenance_id=item["provenance_id"],
                        value=item.get("value"),
                        source_id=item.get("source_id"),
                        confidence=item.get("confidence"),
                        decided_by=item.get("decided_by"),
                        decided_at=item.get("decided_at"),
                    )
                    for item in items
                    if item.get("is_current")
                ]
                for field, items in summary.items()
            }
            data = PaperDetailData(
                paper=PaperOut.model_validate(payload),
                provenance={field: rows for field, rows in claims.items() if rows},
                degradations=degradations,
                chunk_count=chunk_service.count_chunks(session, paper_id),
            )

        took_ms = int((time.perf_counter() - started) * 1000)
        warnings = [
            f"degradation: {item.stage}/{item.code} (parse quality reduced)"
            for item in data.degradations
        ]
        if data.chunk_count == 0:
            warnings.append("this paper has no chunks: never parsed, or parsing failed")
        log_call(
            tool="paper_get",
            agent="default",
            arguments={"paper_id": paper_id},
            outcome="ok",
            affected={"chunks": data.chunk_count, "degradations": len(data.degradations)},
            took_ms=took_ms,
        )
        return Envelope[PaperDetailData](
            data=data, meta=_meta("paper_get", took_ms), warnings=warnings, citations=[]
        )

    # ------------------------------------------------------------ paper chunks
    @server.tool(
        name="paper_get_chunks",
        title="Read a paper's text in chunks",
        description=(
            "Read one paper's text chunk by chunk in reading order, each chunk "
            "carrying page and section. Use it to summarise a paper or to find a "
            "specific part when you already know the paper_id; use paper_get_context "
            "instead when you have a chunk_id from a search hit. Long papers page: "
            "when the answer says truncated/next_offset, call again with that offset. "
            'Example: paper_get_chunks(paper_id="...", offset=0, limit=10)'
        ),
        meta={"toolset": settings.mcp_toolset},
    )
    async def paper_get_chunks(
        paper_id: str,
        offset: int = 0,
        limit: int = chunk_service.DEFAULT_LIMIT,
        max_chars: int | None = None,
    ) -> Envelope[ChunkPageData]:
        started = time.perf_counter()
        budget = _budget(max_chars)
        if limit < 1 or limit > chunk_service.MAX_LIMIT:
            raise errors.invalid_argument(
                f"limit must be between 1 and {chunk_service.MAX_LIMIT}",
                hint="page through long papers with offset",
            )
        if offset < 0:
            raise errors.invalid_argument("offset must be >= 0")

        with _Session() as session:
            paper = _load_paper(session, paper_id)
            title = paper.title or ""
            page = chunk_service.list_chunks(session, paper_id, limit=limit, offset=offset)
            views, truncated, next_offset = _fit_chunks(
                page.chunks, budget, start_offset=offset
            )
            if next_offset is None and offset + len(views) < page.total:
                # The page fit the budget but the paper continues: still hand back
                # the cursor, so an agent never has to invent offset arithmetic.
                next_offset = offset + len(views)

        took_ms = int((time.perf_counter() - started) * 1000)
        warnings: list[str] = []
        if truncated:
            warnings.append(
                f"truncated at {budget} characters; continue with offset={next_offset}"
            )
        elif next_offset is not None:
            warnings.append(f"more chunks available: call again with offset={next_offset}")
        if page.total == 0:
            warnings.append("this paper has no chunks")
        data = ChunkPageData(
            paper_id=paper_id,
            title=title,
            total=page.total,
            returned=len(views),
            offset=offset,
            limit=limit,
            truncated=truncated,
            next_offset=next_offset,
            chunks=views,
        )
        found = citations.from_chunks(paper_id, title, views)
        log_call(
            tool="paper_get_chunks",
            agent="default",
            arguments={
                "paper_id": paper_id,
                "offset": offset,
                "limit": limit,
                "max_chars": budget,
            },
            outcome="ok",
            affected={"returned": len(views), "total": page.total},
            took_ms=took_ms,
        )
        return Envelope[ChunkPageData](
            data=data,
            meta=_meta("paper_get_chunks", took_ms),
            warnings=warnings,
            citations=found,
        )

    # ----------------------------------------------------------- paper context
    @server.tool(
        name="paper_get_context",
        title="Read around a search hit",
        description=(
            "Read the chunks around one chunk_id (usually a citation from "
            "paper_search) in reading order, so you can see the full argument the hit "
            "was cut out of. Use it when a citation looks relevant but is too short to "
            "quote; use paper_get_chunks to walk a whole paper instead. "
            'Example: paper_get_context(chunk_id="<chunk_id from citations[]>", before=2, after=2)'
        ),
        meta={"toolset": settings.mcp_toolset},
    )
    async def paper_get_context(
        chunk_id: str, before: int = 1, after: int = 1, max_chars: int | None = None
    ) -> Envelope[ContextData]:
        started = time.perf_counter()
        budget = _budget(max_chars)
        if before < 0 or after < 0:
            raise errors.invalid_argument("before/after must be >= 0")
        if before > chunk_service.MAX_NEIGHBOURS or after > chunk_service.MAX_NEIGHBOURS:
            raise errors.invalid_argument(
                f"before/after must each be <= {chunk_service.MAX_NEIGHBOURS}",
                hint="use paper_get_chunks to read a whole paper section by section",
            )

        with _Session() as session:
            window = chunk_service.chunk_context(session, chunk_id, before=before, after=after)
            if window is None:
                raise errors.ToolFailure(
                    code=errors.NOT_FOUND,
                    message=f"chunk {chunk_id!r} does not exist",
                    hint="pass a chunk_id from paper_search citations[] or paper_get_chunks",
                )
            title = ""
            paper = paper_service.get_paper(session, window.paper_id)
            if paper is not None and paper.deleted_at is None:
                title = paper.title or ""
            rows = [_chunk_out(row) for row in window.chunks]
            views, truncated, _ = _fit_chunks(rows, budget)
            positions = {view.chunk_id: index for index, view in enumerate(views)}
            if window.target.id in positions:
                views[positions[window.target.id]].primary = True
            elif rows:
                # The budget ran out before the chunk the caller asked about: answer
                # with that one (cut) instead of only its neighbours, and say so.
                target = next(row for row in rows if row.chunk_id == chunk_id)
                views = [
                    _view(target, (target.text or "")[:budget], truncated=True, primary=True)
                ]
                truncated = True

        took_ms = int((time.perf_counter() - started) * 1000)
        warnings: list[str] = []
        if window.missing_before:
            warnings.append(
                f"the paper starts here: {window.missing_before} earlier chunk(s) do not exist"
            )
        if window.missing_after:
            warnings.append(
                f"the paper ends here: {window.missing_after} later chunk(s) do not exist"
            )
        if truncated:
            warnings.append(f"truncated at {budget} characters")
        data = ContextData(
            paper_id=window.paper_id,
            title=title,
            target_chunk_id=chunk_id,
            returned=len(views),
            truncated=truncated,
            missing_before=window.missing_before,
            missing_after=window.missing_after,
            chunks=views,
        )
        found = citations.from_chunks(window.paper_id, title, views)
        log_call(
            tool="paper_get_context",
            agent="default",
            arguments={
                "chunk_id": chunk_id,
                "before": before,
                "after": after,
                "max_chars": budget,
            },
            outcome="ok",
            affected={"returned": len(views), "paper_id": window.paper_id},
            took_ms=took_ms,
        )
        return Envelope[ContextData](
            data=data,
            meta=_meta("paper_get_context", took_ms),
            warnings=warnings,
            citations=found,
        )

    # -------------------------------------------------------------- paper file
    @server.tool(
        name="paper_get_file",
        title="Get a short-lived link to the original PDF",
        description=(
            "Hand back a time-limited download URL for the stored original PDF. Use it "
            "only when the raw file itself matters (figures, unusual layout, checking a "
            "number); for reading text use paper_get_chunks or paper_get_context, which "
            "already give you page and section references. The link expires in a few "
            "minutes and needs no credentials: fetch it exactly as returned. "
            'Example: paper_get_file(paper_id="...")'
        ),
        meta={"toolset": settings.mcp_toolset},
    )
    async def paper_get_file(paper_id: str, ctx: Context | None = None) -> Envelope[FileData]:
        started = time.perf_counter()
        base_url = _base_url(ctx)
        with _Session() as session:
            paper = _load_paper(session, paper_id)
            record = paper_service.original_file(paper)
            if record is None:
                raise errors.ToolFailure(
                    code=errors.NOT_FOUND,
                    message=f"paper {paper_id!r} has no stored original file",
                    hint="the paper may be a metadata-only shell: see paper_get",
                )
            try:
                url, expires_at = download_signing.build_url(paper_id, base_url)
            except RuntimeError as exc:
                raise errors.ToolFailure(
                    code=errors.INTERNAL,
                    message=str(exc),
                    hint="ask the operator to set MCP_DOWNLOAD_SECRET",
                ) from exc
            data = FileData(
                download_url=url,
                expires_at=expires_at,
                filename=record.filename or "original.pdf",
                bytes=record.size_bytes,
                content_type=record.content_type,
                page_count=chunk_service.page_count(session, paper_id),
            )

        took_ms = int((time.perf_counter() - started) * 1000)
        log_call(
            tool="paper_get_file",
            agent="default",
            arguments={"paper_id": paper_id},
            outcome="ok",
            affected={"bytes": data.bytes, "ttl_s": download_signing.ttl_seconds()},
            took_ms=took_ms,
        )
        return Envelope[FileData](
            data=data,
            meta=_meta("paper_get_file", took_ms),
            warnings=[f"the link expires at {data.expires_at.isoformat()}"],
            citations=[],
        )

    # ------------------------------------------------------- paper job status
    @server.tool(
        name="paper_job_status",
        title="Check an ingestion job",
        description=(
            "Use this to follow up on a long import or reindex: pass the job_id a "
            "previous call handed back. Set wait_seconds to block until the job "
            "finishes (bounded, so a slow parse does not hold the connection open "
            "forever). Do not use it to poll in a tight loop -- if it comes back "
            "running, wait before asking again. "
            'Example: paper_job_status(job_id="...", wait_seconds=30)'
        ),
        meta={"toolset": settings.mcp_toolset},
    )
    async def paper_job_status(job_id: str, wait_seconds: int = 0) -> Envelope[Any]:
        started = time.perf_counter()
        arguments = {"job_id": job_id, "wait_seconds": wait_seconds}
        try:
            if wait_seconds < 0:
                raise errors.invalid_argument("wait_seconds must be >= 0")
            if wait_seconds > MAX_STATUS_WAIT_SECONDS:
                raise errors.invalid_argument(
                    f"wait_seconds must be <= {MAX_STATUS_WAIT_SECONDS}",
                    hint="poll again instead of waiting longer in one call",
                )
            deadline = started + wait_seconds
            while True:
                job = await asyncio.to_thread(_load_job, job_id)
                if _status_of(job) != "running" or time.perf_counter() >= deadline:
                    break
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            took_ms = int((time.perf_counter() - started) * 1000)
            warnings: list[str] = []
            if _status_of(job) == "running":
                warnings.append("still running; poll paper_job_status again")
            log_call(
                tool="paper_job_status",
                agent="default",
                arguments=arguments,
                outcome="ok",
                affected={"stage": job.stage},
                took_ms=took_ms,
            )
            return Envelope[Any](
                data=job,
                meta=_meta("paper_job_status", took_ms),
                warnings=warnings,
                citations=[],
            )
        except errors.ToolFailure as failure:
            log_call(
                tool="paper_job_status",
                agent="default",
                arguments=arguments,
                outcome="error",
                code=failure.code,
                took_ms=int((time.perf_counter() - started) * 1000),
            )
            raise


# --------------------------------------------------------------------------- #
# job helpers (module level so tests can stub the database boundary)
# --------------------------------------------------------------------------- #
def _load_job(job_id: str):
    """Read one job through the service layer (same shape as the REST endpoint)."""
    from app.schemas.job import JobOut
    from app.services import ingestion_service

    session = SessionLocal()
    try:
        job = ingestion_service.get_job(session, job_id)
        if job is None:
            raise errors.not_found("job", job_id)
        return JobOut.model_validate(ingestion_service.serialize_job(job))
    finally:
        session.close()


def _status_of(job) -> str:
    if job.stage in TERMINAL_STAGES:
        return "completed" if job.stage == "COMPLETED" else "failed"
    return "running"


__all__ = [
    "MAX_STATUS_WAIT_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "TERMINAL_STAGES",
    "register",
]
