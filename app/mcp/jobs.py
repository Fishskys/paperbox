"""Job waiting, shared by ``paper_job_status`` and the writing tools.

Reading and writing tools need the same three things -- load a job through the
service layer, tell whether it is still moving, and wait for it with a bound -- so
they live here once. The bound matters: an MCP call that blocks for ten minutes is a
client timeout, not patience, and the contract's rule is that a timeout is **not** a
failure (the caller gets the job id and polls).
"""

from __future__ import annotations

import asyncio
import time

from app.db.session import SessionLocal
from app.mcp import errors

#: Stages after which a job will not move on its own.
TERMINAL_STAGES = frozenset({"COMPLETED", "FAILED"})

#: Poll interval, and the hard ceiling for any tool's ``wait_seconds``.
POLL_INTERVAL_SECONDS = 1.0
MAX_WAIT_SECONDS = 600


def load_job(job_id: str):
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


def status_of(job) -> str:
    """``completed`` / ``failed`` / ``running`` for one job payload."""
    if job.stage not in TERMINAL_STAGES:
        return "running"
    return "completed" if job.stage == "COMPLETED" else "failed"


def check_wait(wait_seconds: int) -> int:
    """Validate a caller's ``wait_seconds`` (never silently clamp it)."""
    value = int(wait_seconds)
    if value < 0:
        raise errors.invalid_argument("wait_seconds must be >= 0")
    if value > MAX_WAIT_SECONDS:
        raise errors.invalid_argument(
            f"wait_seconds must be <= {MAX_WAIT_SECONDS}",
            hint="poll the job instead of waiting longer in one call",
        )
    return value


async def wait_for_job(job_id: str, wait_seconds: int):
    """Poll one job until it leaves ``running`` or the bound expires.

    Returns ``(job, waited_seconds)``. A job that is still running when the bound
    expires is **not** an error: the caller gets the job id, the stage and a warning.
    """
    bound = check_wait(wait_seconds)
    started = time.perf_counter()
    deadline = started + bound
    while True:
        job = await asyncio.to_thread(load_job, job_id)
        if status_of(job) != "running" or time.perf_counter() >= deadline:
            return job, int(time.perf_counter() - started)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


__all__ = [
    "MAX_WAIT_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "TERMINAL_STAGES",
    "check_wait",
    "load_job",
    "status_of",
    "wait_for_job",
]
