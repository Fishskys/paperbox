"""Scripted acceptance for the MCP endpoint (contract sections 9 and 10).

One run walks the whole surface and prints a PASS/FAIL table:

* **transport** -- ``initialize``, ``tools/list``, and ``POST /mcp`` *without* the
  trailing slash answering in one hop (contract invariant: no 307 to ``/mcp/``).
* **auth** -- 401 without a credential, 403 with a wrong one, 421 when the ``Host``
  is outside ``MCP_ALLOWED_HOSTS``.
* **read** -- all six read tools on the live corpus, including the signed download
  URL actually downloading a PDF.
* **error paths** -- ``NOT_FOUND`` / ``INVALID_ARGUMENT`` / ``SSRF_BLOCKED``, plus
  the write tools being *absent* (not merely refused) when they are switched off.
* **write** (``--with-writes``) -- all four write tools, when the server has the
  master switch and the per-tool switches on.

Self-cleanup, and why it works this way: the script imports a **synthetic PDF with
unique bytes**, then finds the paper it created by **diffing the corpus before and
after** -- never by guessing from a job payload, and never by comparing hashes.
A paper that existed before the run is never touched: if the diff is not exactly one
new paper, the run aborts without deleting anything. (Earlier, importing a fixture
that happened to be byte-identical to a corpus paper, then deleting it by id, really
did delete a corpus paper. This script cannot repeat that.)

Usage::

    PAPER_API_KEY=<key> uv run python scripts/acceptance_mcp.py               # read-only
    PAPER_API_KEY=<key> uv run python scripts/acceptance_mcp.py --with-writes # + write tools
    uv run python scripts/acceptance_mcp.py --json report.json                # machine-readable

Pass the key through the environment (``PAPER_API_KEY``) rather than ``--token``: a token
given on the command line ends up in shell history and in whatever logs the caller keeps.

Exit code is 0 only when every check passed. With ``--with-writes`` the server must
run with ``MCP_WRITE_ENABLED=true`` plus ``MCP_ALLOW_DELETE`` / ``MCP_ALLOW_REINDEX`` /
``MCP_ALLOW_METADATA_WRITE``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8077"
READ_TOOLS = (
    "paper_search",
    "paper_get",
    "paper_get_chunks",
    "paper_get_context",
    "paper_get_file",
    "paper_job_status",
)
WRITE_TOOLS = (
    "paper_import",
    "paper_reindex",
    "paper_delete",
    "paper_update_metadata",
)
# The corpus query the read section runs against. Kept ordinary on purpose: it is the
# worked example from the contract, not a topic the acceptance asserts anything about.
SEARCH_QUERY = "Transformer attention architecture"
JOB_POLL_TIMEOUT_SECONDS = 180
MAX_CHARS_CEILING_PROBE = 10_000_000


# --------------------------------------------------------------------------------------
# pure helpers -- unit tested in tests/test_acceptance_mcp.py
# --------------------------------------------------------------------------------------


@dataclass
class Check:
    """One assertion in the report."""

    label: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    """Collected checks, with the exit-code rule in one place."""

    checks: list[Check] = field(default_factory=list)

    def add(self, label: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(label=label, ok=bool(ok), detail=detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""), flush=True)
        return bool(ok)

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]


def exit_code(report: Report) -> int:
    """0 when nothing failed -- the only success condition."""
    return 1 if report.failed else 0


def synthetic_pdf(marker: str, page_lines: int = 12) -> bytes:
    """A minimal, valid, single-page PDF whose text contains ``marker``.

    Deterministic for a given marker, and different markers give different bytes --
    which is the whole point: the import cannot collide with a real paper's hash.
    """
    lines = [marker, "", "MCP acceptance probe.", "This document is generated on the fly."]
    lines += [f"Line {index}: no bibliographic value whatsoever." for index in range(page_lines)]
    text_ops = "BT /F1 11 Tf 72 760 Td 14 TL\n" + "".join(
        f"({_pdf_escape(line)}) Tj T*\n" for line in lines
    ) + "ET"
    stream = text_ops.encode("latin-1")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n"
        "%%EOF\n"
    ).encode()
    return bytes(out)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def parse_ingest_roots(value: str) -> list[str]:
    """Split ``INGEST_LOCAL_ROOTS`` the way the server does (comma separated)."""
    return [part.strip().rstrip("\\/") for part in value.split(",") if part.strip()]


def default_probe_dir(explicit: str, env_value: str, env_file: Path) -> tuple[str, str]:
    """Pick where the probe PDF may live: it has to be inside an ``INGEST_LOCAL_ROOTS`` entry.

    Returns ``(directory, why)`` -- an empty directory means the write section cannot run.
    An explicit ``--probe-dir`` wins; otherwise the first configured root is used, since the
    server refuses local paths outside its roots with ``FORBIDDEN``.
    """
    if explicit:
        return explicit, "given with --probe-dir"
    roots = parse_ingest_roots(env_value)
    if not roots and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("INGEST_LOCAL_ROOTS="):
                roots = parse_ingest_roots(line.split("=", 1)[1])
                break
    if not roots:
        return "", "INGEST_LOCAL_ROOTS is not set; pass --probe-dir inside an allowed root"
    return roots[0], f"first INGEST_LOCAL_ROOTS entry ({roots[0]})"


def decide_cleanup(paper_id: str | None, before_ids: set[str]) -> tuple[bool, str]:
    """May this run delete ``paper_id``? Only if *this run* created it."""
    if not paper_id:
        return False, "no paper was created by this run"
    if paper_id in before_ids:
        return False, "refusing to delete a paper that existed before this run"
    return True, "created by this run"


def missing_from(before: set[str], after: set[str]) -> set[str]:
    """Ids present before and gone after -- the postcondition's red flag."""
    return set(before) - set(after)


def new_ids(before: set[str], after: set[str]) -> set[str]:
    return set(after) - set(before)


def error_payload(text: str) -> dict[str, Any]:
    """Pull the contract error JSON out of the SDK's ``Error executing tool ...`` text."""
    start = text.find("{")
    if start < 0:
        return {}
    try:
        # raw_decode stops at the end of the first JSON value, so trailing prose (or a
        # second object) does not turn a good payload into a parse failure.
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def error_code(text: str) -> str:
    payload = error_payload(text)
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    code = (error or {}).get("code")
    return code if isinstance(code, str) else ""


# --------------------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------------------


class Mcp:
    """Minimal Streamable HTTP client -- no SDK, so the wire format is what is tested."""

    def __init__(self, base_url: str, token: str | None, timeout: float = 240.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        # trust_env=False: on Windows httpx otherwise honours the machine-wide proxy
        # settings, and a request carrying a custom ``Host`` then dies at the proxy with
        # 502 instead of reaching the server (which correctly answers 421). Measured
        # 2026-10-04: same request -> 502 through the proxy, 421 direct.
        self.client = httpx.Client(timeout=timeout, trust_env=False)
        self._request_id = 0

    # -- raw http ----------------------------------------------------------------
    def rpc(
        self,
        method: str,
        params: dict | None = None,
        *,
        path: str = "/mcp",
        token: str | None = None,
        host: str | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        self._request_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
        if params is not None:
            body["params"] = params
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        credential = self.token if token is None else token
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        if host:
            headers["Host"] = host
        return self.client.post(
            f"{self.base_url}{path}",
            content=json.dumps(body),
            headers=headers,
            follow_redirects=follow_redirects,
        )

    # -- tool calls --------------------------------------------------------------
    def call(self, tool: str, arguments: dict | None = None) -> dict[str, Any]:
        """Call a tool, raising :class:`ToolError` when the server reports ``isError``."""
        response = self.rpc("tools/call", {"name": tool, "arguments": arguments or {}})
        response.raise_for_status()
        payload = response.json()["result"]
        if payload.get("isError"):
            text = payload["content"][0]["text"]
            raise ToolError(text)
        if "structuredContent" in payload and payload["structuredContent"] is not None:
            return payload["structuredContent"]
        return {"text": payload["content"][0]["text"] if payload.get("content") else ""}

    def expect_error(self, tool: str, arguments: dict | None = None) -> str:
        try:
            self.call(tool, arguments)
        except ToolError as failure:
            return failure.text
        raise AssertionError(f"{tool} unexpectedly succeeded")


class ToolError(RuntimeError):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def envelope_data(payload: dict[str, Any]) -> Any:
    return payload.get("data", payload)


# --------------------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------------------


def section_transport(mcp: Mcp, report: Report, *, expect_writes: bool) -> list[str]:
    print("\n== 1. transport ==")
    response = mcp.rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "acceptance_mcp", "version": "1"},
        },
    )
    report.add("initialize answers 200", response.status_code == 200, f"HTTP {response.status_code}")
    info = response.json()["result"]["serverInfo"]
    report.add("server identifies itself", info.get("name") == "paperbox", str(info))

    listing = mcp.rpc("tools/list", {})
    listed = [tool["name"] for tool in listing.json()["result"]["tools"]]
    for tool in READ_TOOLS:
        report.add(f"tools/list has {tool}", tool in listed)
    for tool in WRITE_TOOLS:
        if expect_writes:
            report.add(f"tools/list has {tool}", tool in listed)
        else:
            report.add(f"write tool {tool} is absent", tool not in listed, "not registered")
    report.add(
        "every tool declares an output schema",
        all("outputSchema" in tool for tool in listing.json()["result"]["tools"]),
    )

    # No trailing slash: it must be served directly, not 307-ed to /mcp/ (the fix for
    # a redirect that cost a round trip and dropped Authorization on some clients).
    direct = mcp.rpc("tools/list", {}, path="/mcp")
    report.add(
        "POST /mcp (no slash) is served, not redirected",
        direct.status_code == 200 and not direct.history,
        f"HTTP {direct.status_code}, {len(direct.history)} redirect(s)",
    )
    return listed


def section_auth(mcp: Mcp, report: Report, host_outside_allowlist: str) -> None:
    print("\n== 2. auth and host ==")
    anonymous = mcp.rpc("tools/list", {}, token="")
    report.add("no credential -> 401", anonymous.status_code == 401, f"HTTP {anonymous.status_code}")

    wrong = mcp.rpc("tools/list", {}, token="definitely-not-the-key")
    report.add("wrong credential -> 403", wrong.status_code == 403, f"HTTP {wrong.status_code}")

    bad_host = mcp.rpc("tools/list", {}, host=host_outside_allowlist)
    report.add(
        f"Host {host_outside_allowlist} -> 421",
        bad_host.status_code == 421,
        f"HTTP {bad_host.status_code}",
    )


def section_read(mcp: Mcp, report: Report) -> str | None:
    """Exercises the six read tools; returns a paper_id for the write section."""
    print("\n== 3. read tools ==")
    envelope = mcp.call("paper_search", {"query": SEARCH_QUERY, "top_k": 3})
    search = envelope.get("data") or {}
    # ``results`` is the paper list; ``citations`` hangs off the envelope, not off data.
    papers = search.get("results") or []
    citations = envelope.get("citations") or []
    report.add("paper_search returns papers", bool(papers), f"{len(papers)} hit(s)")
    report.add(
        "every citation carries a page and a section",
        bool(citations) and all(c.get("page") for c in citations),
        f"{len(citations)} citation(s)",
    )
    report.add(
        "citations point at chunks of the returned papers",
        all(c.get("chunk_id") for c in citations),
        f"{len(citations)} citation(s)",
    )
    if not papers:
        return None
    paper_id = papers[0]["paper_id"]

    detail = envelope_data(mcp.call("paper_get", {"paper_id": paper_id}))
    record = detail.get("paper") or {}
    report.add(
        "paper_get returns the searched paper",
        record.get("paper_id") == paper_id,
        str(record.get("title"))[:60],
    )
    report.add(
        "paper_get reports where each field came from",
        "provenance" in detail,
        f"{len(detail.get('provenance') or {})} field(s) with provenance",
    )

    chunks = envelope_data(mcp.call("paper_get_chunks", {"paper_id": paper_id, "limit": 3}))
    views = chunks.get("chunks") or []
    report.add("paper_get_chunks returns chunks", bool(views), f"{len(views)} chunk(s)")
    report.add(
        "chunks are typed views (page, section, text)",
        bool(views) and {"page", "section", "text"} <= set(views[0]),
    )
    if chunks.get("next_offset") is not None:
        report.add(
            "next_offset is a usable cursor",
            isinstance(chunks["next_offset"], int) and chunks["next_offset"] > 0,
            str(chunks["next_offset"]),
        )

    if citations:
        chunk_id = citations[0].get("chunk_id") or (views[0]["chunk_id"] if views else None)
        if chunk_id:
            context = envelope_data(
                mcp.call("paper_get_context", {"chunk_id": chunk_id, "before": 1, "after": 1})
            )
            window = context.get("chunks") or []
            report.add(
                "paper_get_context returns the asked-for chunk",
                any(view.get("chunk_id") == chunk_id for view in window),
                f"window of {len(window)}",
            )

    file_data = envelope_data(mcp.call("paper_get_file", {"paper_id": paper_id}))
    url = file_data.get("download_url") or ""
    report.add(
        "paper_get_file returns a signed URL",
        "sig=" in url and "exp=" in url,
        url.split("?")[-1][:48],
    )
    if url:
        download = mcp.client.get(url, follow_redirects=True)
        report.add(
            "the signed URL actually downloads the PDF",
            download.status_code == 200 and download.content[:4] == b"%PDF",
            f"HTTP {download.status_code}, {len(download.content)} bytes",
        )

    unknown_job = mcp.expect_error("paper_job_status", {"job_id": str(uuid.uuid4())})
    report.add(
        "unknown job -> NOT_FOUND",
        error_code(unknown_job) == "NOT_FOUND",
        error_code(unknown_job) or unknown_job[:80],
    )
    return paper_id


def section_error_paths(mcp: Mcp, report: Report, paper_id: str | None) -> None:
    print("\n== 4. error paths ==")
    if paper_id:
        missing = mcp.expect_error("paper_get", {"paper_id": str(uuid.uuid4())})
        report.add(
            "unknown paper -> NOT_FOUND",
            error_code(missing) == "NOT_FOUND",
            error_code(missing) or missing[:80],
        )
        ceiling = mcp.expect_error(
            "paper_get_chunks", {"paper_id": paper_id, "max_chars": MAX_CHARS_CEILING_PROBE}
        )
        report.add(
            "max_chars above the ceiling -> INVALID_ARGUMENT",
            error_code(ceiling) == "INVALID_ARGUMENT",
            error_code(ceiling) or ceiling[:80],
        )
        negative = mcp.expect_error(
            "paper_get_chunks", {"paper_id": paper_id, "offset": -1}
        )
        report.add(
            "negative offset -> INVALID_ARGUMENT",
            error_code(negative) == "INVALID_ARGUMENT",
            error_code(negative) or negative[:80],
        )


def section_writes(
    mcp: Mcp,
    report: Report,
    *,
    snapshot: "Snapshot",
    probe_dir: Path,
    keep_probe: bool,
) -> None:
    print("\n== 5. write tools ==")
    marker = f"paperbox MCP acceptance probe {uuid.uuid4()}"
    probe_slot = probe_dir / f"paperbox-mcp-acceptance-{uuid.uuid4().hex[:8]}"
    probe_slot.mkdir(parents=True, exist_ok=True)
    pdf_path = probe_slot / "acceptance_probe.pdf"
    pdf_path.write_bytes(synthetic_pdf(marker))
    report.add(
        "synthetic probe PDF is unique per run",
        len(set(synthetic_pdf(marker + suffix) for suffix in ("", "x"))) == 2,
    )
    report.add(
        "synthetic probe PDF names its run",
        marker.encode("latin-1") in pdf_path.read_bytes(),
    )

    # Recorded *before* the import, because Snapshot.take() overwrites the working set:
    # diffing the post-import set against itself would always report zero new papers.
    before_ids = set(snapshot.ids)
    created: str | None = None
    try:
        try:
            imported = envelope_data(
                mcp.call(
                    "paper_import",
                    {
                        "source": str(pdf_path),
                        "source_type": "local_path",
                        "wait_seconds": JOB_POLL_TIMEOUT_SECONDS,
                    },
                )
            )
        except ToolError as failure:
            report.add(
                "paper_import accepts a path inside INGEST_LOCAL_ROOTS",
                False,
                failure.text[:200],
            )
            return
        job_id = imported.get("job_id")
        status = str(imported.get("status") or "")
        if job_id and status != "completed":
            deadline = time.time() + JOB_POLL_TIMEOUT_SECONDS
            while time.time() < deadline:
                state = envelope_data(
                    mcp.call("paper_job_status", {"job_id": job_id, "wait_seconds": 15})
                )
                status = str(state.get("status") or state.get("stage") or "")
                if status in {"completed", "failed"}:
                    break
        report.add(
            "paper_import queues and finishes a job",
            status == "completed",
            f"job {job_id} -> {status}",
        )

        after = snapshot.take(mcp)
        fresh = new_ids(before_ids, after)
        report.add(
            "the import created exactly one new paper",
            len(fresh) == 1,
            f"{len(fresh)} new id(s): {sorted(fresh)}",
        )
        if len(fresh) != 1:
            report.add("aborting the write section", False, "cannot identify the paper to clean up")
            return
        created = fresh.pop()
        snapshot.ids.add(created)
        report.add(
            "the new paper is not one that predates this run",
            created not in snapshot.original_ids,
        )

        over_wait = mcp.expect_error(
            "paper_reindex", {"paper_id": created, "wait_seconds": 10_000_000}
        )
        report.add(
            "wait_seconds above the maximum -> INVALID_ARGUMENT (never clamped)",
            error_code(over_wait) == "INVALID_ARGUMENT",
            error_code(over_wait) or over_wait[:60],
        )

        queued = envelope_data(
            mcp.call("paper_reindex", {"paper_id": created, "dry_run": False, "wait_seconds": 0})
        )
        report.add(
            "paper_reindex(dry_run=false, wait_seconds=0) queues without waiting",
            bool(queued.get("job_id")) and str(queued.get("status")) in {"running", "queued"},
            f"job {queued.get('job_id')} -> {queued.get('status')}",
        )
        if queued.get("job_id"):
            finished = envelope_data(
                mcp.call("paper_job_status", {"job_id": queued["job_id"], "wait_seconds": 120})
            )
            # paper_job_status returns the raw job row: the stage is upper case and
            # there is no separate lowercase ``status`` key (that one belongs to the
            # job references paper_import/paper_reindex build themselves).
            stage = str(finished.get("stage") or "").lower()
            report.add(
                "paper_job_status follows the queued job to a terminal stage",
                stage in {"completed", "failed"},
                f"stage={finished.get('stage')} keys={sorted(finished)[:8]}",
            )

        preview = envelope_data(mcp.call("paper_reindex", {"paper_id": created}))
        report.add(
            "paper_reindex defaults to a dry run",
            preview.get("dry_run") is True or preview.get("queued") is not True,
            f"chunks={preview.get('chunks')}",
        )

        metadata = envelope_data(
            mcp.call(
                "paper_update_metadata",
                {"paper_id": created, "fields": {"title": f"Acceptance probe {marker[-8:]}"}},
            )
        )
        report.add(
            "paper_update_metadata reports before -> after",
            metadata.get("applied") is True and bool(metadata.get("changes")),
            json.dumps(metadata.get("changes", {}), ensure_ascii=False)[:90],
        )
        read_back = envelope_data(mcp.call("paper_get", {"paper_id": created}))
        title_after = str((read_back.get("paper") or {}).get("title", ""))
        report.add(
            "the metadata write is readable afterwards",
            "Acceptance probe" in title_after,
            title_after[:60],
        )

        delete_preview = envelope_data(mcp.call("paper_delete", {"paper_id": created}))
        report.add(
            "paper_delete defaults to a dry run",
            delete_preview.get("deleted") is not True,
            f"would remove {delete_preview.get('chunks')} chunk(s)",
        )

        deletion = envelope_data(
            mcp.call("paper_delete", {"paper_id": created, "dry_run": False})
        )
        report.add("paper_delete removes the paper", deletion.get("deleted") is True)
        report.add(
            "the deleted paper is gone from reads",
            error_code(mcp.expect_error("paper_get", {"paper_id": created})) == "NOT_FOUND",
        )
    finally:
        if created:
            final = snapshot.take(mcp)
            report.add(
                "no pre-existing paper disappeared",
                not missing_from(snapshot.original_ids, final),
                str(sorted(missing_from(snapshot.original_ids, final)))[:100],
            )
            if created in final:
                allowed, why = decide_cleanup(created, snapshot.original_ids)
                report.add("cleanup is allowed for this paper", allowed, why)
                if allowed and not keep_probe:
                    try:
                        mcp.call("paper_delete", {"paper_id": created, "dry_run": False})
                        report.add(
                            "cleanup removed the probe paper",
                            created not in snapshot.take(mcp),
                        )
                    except ToolError as failure:
                        report.add("cleanup removed the probe paper", False, failure.text[:120])
            else:
                report.add("no probe paper left behind", True, "already deleted by this run")
        shutil.rmtree(probe_slot, ignore_errors=True)


@dataclass
class Snapshot:
    """Live-corpus ids, so the script only ever touches what it created."""

    ids: set[str] = field(default_factory=set)
    original_ids: set[str] = field(default_factory=set)

    def take(self, mcp: "Mcp") -> set[str]:
        # Page through the whole list (review 2026-10-05, P3): a single
        # limit=500 page silently truncated the snapshot, which would have
        # broken both the "one paper fewer" assertion and the cleanup decision
        # once the corpus passed 500.
        self.ids: set[str] = set()
        offset = 0
        while True:
            response = mcp.client.get(
                f"{mcp.base_url}/api/papers",
                params={"limit": 500, "offset": offset},
                headers={"Authorization": f"Bearer {mcp.token}"} if mcp.token else {},
            )
            response.raise_for_status()
            body = response.json()
            page = {row["paper_id"] for row in body.get("papers", [])}
            self.ids |= page
            offset += 500
            if len(page) < 500 or offset >= 10000:
                break
        if not self.original_ids:
            self.original_ids = set(self.ids)
        return set(self.ids)

    @property
    def total(self) -> int:
        return len(self.ids)


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scripted acceptance for the MCP endpoint")
    parser.add_argument("--base-url", default=os.getenv("MCP_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument(
        "--token",
        default=os.getenv("PAPER_API_KEY", ""),
        help="prefer the PAPER_API_KEY environment variable: a token on the command line "
        "leaks into shell history and caller logs",
    )
    parser.add_argument(
        "--with-writes",
        action="store_true",
        help="also exercise the four write tools (server needs the write switches on)",
    )
    parser.add_argument(
        "--keep-probe",
        action="store_true",
        help="leave the probe paper in place (debugging only; it will not be deleted)",
    )
    parser.add_argument(
        "--bad-host",
        default=os.getenv("MCP_BAD_HOST", "not-in-the-allowlist.example"),
        help="Host header expected to be refused with 421",
    )
    parser.add_argument(
        "--probe-dir",
        default="",
        help="where to write the probe PDF; must sit inside INGEST_LOCAL_ROOTS "
        "(defaults to the first configured root)",
    )
    parser.add_argument("--json", dest="json_path", default="")
    args = parser.parse_args(argv)

    report = Report()
    mcp = Mcp(args.base_url, args.token or None)
    snapshot = Snapshot()
    try:
        health = mcp.client.get(f"{mcp.base_url}/health")
        report.add("app answers /health", health.status_code == 200, f"HTTP {health.status_code}")
        snapshot.take(mcp)
        print(f"corpus before the run: {snapshot.total} live paper(s)")

        section_transport(mcp, report, expect_writes=args.with_writes)
        section_auth(mcp, report, args.bad_host)
        paper_id = section_read(mcp, report)
        section_error_paths(mcp, report, paper_id)
        if args.with_writes:
            probe_dir, why = default_probe_dir(
                args.probe_dir,
                os.getenv("INGEST_LOCAL_ROOTS", ""),
                Path(__file__).resolve().parents[1] / ".env",
            )
            if not probe_dir:
                report.add("a writable ingest root is available", False, why)
            else:
                report.add("a writable ingest root is available", True, why)
                section_writes(
                    mcp,
                    report,
                    snapshot=snapshot,
                    probe_dir=Path(probe_dir),
                    keep_probe=args.keep_probe,
                )
            print(f"corpus after the run: {snapshot.total} live paper(s)")
        else:
            print("\n== 5. write tools ==")
            for tool in WRITE_TOOLS:
                text = mcp.expect_error(tool, {"paper_id": str(uuid.uuid4())})
                report.add(
                    f"{tool} is not reachable while switched off",
                    "Unknown tool" in text or error_code(text) == "WRITE_DISABLED",
                    (error_code(text) or text)[:70],
                )

        print(
            f"\n{len(report.checks) - len(report.failed)}/{len(report.checks)} checks passed"
        )
        for check in report.failed:
            print(f"  FAILED: {check.label} -- {check.detail}")
        if args.json_path:
            Path(args.json_path).write_text(
                json.dumps(
                    {
                        "base_url": args.base_url,
                        "with_writes": args.with_writes,
                        "corpus": sorted(snapshot.original_ids),
                        "checks": [check.__dict__ for check in report.checks],
                        "failed": len(report.failed),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return exit_code(report)
    finally:
        mcp.client.close()


if __name__ == "__main__":
    sys.exit(main())
