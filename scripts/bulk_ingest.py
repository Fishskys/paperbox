#!/usr/bin/env python3
"""Bulk-import the evaluation corpus over HTTP (SPEC-P1 section F).

Reads a list of arXiv ids (``evals/arxiv_ids.txt``, ``id<TAB>topic<TAB>title``,
``#`` comments and blank lines ignored) and imports them one at a time against
a *running* paperbox instance:

    uv run python scripts/bulk_ingest.py --dry-run --limit 3
    uv run python scripts/bulk_ingest.py --resume
    uv run python scripts/bulk_ingest.py --file evals/arxiv_ids.txt --limit 20

Each entry is POSTed to ``/api/papers/ingest`` as
``{"source_type": "url", "source": "https://arxiv.org/pdf/<id>"}``; the returned
``job_id`` is polled on ``GET /api/jobs/{job_id}`` until the job leaves
``RECEIVED``/``*_ING``, or until ``--timeout`` elapses. Entries run strictly
sequentially (no concurrency) so the pipeline is never overloaded.

A failing entry never aborts the run: the error is recorded, the loop moves on
and the tail of the run prints the failures grouped by ``error_code``. The
summary is written to ``evals/ingest-report.json`` (``--out``):

    {"started_at", "finished_at", "base_url", "total", "completed", "failed",
     "skipped", "items": [{"arxiv_id", "paper_id", "job_id", "status", "stage",
                           "error_code", "error_message", "chunks", "elapsed_s"}]}

``--dry-run`` only parses the list and prints what would be imported; it sends
no request. The API key is never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
DEFAULT_FILE = ROOT / "evals" / "arxiv_ids.txt"
DEFAULT_OUT = ROOT / "evals" / "ingest-report.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8077"
DEFAULT_TIMEOUT = 300.0
DEFAULT_POLL_INTERVAL = 2.0
REQUEST_TIMEOUT = 60.0
PAPER_PAGE_SIZE = 100
TERMINAL_STAGES = {"COMPLETED", "FAILED"}
DRY_RUN_PREVIEW = 3


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
def load_env(path: Path = ENV) -> dict[str, str]:
    """Minimal ``KEY=VALUE`` reader (same convention as the other scripts)."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def parse_arxiv_list(text: str) -> list[dict[str, str]]:
    """Parse ``id<TAB>topic<TAB>title`` lines, skipping ``#`` and blanks.

    The topic/title columns are optional; everything after the id is kept as
    free text so the report can echo it. Ids are de-duplicated, first wins.
    """
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("\t") if part.strip()]
        if not parts:
            continue
        arxiv_id = parts[0]
        if arxiv_id in seen:
            continue
        seen.add(arxiv_id)
        entries.append(
            {
                "arxiv_id": arxiv_id,
                "topic": parts[1] if len(parts) > 1 else "",
                "title": parts[2] if len(parts) > 2 else "",
            }
        )
    return entries


def load_arxiv_list(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"arxiv list not found: {path}")
    return parse_arxiv_list(path.read_text(encoding="utf-8"))


def pdf_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/pdf/{arxiv_id}"


# --------------------------------------------------------------------------- #
# API calls
# --------------------------------------------------------------------------- #
def fetch_existing_arxiv_ids(client: httpx.Client) -> set[str]:
    """Every ``arxiv_id`` already in the library (paginated ``GET /api/papers``)."""
    existing: set[str] = set()
    offset = 0
    while True:
        response = client.get(
            "/api/papers", params={"limit": PAPER_PAGE_SIZE, "offset": offset}
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else payload
        items = items or []
        for item in items:
            if not isinstance(item, dict):
                continue
            arxiv_id = item.get("arxiv_id")
            if arxiv_id:
                existing.add(str(arxiv_id))
        if len(items) < PAPER_PAGE_SIZE:
            return existing
        offset += PAPER_PAGE_SIZE


def start_ingest(client: httpx.Client, arxiv_id: str) -> str:
    """``POST /api/papers/ingest`` -> ``job_id``."""
    response = client.post(
        "/api/papers/ingest",
        json={"source_type": "url", "source": pdf_url(arxiv_id)},
    )
    response.raise_for_status()
    payload = response.json()
    job_id = payload.get("job_id") or payload.get("id")
    if not job_id:
        raise RuntimeError(f"ingest response has no job_id: {payload!r}")
    return str(job_id)


def poll_job(
    client: httpx.Client,
    job_id: str,
    *,
    timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    """Poll ``GET /api/jobs/{id}`` until a terminal stage or the deadline.

    Raises ``TimeoutError`` when the job is still running after ``timeout``
    seconds; the caller records it as a failed entry.
    """
    started = time.monotonic()
    last: dict[str, Any] = {}
    while True:
        response = client.get(f"/api/jobs/{job_id}")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            last = payload
        stage = str(last.get("stage") or "")
        if stage in TERMINAL_STAGES:
            return last
        if time.monotonic() - started >= timeout:
            raise TimeoutError(
                f"job {job_id} still {stage or 'unknown'} after {timeout:.0f}s"
            )
        time.sleep(poll_interval)


def chunk_count(client: httpx.Client, paper_id: str) -> int | None:
    """``chunks`` from ``GET /api/papers/{id}`` when the API reports it."""
    try:
        response = client.get(f"/api/papers/{paper_id}")
        response.raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001 - a missing count must not fail the run
        return None
    if isinstance(payload, dict):
        value = payload.get("chunks")
        if isinstance(value, int):
            return value
    return None


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_console(report: dict[str, Any]) -> str:
    lines = [
        "",
        f"total {report['total']}  completed {report['completed']}  "
        f"failed {report['failed']}  skipped {report['skipped']}",
    ]
    failures = [item for item in report["items"] if item["status"] == "failed"]
    if failures:
        grouped: dict[str, int] = {}
        for item in failures:
            key = item.get("error_code") or "UNKNOWN"
            grouped[key] = grouped.get(key, 0) + 1
        lines.append("failures by error_code:")
        for code, count in sorted(grouped.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {code:<24} {count}")
        lines.append("failed entries:")
        for item in failures:
            lines.append(
                f"  {item['arxiv_id']}  {item.get('error_code') or '-'}  "
                f"{(item.get('error_message') or '')[:120]}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="arXiv id list")
    parser.add_argument("--limit", default=None, type=int, help="import at most N entries")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="summary JSON path")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip entries whose arxiv_id is already in the library",
    )
    parser.add_argument("--base-url", default=None, help="paperbox base url")
    parser.add_argument("--api-key", default=None, help="bearer token (never printed)")
    parser.add_argument(
        "--timeout", default=DEFAULT_TIMEOUT, type=float, help="per-paper poll budget (s)"
    )
    parser.add_argument(
        "--poll-interval", default=DEFAULT_POLL_INTERVAL, type=float, help="poll spacing (s)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only parse the list and print what would be imported (no HTTP)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        print("--limit must be positive", file=sys.stderr)
        return 2

    env = load_env()
    base_url = (
        args.base_url
        or env.get("PAPER_API_BASE")
        or env.get("PAPER_API_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")
    api_key = args.api_key or env.get("PAPER_API_KEY") or ""

    list_path = Path(args.file)
    if not list_path.is_absolute() and not list_path.exists():
        list_path = ROOT / list_path
    entries = load_arxiv_list(list_path)
    if args.limit is not None:
        entries = entries[: args.limit]

    if args.dry_run:
        print(f"file: {list_path}")
        print(f"base_url: {base_url}")
        print(f"api_key: {'set' if api_key else 'missing'}")
        print(f"would import: {len(entries)} entries")
        for entry in entries[:DRY_RUN_PREVIEW]:
            print(f"  {entry['arxiv_id']}  {pdf_url(entry['arxiv_id'])}")
        if len(entries) > DRY_RUN_PREVIEW:
            print(f"  ... and {len(entries) - DRY_RUN_PREVIEW} more")
        return 0

    started_at = _now()
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    print(f"bulk ingest: {len(entries)} entries -> {base_url}")

    with httpx.Client(base_url=base_url, headers=headers, timeout=REQUEST_TIMEOUT) as client:
        existing: set[str] = set()
        if args.resume:
            try:
                existing = fetch_existing_arxiv_ids(client)
            except Exception as exc:  # noqa: BLE001 - degrade to a full run
                print(f"  WARN could not list existing papers ({type(exc).__name__}: {exc}); importing everything")
            else:
                print(f"  resume: {len(existing)} arxiv ids already in the library")

        for index, entry in enumerate(entries, start=1):
            arxiv_id = entry["arxiv_id"]
            prefix = f"  [{index}/{len(entries)}] {arxiv_id}"
            if args.resume and arxiv_id in existing:
                print(f"{prefix} skipped (already ingested)")
                skipped.append(
                    {
                        "arxiv_id": arxiv_id,
                        "paper_id": None,
                        "job_id": None,
                        "status": "skipped",
                        "stage": None,
                        "error_code": None,
                        "error_message": None,
                        "chunks": None,
                        "elapsed_s": 0.0,
                    }
                )
                continue

            row: dict[str, Any] = {
                "arxiv_id": arxiv_id,
                "paper_id": None,
                "job_id": None,
                "status": "failed",
                "stage": None,
                "error_code": None,
                "error_message": None,
                "chunks": None,
                "elapsed_s": 0.0,
            }
            started = time.monotonic()
            try:
                job_id = start_ingest(client, arxiv_id)
                row["job_id"] = job_id
                job = poll_job(
                    client,
                    job_id,
                    timeout=args.timeout,
                    poll_interval=args.poll_interval,
                )
                row["stage"] = job.get("stage")
                row["paper_id"] = job.get("paper_id")
                row["error_code"] = job.get("error_code")
                row["error_message"] = job.get("error_message")
                if str(job.get("stage")) == "COMPLETED":
                    row["status"] = "completed"
                else:
                    row["status"] = "failed"
                if row["paper_id"]:
                    row["chunks"] = chunk_count(client, str(row["paper_id"]))
            except Exception as exc:  # noqa: BLE001 - one entry must not kill the run
                row["status"] = "failed"
                row["error_code"] = row["error_code"] or type(exc).__name__
                row["error_message"] = f"{type(exc).__name__}: {exc}"
            row["elapsed_s"] = round(time.monotonic() - started, 2)
            items.append(row)
            state = row["status"]
            detail = row["error_code"] or row["stage"] or ""
            print(f"{prefix} {state} ({row['elapsed_s']}s) {detail}".rstrip())

    finished_at = _now()
    report: dict[str, Any] = {
        "started_at": started_at,
        "finished_at": finished_at,
        "base_url": base_url,
        "total": len(entries),
        "completed": sum(1 for item in items if item["status"] == "completed"),
        "failed": sum(1 for item in items if item["status"] == "failed"),
        "skipped": len(skipped),
        "items": items + skipped,
    }

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(format_console(report))
    print(f"\nreport: {out_path}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
