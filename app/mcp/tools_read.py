"""Read-only MCP tools (contract doc section 5.1).

First tool: ``paper_job_status``. It is deliberately the simplest one -- no content
budget, no citations, no aggregation -- so the transport (mount, allowlist,
stateless HTTP, structured output) can be proven end to end before the search and
reading tools land. The rest of the read family follows the same shape:

    typed arguments  ->  service call  ->  Envelope(data, meta, warnings, citations)
"""

from __future__ import annotations

import asyncio
import time

from mcp.server import MCPServer

from app.core.config import settings
from app.db.session import SessionLocal
from app.mcp import errors
from app.mcp.audit import log_call
from app.mcp.models import Citation, Envelope, ToolMeta
from app.schemas.job import JobOut
from app.services import ingestion_service

#: Stages after which a job will not move on its own.
TERMINAL_STAGES = frozenset({"COMPLETED", "FAILED"})

#: Poll interval and hard ceiling for ``wait_seconds`` on the status tool.
POLL_INTERVAL_SECONDS = 1.0
MAX_STATUS_WAIT_SECONDS = 600


def _load_job(job_id: str) -> JobOut:
    """Read one job through the service layer (same shape as the REST endpoint)."""
    session = SessionLocal()
    try:
        job = ingestion_service.get_job(session, job_id)
        if job is None:
            raise errors.not_found("job", job_id)
        return JobOut.model_validate(ingestion_service.serialize_job(job))
    finally:
        session.close()


def _status_of(job: JobOut) -> str:
    if job.stage in TERMINAL_STAGES:
        return "completed" if job.stage == "COMPLETED" else "failed"
    return "running"


def _summarise(job: JobOut) -> str:
    """One human readable line, so a client that ignores structured content is fine."""
    parts = [f"job {job.job_id} is {job.stage}", f"progress {job.progress:.0%}"]
    if job.paper_id:
        parts.append(f"paper {job.paper_id}")
    if job.error_code:
        parts.append(f"error {job.error_code}: {job.error_message or ''}".strip())
    return "; ".join(parts)


def register(server: MCPServer) -> None:
    """Attach every read tool to ``server`` (called once, at import/build time)."""

    @server.tool(
        name="paper_job_status",
        title="Check an ingestion job",
        description=(
            "Use this to follow up on a long import or reindex: pass the job_id a "
            "previous call handed back. Set wait_seconds to block until the job "
            "finishes (bounded, so a slow parse does not hold the connection open "
            "forever). Do not use it to poll in a tight loop -- if it returns "
            "status='running', wait before asking again."
        ),
        meta={"toolset": settings.mcp_toolset},
    )
    async def paper_job_status(job_id: str, wait_seconds: int = 0) -> Envelope[JobOut]:
        started = time.perf_counter()
        agent = "default"
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
                job = _load_job(job_id)
                if _status_of(job) != "running" or time.perf_counter() >= deadline:
                    break
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            took_ms = int((time.perf_counter() - started) * 1000)
            warnings: list[str] = []
            if _status_of(job) == "running":
                warnings.append("still running; poll paper_job_status again")
            log_call(
                tool="paper_job_status",
                agent=agent,
                arguments={"job_id": job_id, "wait_seconds": wait_seconds},
                outcome="ok",
                affected={"stage": job.stage},
                took_ms=took_ms,
            )
            return Envelope[JobOut](
                data=job,
                meta=ToolMeta(
                    tool="paper_job_status", agent=agent, toolset=settings.mcp_toolset, took_ms=took_ms
                ),
                warnings=warnings,
                citations=[],
            )
        except errors.ToolFailure as failure:
            log_call(
                tool="paper_job_status",
                agent=agent,
                arguments={"job_id": job_id, "wait_seconds": wait_seconds},
                outcome="error",
                code=failure.code,
                took_ms=int((time.perf_counter() - started) * 1000),
            )
            raise
