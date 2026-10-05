"""Writing MCP tools (contract: ``docs/architecture/11-mcp-agent-interface.md`` 5.2).

These four tools change state, so three rules are enforced here rather than left to
the caller:

* **they are invisible until the operator says otherwise** -- the master switch
  (``MCP_WRITE_ENABLED``) and the per-tool switches decide what gets *registered*, so
  a disabled tool never appears in ``tools/list`` and cannot be called by guessing
  its name;
* **the destructive ones default to a preview** -- ``paper_delete`` and
  ``paper_reindex`` run with ``dry_run=true`` unless the caller says ``false``, and a
  dry run makes **zero** mutating service calls;
* **everything they do is audited** with the blast radius (``affected``), because
  "which agent deleted that paper" is the question this layer exists to answer.

Like the reading tools, they call the service layer and never re-implement its
rules: ``paper_service.purge_paper``/``delete_preview``, ``ingestion_service``,
``metadata_manual`` and ``net_guard`` are the same functions the REST endpoints use.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from mcp.server import MCPServer

from app.core.config import settings
from app.db.session import SessionLocal
from app.mcp import auth, errors, jobs
from app.mcp.audit import log_call
from app.mcp.models import (
    ChangeView,
    DeletePreviewData,
    Envelope,
    ImportPreviewData,
    JobRefData,
    MetadataPatchData,
    ReindexPreviewData,
    ToolMeta,
)
from app.schemas.metadata import MetadataPatch
from app.services import (
    chunk_service,
    ingestion_service,
    local_scan,
    metadata_manual,
    net_guard,
    paper_service,
)
from app.services.api_key_service import ROLE_ADMIN, ROLE_WRITE, role_at_least

#: Switch that must be on for every writing tool (contract section 1.1).
MASTER_SWITCH = "MCP_WRITE_ENABLED"
#: Per-tool switches, checked at registration time *and* defensively at call time.
ALLOW_DELETE = "MCP_ALLOW_DELETE"
ALLOW_REINDEX = "MCP_ALLOW_REINDEX"
ALLOW_METADATA = "MCP_ALLOW_METADATA_WRITE"

#: Source types the writing tools accept (no file bytes over MCP; that is REST).
SOURCE_URL = "url"
SOURCE_LOCAL = "local_path"


class _Session:
    """A short-lived session for one tool call."""

    def __enter__(self):
        self._session = SessionLocal()
        return self._session

    def __exit__(self, *exc_info) -> None:
        self._session.close()


def enabled(variable: str) -> bool:
    """Whether a per-tool switch is on *and* the master switch allows it."""
    if not settings.mcp_write_enabled:
        return False
    mapping = {
        ALLOW_DELETE: settings.mcp_allow_delete,
        ALLOW_REINDEX: settings.mcp_allow_reindex,
        ALLOW_METADATA: settings.mcp_allow_metadata_write,
    }
    return bool(mapping.get(variable, False))


def require(variable: str, tool: str) -> None:
    """Defensive call-time check (registration already hid the tool).

    Registration is what keeps a disabled tool out of ``tools/list``; this catches
    the case where settings changed after the server was built.
    """
    if not enabled(variable):
        raise errors.write_disabled(variable, tool)


def require_role(tool: str, minimum: str) -> None:
    """Call-time tier check (plan §4.3): the key's role must reach ``minimum``.

    Registration cannot do this -- which tools a key may call depends on the
    key, not the build. With ``AUTH_ENABLED=false`` every caller is the
    anonymous admin, so the switch gates (``require`` above) remain the only
    guards, exactly as before.
    """
    identity = auth.current_identity()
    if identity is None or not role_at_least(identity.role, minimum):
        raise errors.forbidden_role(tool, minimum)


def _meta(tool: str, took_ms: int) -> ToolMeta:
    return ToolMeta(
        tool=tool, agent=auth.agent_name(), toolset=settings.mcp_toolset, took_ms=took_ms
    )


def _load_live_paper(session, paper_id: str):
    paper = paper_service.get_paper(session, paper_id)
    if paper is None or paper.deleted_at is not None:
        raise errors.not_found("paper", paper_id)
    return paper


def _job_ref(job, waited_s: int) -> JobRefData:
    return JobRefData(
        job_id=job.job_id,
        paper_id=getattr(job, "paper_id", None),
        status=jobs.status_of(job),
        stage=job.stage,
        error_code=getattr(job, "error_code", None),
        error_message=getattr(job, "error_message", None),
        waited_s=waited_s,
    )


def _local_pdf(source: str) -> tuple[Path, int]:
    """Validate a server-side PDF path against the whitelist; return (path, size).

    A file (not a directory): MCP imports one paper per call so the result is one
    job the caller can follow. Bulk folder import stays on REST ``/ingest/dir``,
    which reports per-file outcomes and has its own ``dry_run``.
    """
    roots = settings.local_roots
    if not roots:
        raise errors.ToolFailure(
            code=errors.FORBIDDEN,
            message="server-side path import is disabled on this server",
            hint="ask the operator to set INGEST_LOCAL_ROOTS to the directory papers live in",
        )
    raw = (source or "").strip()
    if not raw:
        raise errors.invalid_argument("source is required for source_type=local_path")
    resolved = Path(os.path.realpath(os.path.expanduser(raw)))
    if not any(local_scan.is_within(resolved, root) for root in roots):
        raise errors.ToolFailure(
            code=errors.FORBIDDEN,
            message=f"path is outside the allowed roots: {raw}",
            hint="INGEST_LOCAL_ROOTS lists the directories this server may read from",
        )
    if not resolved.is_file():
        raise errors.invalid_argument(f"not a file: {raw}")
    if resolved.suffix.lower() != ".pdf":
        raise errors.invalid_argument(f"only PDF files can be imported: {resolved.name}")
    size = resolved.stat().st_size
    try:
        ingestion_service.ensure_size(size)
    except ingestion_service.UnsupportedSource as exc:
        raise errors.ToolFailure(code="OVERSIZED", message=str(exc)) from exc
    return resolved, size


def _check_url(source: str) -> str:
    """Validate an inbound URL through the shared safety gate (contract section 6)."""
    try:
        return net_guard.check_url(source)
    except net_guard.URLBlocked as exc:
        raise errors.ToolFailure(
            code=errors.SSRF_BLOCKED,
            message=exc.reason,
            hint="add the host or its CIDR to INGEST_ALLOW_PRIVATE_HOSTS to allow it",
        ) from exc


def register(server: MCPServer) -> None:
    """Register the writing tools this deployment allows."""

    # ------------------------------------------------------------------ import
    @server.tool(
        name="paper_import",
        title="Import a paper (URL or server-side file)",
        description=(
            "Add one paper to the corpus and have it parsed, chunked and indexed. Use "
            "source_type='url' for a PDF on the web (the server refuses private, "
            "loopback and link-local addresses) or 'local_path' for a PDF already on "
            "the server. Importing runs a pipeline, so this call waits a little and "
            "then hands back either the finished paper_id or the job_id to follow with "
            "paper_job_status. Set dry_run=true to validate the source without queueing "
            "anything. It does not take file bytes: upload those over REST. "
            'Example: paper_import(source="https://arxiv.org/pdf/1807.11311")'
        ),
        meta={"toolset": settings.mcp_toolset},
    )
    async def paper_import(
        source: str,
        source_type: str = SOURCE_URL,
        wait_seconds: int | None = None,
        dry_run: bool = False,
    ) -> Envelope[ImportPreviewData | JobRefData]:
        started = time.perf_counter()
        require_role("paper_import", ROLE_WRITE)
        if not settings.mcp_write_enabled:
            raise errors.write_disabled(MASTER_SWITCH, "paper_import")
        known = settings.mcp_wait_seconds if wait_seconds is None else wait_seconds
        bound = jobs.check_wait(known)

        if source_type == SOURCE_URL:
            checked = _check_url(source)
            filename = ingestion_service.filename_from_url(checked)
            payload = ImportPreviewData(
                source=checked, source_type=SOURCE_URL, filename=filename
            )
        elif source_type == SOURCE_LOCAL:
            path, size = _local_pdf(source)
            payload = ImportPreviewData(
                source=source,
                source_type=SOURCE_LOCAL,
                filename=path.name,
                size_bytes=size,
                content_type=ingestion_service.PDF_CONTENT_TYPE,
                resolved_path=str(path),
            )
        else:
            raise errors.invalid_argument(
                f"source_type must be '{SOURCE_URL}' or '{SOURCE_LOCAL}'"
            )

        if dry_run:
            took_ms = int((time.perf_counter() - started) * 1000)
            log_call(
                tool="paper_import",
                agent=auth.agent_name(),
                arguments={
                    "source": source,
                    "source_type": source_type,
                    "dry_run": True,
                },
                outcome="ok",
                affected={"queued": False},
                took_ms=took_ms,
            )
            return Envelope[ImportPreviewData | JobRefData](
                data=payload,
                meta=_meta("paper_import", took_ms),
                warnings=["dry run: nothing was queued"],
                citations=[],
            )

        with _Session() as session:
            if source_type == SOURCE_URL:
                job = ingestion_service.create_job(
                    session, source_type=SOURCE_URL, source=payload.source
                )
            else:
                job = ingestion_service.create_job(
                    session,
                    source_type=ingestion_service.SOURCE_TYPE_LOCAL,
                    filename=payload.filename,
                    content_type=ingestion_service.PDF_CONTENT_TYPE,
                    size_bytes=payload.size_bytes,
                    payload={"local_path": payload.resolved_path},
                )
            session.commit()
            from app.workers import queue as job_queue

            job_queue.submit(session, job.id, job_queue.KIND_INGEST)
            job_id = job.id

        finished, waited = await jobs.wait_for_job(job_id, bound)
        took_ms = int((time.perf_counter() - started) * 1000)
        warnings: list[str] = []
        if jobs.status_of(finished) == "running":
            warnings.append(
                f"still running after {waited}s; follow it with paper_job_status(job_id='{job_id}')"
            )
        if finished.error_message:
            warnings.append(f"pipeline failed: {finished.error_code}")
        log_call(
            tool="paper_import",
            agent=auth.agent_name(),
            arguments={"source_type": source_type, "wait_seconds": bound},
            outcome="ok",
            affected={
                "job_id": job_id,
                "stage": finished.stage,
                "paper_id": getattr(finished, "paper_id", None),
            },
            took_ms=took_ms,
        )
        return Envelope[ImportPreviewData | JobRefData](
            data=_job_ref(finished, waited),
            meta=_meta("paper_import", took_ms),
            warnings=warnings,
            citations=[],
        )

    # ----------------------------------------------------------------- reindex
    if enabled(ALLOW_REINDEX):

        @server.tool(
            name="paper_reindex",
            title="Rebuild a paper's chunks and vectors",
            description=(
                "Re-parse, re-chunk, re-embed and re-index one paper that is already in "
                "the corpus: use it after the parser changed, or when a paper looks "
                "wrong in search results. It defaults to dry_run=true, which reports the "
                "blast radius (existing chunks, whether the original file is there, "
                "whether a job is already running) and changes nothing. Pass dry_run="
                "false to actually queue it. "
                'Example: paper_reindex(paper_id="...", dry_run=false, wait_seconds=120)'
            ),
            meta={"toolset": settings.mcp_toolset},
        )
        async def paper_reindex(
            paper_id: str,
            dry_run: bool = True,
            wait_seconds: int | None = None,
        ) -> Envelope[ReindexPreviewData | JobRefData]:
            started = time.perf_counter()
            require(ALLOW_REINDEX, "paper_reindex")
            require_role("paper_reindex", ROLE_WRITE)
            bound = jobs.check_wait(
                settings.mcp_wait_seconds if wait_seconds is None else wait_seconds
            )

            with _Session() as session:
                paper = _load_live_paper(session, paper_id)
                record = paper_service.original_file(paper)
                if record is None:
                    raise errors.ToolFailure(
                        code=errors.NOT_FOUND,
                        message=f"paper {paper_id!r} has no stored original file",
                        hint="a paper without bytes cannot be re-parsed",
                    )
                preview = ReindexPreviewData(
                    paper_id=paper_id,
                    title=paper.title or "",
                    chunks=chunk_service.count_chunks(session, paper.id),
                    has_original_file=True,
                    filename=record.filename,
                    running_jobs=ingestion_service.count_running_jobs(session, paper_id),
                )
                if not dry_run:
                    job = ingestion_service.create_reindex_job(session, paper, record)
                    job_id = job.id

            if dry_run:
                took_ms = int((time.perf_counter() - started) * 1000)
                log_call(
                    tool="paper_reindex",
                    agent=auth.agent_name(),
                    arguments={"paper_id": paper_id, "dry_run": True},
                    outcome="ok",
                    affected={"chunks": preview.chunks, "queued": False},
                    took_ms=took_ms,
                )
                return Envelope[ReindexPreviewData | JobRefData](
                    data=preview,
                    meta=_meta("paper_reindex", took_ms),
                    warnings=["dry run: no job was queued"],
                    citations=[],
                )

            finished, waited = await jobs.wait_for_job(job_id, bound)
            took_ms = int((time.perf_counter() - started) * 1000)
            warnings: list[str] = []
            if preview.running_jobs:
                warnings.append(
                    f"{preview.running_jobs} job(s) were already running for this paper"
                )
            if jobs.status_of(finished) == "running":
                warnings.append(f"still running; poll paper_job_status(job_id='{job_id}')")
            log_call(
                tool="paper_reindex",
                agent=auth.agent_name(),
                arguments={"paper_id": paper_id, "dry_run": False},
                outcome="ok",
                affected={"job_id": job_id, "stage": finished.stage},
                took_ms=took_ms,
            )
            return Envelope[ReindexPreviewData | JobRefData](
                data=_job_ref(finished, waited),
                meta=_meta("paper_reindex", took_ms),
                warnings=warnings,
                citations=[],
            )

    # ------------------------------------------------------------------ delete
    if enabled(ALLOW_DELETE):

        @server.tool(
            name="paper_delete",
            title="Delete a paper",
            description=(
                "Remove one paper from the corpus: its indexed chunks, its stored "
                "files, and the paper itself (soft delete -- the row is kept, "
                "consistent with REST). It defaults to dry_run=true and only reports "
                "what would go; pass dry_run=false to actually delete. This is the one "
                "call in this toolset that cannot be undone from MCP. "
                'Example: paper_delete(paper_id="...", dry_run=true)'
            ),
            meta={"toolset": settings.mcp_toolset},
        )
        async def paper_delete(paper_id: str, dry_run: bool = True) -> Envelope[DeletePreviewData]:
            started = time.perf_counter()
            require(ALLOW_DELETE, "paper_delete")
            require_role("paper_delete", ROLE_ADMIN)
            with _Session() as session:
                paper = _load_live_paper(session, paper_id)
                preview = paper_service.delete_preview(session, paper)
                data = DeletePreviewData(
                    paper_id=paper_id,
                    title=preview.title,
                    chunks=preview.chunks,
                    objects=preview.objects,
                    object_bytes=preview.object_bytes,
                    running_jobs=preview.running_jobs,
                )
                if not dry_run:
                    outcome = paper_service.purge_paper(session, paper)
                    data.deleted = True
                    data.objects_removed = outcome.objects_removed
                    data.chunks = outcome.chunks_removed

            took_ms = int((time.perf_counter() - started) * 1000)
            warnings: list[str] = []
            if dry_run:
                warnings.append("dry run: nothing was deleted")
            if data.running_jobs:
                warnings.append(
                    f"{data.running_jobs} job(s) were running for this paper: their "
                    "pipeline may still write to a paper that is now gone"
                )
            log_call(
                tool="paper_delete",
                agent=auth.agent_name(),
                arguments={"paper_id": paper_id, "dry_run": dry_run},
                outcome="ok",
                affected={
                    "deleted": data.deleted,
                    "chunks": data.chunks,
                    "objects": data.objects,
                },
                took_ms=took_ms,
            )
            return Envelope[DeletePreviewData](
                data=data, meta=_meta("paper_delete", took_ms), warnings=warnings, citations=[]
            )

    # ---------------------------------------------------------------- metadata
    if enabled(ALLOW_METADATA):

        @server.tool(
            name="paper_update_metadata",
            title="Correct a paper's metadata",
            description=(
                "Fix bibliographic fields of one paper (title, authors, venue, year, "
                "doi, arxiv_id, tags, ...) and report what changed, old value -> new "
                "value. Every edit is recorded as a manual claim, so it is visible in "
                "the paper's provenance; undoing it is a REST call "
                "(POST /api/papers/{id}/metadata/rollback). Set dry_run=true to see the "
                "current values without writing. "
                'Example: paper_update_metadata(paper_id="...", fields={"venue": {"name": "ISSCC", "year": 2021}})'
            ),
            meta={"toolset": settings.mcp_toolset},
        )
        async def paper_update_metadata(
            paper_id: str, fields: MetadataPatch, dry_run: bool = False
        ) -> Envelope[MetadataPatchData]:
            started = time.perf_counter()
            require(ALLOW_METADATA, "paper_update_metadata")
            require_role("paper_update_metadata", ROLE_WRITE)
            payload = fields.model_dump(exclude_unset=True)
            if not payload:
                raise errors.invalid_argument("fields must contain at least one key")

            with _Session() as session:
                paper = _load_live_paper(session, paper_id)
                before = metadata_manual.preview_patch(session, paper, payload)
                data = MetadataPatchData(
                    paper_id=paper_id,
                    applied=False,
                    changes={
                        key: ChangeView(before=value, after=payload.get(key))
                        for key, value in before.items()
                    },
                    rejected=[key for key in payload if key not in before],
                    rollback=f"POST /api/papers/{paper_id}/metadata/rollback",
                )
                if not dry_run:
                    result = metadata_manual.patch_metadata(session, paper, payload)
                    session.commit()
                    after = metadata_manual.preview_patch(session, paper, payload)
                    data.applied = True
                    data.changed = list(result.fields)
                    data.rejected = list(result.rejected)
                    data.fingerprint = result.fingerprint
                    data.changes = {
                        key: ChangeView(
                            before=before.get(key),
                            after=after.get(key, payload.get(key)),
                        )
                        for key in set(before) | set(result.fields)
                    }

            took_ms = int((time.perf_counter() - started) * 1000)
            warnings: list[str] = []
            if dry_run:
                warnings.append("dry run: nothing was written")
            if data.rejected:
                warnings.append(f"ignored unknown fields: {', '.join(data.rejected)}")
            log_call(
                tool="paper_update_metadata",
                agent=auth.agent_name(),
                arguments={
                    "paper_id": paper_id,
                    "dry_run": dry_run,
                    "fields": sorted(payload),
                },
                outcome="ok",
                affected={"changed": data.changed or sorted(data.changes)},
                took_ms=took_ms,
            )
            return Envelope[MetadataPatchData](
                data=data,
                meta=_meta("paper_update_metadata", took_ms),
                warnings=warnings,
                citations=[],
            )


__all__ = [
    "ALLOW_DELETE",
    "ALLOW_METADATA",
    "ALLOW_REINDEX",
    "MASTER_SWITCH",
    "SOURCE_LOCAL",
    "SOURCE_URL",
    "enabled",
    "register",
    "require",
]
