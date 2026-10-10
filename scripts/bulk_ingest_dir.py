#!/usr/bin/env python3
"""Import a folder of PDFs into paperbox (2026-09-19, plan section 3).

Two modes, chosen by where the PDFs live:

* **same machine** (default) -- the folder is already on the server, so nothing
  needs to be transferred: ``POST /api/papers/ingest/dir`` hands the server the
  root, and it walks, hashes and queues everything itself. This is the mode to
  use for a 1000-file dump: it is minutes of hashing, not hours of upload.
* **remote** (``--via-http``) -- the folder is on *this* machine and the server
  is elsewhere: each PDF is POSTed to ``/api/papers/ingest/files`` one file per
  request (the parts of a multipart body arrive serially, so parallel uploads
  only come from parallel requests, and the script keeps it to one at a time),
  honouring ``429 + Retry-After`` by waiting and retrying.

Both modes then poll ``GET /api/jobs/{id}`` until every job is terminal and write
a JSON report (``--out``) with per-file outcomes. ``--resume`` reads that report
back and skips the files that already succeeded.

    uv run python scripts/bulk_ingest_dir.py --root D:/papers --dry-run
    uv run python scripts/bulk_ingest_dir.py --root D:/papers --limit 50
    uv run python scripts/bulk_ingest_dir.py --root ./local --via-http --resume

Client-side filtering is deliberately thin -- ``.pdf`` by extension and the size
ceiling -- because the server enforces the real limits anyway (``INGEST_MAX_FILE_MB``);
its job is to avoid sending bytes that are certain to be refused.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
DEFAULT_GLOB = "**/*.pdf"
DEFAULT_OUT = ROOT / "evals" / "ingest-dir-report.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8077"
DEFAULT_TIMEOUT = 900.0
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_RETRY_AFTER = 2.0
DEFAULT_MAX_ATTEMPTS = 6
BACKOFF_CAP = 60.0
REQUEST_TIMEOUT = 300.0
TERMINAL_STAGES = {"COMPLETED", "FAILED"}
TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".swp", "~")
PREVIEW = 5


# --------------------------------------------------------------------------- #
# pure helpers (unit-tested without HTTP or a filesystem tree)
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


def is_candidate(name: str) -> bool:
    """Client-side prescreen: ``.pdf`` and not a hidden/temporary file."""
    lowered = name.lower()
    if lowered.startswith("."):
        return False
    if any(lowered.endswith(suffix) for suffix in TEMP_SUFFIXES):
        return False
    return lowered.endswith(".pdf")


def matches_glob(relative: str, pattern: str) -> bool:
    """Glob match where ``**`` stands for zero or more path segments."""
    def _match(parts: tuple[str, ...], pat: tuple[str, ...]) -> bool:
        if not pat:
            return not parts
        head, rest = pat[0], pat[1:]
        if head == "**":
            return _match(parts, rest) or (bool(parts) and _match(parts[1:], pat))
        if not parts:
            return False
        # 与 app.services.local_scan 保持同一口径：显式大小写不敏感。
        if not fnmatchcase(parts[0].casefold(), head.casefold()):
            return False
        return _match(parts[1:], rest)

    return _match(PurePosixPath(relative).parts, PurePosixPath(pattern).parts)


def collect_files(
    root: str | os.PathLike[str],
    *,
    glob: str = DEFAULT_GLOB,
    recursive: bool = True,
    limit: int | None = None,
) -> list[Path]:
    """Every PDF under ``root`` matching ``glob`` (sorted, links not followed)."""
    base = Path(root)
    found: list[Path] = []
    if recursive:
        iterator = (
            path
            for path in base.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    else:
        iterator = (path for path in base.iterdir() if path.is_file() and not path.is_symlink())
    for path in sorted(iterator):
        relative = path.relative_to(base).as_posix()
        if not is_candidate(path.name) or not matches_glob(relative, glob):
            continue
        found.append(path)
        if limit is not None and len(found) >= limit:
            break
    return found


def build_manifest(
    paths: Iterable[Path],
    root: str | os.PathLike[str],
    *,
    max_bytes: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split paths into ``(candidates, skipped)`` with the size prescreen applied.

    Every entry carries the relative path (what the report and ``--resume`` key
    on), the absolute path and the size; ``skipped`` entries also carry why.
    """
    base = Path(root)
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for path in paths:
        try:
            relative = path.relative_to(base).as_posix()
        except ValueError:  # pragma: no cover - paths come from the same walk
            relative = path.name
        try:
            size = path.stat().st_size
        except OSError as exc:
            skipped.append(
                {
                    "path": str(path),
                    "relative": relative,
                    "status": "skipped",
                    "reason": f"unreadable: {exc}",
                }
            )
            continue
        entry = {"path": str(path), "relative": relative, "size_bytes": size}
        if size == 0:
            skipped.append({**entry, "status": "skipped", "reason": "empty file"})
            continue
        if max_bytes is not None and size > max_bytes:
            skipped.append(
                {
                    **entry,
                    "status": "skipped",
                    "reason": f"larger than the {max_bytes} byte limit",
                }
            )
            continue
        candidates.append(entry)
    return candidates, skipped


def retry_delay(
    attempt: int,
    *,
    retry_after: float | None = None,
    base: float = DEFAULT_RETRY_AFTER,
    cap: float = BACKOFF_CAP,
    jitter: float = 0.25,
) -> float:
    """How long to wait before retrying a throttled request.

    The server's ``Retry-After`` wins when present (that is the whole point of
    sending it); otherwise it is exponential backoff with a little jitter so
    several clients do not resume in lockstep. ``attempt`` is 1-based.
    """
    if retry_after is not None and retry_after >= 0:
        return float(min(retry_after, cap))
    step = base * (2 ** max(0, attempt - 1))
    return float(min(step, cap) * (1.0 + random.uniform(0.0, jitter)))


def parse_retry_after(value: str | None) -> float | None:
    """Read a ``Retry-After`` header (seconds form only; dates are ignored)."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def summarize(items: list[dict[str, Any]], *, skipped: int = 0) -> dict[str, int]:
    """Count outcomes the way the report and the exit code need them."""
    return {
        "total": len(items) + skipped,
        "accepted": sum(1 for item in items if item.get("status") == "accepted"),
        "duplicate": sum(1 for item in items if item.get("status") == "duplicate"),
        "rejected": sum(1 for item in items if item.get("status") == "rejected"),
        "completed": sum(1 for item in items if item.get("stage") == "COMPLETED"),
        "failed": sum(1 for item in items if item.get("stage") == "FAILED"),
        "skipped": skipped,
    }


def completed_paths(report: dict[str, Any] | None) -> set[str]:
    """Paths an earlier run already finished (used by ``--resume``)."""
    if not report:
        return set()
    done: set[str] = set()
    for item in report.get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("status") == "skipped":
            continue
        if item.get("stage") == "COMPLETED" or item.get("status") in {"duplicate"}:
            key = item.get("relative") or item.get("path")
            if key:
                done.add(str(key))
    return done


def format_console(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "",
        f"mode {report['mode']}  root {report['root']}",
        f"total {counts['total']}  accepted {counts['accepted']}  "
        f"duplicate {counts['duplicate']}  rejected {counts['rejected']}  "
        f"skipped {counts['skipped']}",
        f"jobs completed {counts['completed']}  failed {counts['failed']}",
    ]
    failures = [item for item in report["items"] if item.get("stage") == "FAILED"]
    if failures:
        grouped: dict[str, int] = {}
        for item in failures:
            key = item.get("error_code") or "UNKNOWN"
            grouped[key] = grouped.get(key, 0) + 1
        lines.append("failures by error_code:")
        for code, count in sorted(grouped.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {code:<24} {count}")
        for item in failures[:PREVIEW]:
            lines.append(
                f"  {item.get('relative')}  {item.get('error_code') or '-'}  "
                f"{(item.get('error_message') or '')[:100]}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def poll_job(
    client: httpx.Client, job_id: str, *, timeout: float, poll_interval: float
) -> dict[str, Any]:
    """Poll one job until it is terminal or the deadline passes."""
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
            raise TimeoutError(f"job {job_id} still {stage or 'unknown'} after {timeout:.0f}s")
        time.sleep(poll_interval)


def post_with_backoff(
    client: httpx.Client,
    url: str,
    *,
    files: dict | None = None,
    json_body: dict | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> httpx.Response:
    """POST, waiting out ``429`` responses the way the API documents."""
    attempt = 0
    while True:
        attempt += 1
        if files is not None:
            response = client.post(url, files=files)
        else:
            response = client.post(url, json=json_body)
        if response.status_code != 429 or attempt >= max_attempts:
            return response
        wait = retry_delay(attempt, retry_after=parse_retry_after(response.headers.get("Retry-After")))
        print(f"    429 from {url}: waiting {wait:.1f}s (attempt {attempt}/{max_attempts})")
        time.sleep(wait)


def job_item(entry: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Merge a polled job into the manifest entry."""
    return {
        **entry,
        "status": "accepted",
        "job_id": job.get("job_id"),
        "paper_id": job.get("paper_id"),
        "stage": job.get("stage"),
        "duplicate": bool(job.get("duplicate")),
        "error_code": job.get("error_code"),
        "error_message": job.get("error_message"),
    }


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #
def run_server_side(
    client: httpx.Client,
    manifest: list[dict[str, Any]],
    *,
    root: str,
    glob: str,
    recursive: bool,
    limit: int,
    timeout: float,
    poll_interval: float,
) -> list[dict[str, Any]]:
    """``POST /api/papers/ingest/dir`` once, then poll the jobs it created.

    ``limit`` is what the client already considered (candidates + skipped +
    files an earlier run finished), so the server scans the same set instead of
    an arbitrary prefix of it.
    """
    response = post_with_backoff(
        client,
        "/api/papers/ingest/dir",
        json_body={
            "root": root,
            "glob": glob,
            "recursive": recursive,
            "limit": max(1, limit),
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(f"ingest/dir failed with HTTP {response.status_code}: {response.text[:200]}")
    body = response.json()
    by_relative = {entry["relative"]: entry for entry in manifest}

    items: list[dict[str, Any]] = []
    for result in body.get("jobs") or []:
        key = result.get("relative") or result.get("filename")
        entry = by_relative.get(key, {"path": result.get("path"), "relative": key})
        status = result.get("status")
        item = {
            **entry,
            "status": status,
            "job_id": result.get("job_id"),
            "paper_id": result.get("paper_id"),
            "error_code": result.get("error_code"),
            "error_message": result.get("error_message"),
        }
        if status == "accepted" and result.get("job_id"):
            item["stage"] = poll_job(
                client, str(result["job_id"]), timeout=timeout, poll_interval=poll_interval
            ).get("stage")
        items.append(item)
    return items


def run_client_side(
    client: httpx.Client,
    manifest: list[dict[str, Any]],
    *,
    timeout: float,
    poll_interval: float,
) -> list[dict[str, Any]]:
    """Upload one file per request and poll the job it created."""
    items: list[dict[str, Any]] = []
    total = len(manifest)
    for index, entry in enumerate(manifest, start=1):
        path = Path(entry["path"])
        started = time.monotonic()
        try:
            with path.open("rb") as handle:
                response = post_with_backoff(
                    client,
                    "/api/papers/ingest/files",
                    files={"files": (path.name, handle, "application/pdf")},
                )
        except OSError as exc:
            items.append(
                {**entry, "status": "rejected", "error_code": "UNREADABLE", "error_message": str(exc)}
            )
            continue
        if response.status_code == 413:
            items.append({**entry, "status": "rejected", "error_code": "OVERSIZED"})
            continue
        if response.status_code >= 400:
            items.append(
                {
                    **entry,
                    "status": "rejected",
                    "error_code": f"HTTP_{response.status_code}",
                    "error_message": response.text[:200],
                }
            )
            continue
        results = (response.json().get("results") or [{}])[0]
        item = {
            **entry,
            "status": results.get("status") or "rejected",
            "job_id": results.get("job_id"),
            "paper_id": results.get("paper_id"),
            "error_code": results.get("error_code"),
            "error_message": results.get("error_message"),
        }
        if item["status"] == "accepted" and item["job_id"]:
            item["stage"] = poll_job(
                client, str(item["job_id"]), timeout=timeout, poll_interval=poll_interval
            ).get("stage")
        item["elapsed_s"] = round(time.monotonic() - started, 2)
        items.append(item)
        print(
            f"  [{index}/{total}] {entry['relative']} {item['status']} "
            f"{item.get('stage') or item.get('error_code') or ''}".rstrip()
        )
    return items


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", required=True, help="folder holding the PDFs")
    parser.add_argument("--glob", default=DEFAULT_GLOB, help="pattern relative to root")
    parser.add_argument(
        "--no-recursive", dest="recursive", action="store_false", help="only the top level"
    )
    parser.add_argument("--limit", type=int, default=None, help="import at most N files")
    parser.add_argument(
        "--via-http",
        action="store_true",
        help="upload each file to /ingest/files instead of letting the server read the folder",
    )
    parser.add_argument("--base-url", default=None, help="paperbox base url")
    parser.add_argument("--api-key", default=None, help="bearer token (never printed)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="report JSON path")
    parser.add_argument("--resume", action="store_true", help="skip files an earlier run finished")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, send nothing")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-job poll budget (s)")
    parser.add_argument(
        "--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="poll spacing (s)"
    )
    parser.add_argument(
        "--max-file-mb",
        type=float,
        default=None,
        help="client-side size prescreen (defaults to the server's INGEST_MAX_FILE_MB)",
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
    max_mb = args.max_file_mb
    if max_mb is None:
        try:
            max_mb = float(env.get("INGEST_MAX_FILE_MB") or 100)
        except ValueError:
            max_mb = 100.0
    max_bytes = int(max_mb * 1024 * 1024)

    root = Path(args.root)
    if not root.is_dir():
        print(f"root is not a directory: {root}", file=sys.stderr)
        return 2

    paths = collect_files(
        root, glob=args.glob, recursive=args.recursive, limit=args.limit
    )
    manifest, skipped = build_manifest(paths, root, max_bytes=max_bytes)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path

    previous: dict[str, Any] | None = None
    if args.resume and out_path.exists():
        try:
            previous = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
    done = completed_paths(previous)
    if done:
        before = len(manifest)
        manifest = [entry for entry in manifest if entry["relative"] not in done]
        print(f"  resume: {before - len(manifest)} file(s) already done, skipped")

    mode = "http" if args.via_http else "server-side"
    print(f"bulk ingest dir: {len(manifest)} file(s) from {root} ({mode})")
    print(f"  glob {args.glob}  recursive {args.recursive}  max {max_mb:g} MB")
    print(f"  base_url {base_url}  api_key {'set' if api_key else 'missing'}")

    if args.dry_run:
        for entry in manifest[:PREVIEW]:
            print(f"  would import {entry['relative']} ({entry['size_bytes']} bytes)")
        if len(manifest) > PREVIEW:
            print(f"  ... and {len(manifest) - PREVIEW} more")
        for entry in skipped[:PREVIEW]:
            print(f"  would skip   {entry['relative']} ({entry['reason']})")
        return 0

    started_at = _now()
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    items: list[dict[str, Any]] = []
    error: str | None = None
    with httpx.Client(base_url=base_url, headers=headers, timeout=REQUEST_TIMEOUT) as client:
        try:
            if args.via_http:
                items = run_client_side(
                    client,
                    manifest,
                    timeout=args.timeout,
                    poll_interval=args.poll_interval,
                )
            else:
                items = run_server_side(
                    client,
                    manifest,
                    root=str(root),
                    glob=args.glob,
                    recursive=args.recursive,
                    limit=len(manifest) + len(skipped) + len(done),
                    timeout=args.timeout,
                    poll_interval=args.poll_interval,
                )
        except Exception as exc:  # noqa: BLE001 - report what happened, exit non-zero
            error = f"{type(exc).__name__}: {exc}"
            print(f"  ERROR {error}", file=sys.stderr)

    counts = summarize(items, skipped=len(skipped))
    report: dict[str, Any] = {
        "started_at": started_at,
        "finished_at": _now(),
        "mode": mode,
        "root": str(root),
        "glob": args.glob,
        "base_url": base_url,
        "counts": counts,
        "error": error,
        "items": items + skipped,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(format_console(report))
    print(f"\nreport: {out_path}")
    return 0 if error is None and counts["failed"] == 0 and counts["rejected"] == 0 else 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
