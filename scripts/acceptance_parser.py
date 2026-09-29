#!/usr/bin/env python3
"""Accept both parser backends on real papers (plan T8).

Read-only by construction, with one opt-in exception: the *cache probe* in
``--paper-id`` mode parses a real paper through ``parse_paper_file`` so the T7.1
parse-artifact cache can be timed cold vs warm. That writes the paper's own
``papers/<id>/extracted/parsed/`` objects -- always reported, and removed again
with ``--cleanup``. Nothing else is written to MinIO, PostgreSQL or OpenSearch;
paper rows, chunks and index documents are never touched.

For every input it runs the docling backend and the pypdf fallback over the same
bytes, writes both markdown files plus a line diff, and prints a comparison
table. The point is human judgement (plan T8 step 3) on:

* page markers (both sides must equal pages - 1),
* heading levels (docling: real hierarchy; pypdf: the old rule),
* tables (docling: compact pipe tables; pypdf: a fallback marker),
* formulas (docling: ``$$`` LaTeX blocks; pypdf: text),
* two-column reading order (continuous lines must come from one column),
* cold vs warm parse seconds (the T7.1 cache effect).

Usage::

    uv run python scripts/acceptance_parser.py --pdf logs/eval/docling/corpus/*.pdf
    uv run python scripts/acceptance_parser.py --pdf smoke_sample.pdf \
        --out logs/eval/docling/acceptance-manual
    uv run python scripts/acceptance_parser.py --paper-id <uuid> --cleanup
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.core.logging import get_logger  # noqa: E402
from app.parsing.markdown import PAGE_BREAK_DEFAULT, TABLE_FALLBACK_MARKER  # noqa: E402
from app.services import consistency_service, object_storage, parser_service  # noqa: E402

logger = get_logger(__name__)

BACKENDS = ("docling", "pypdf")
PARSE_PREFIX = "papers/{paper_id}/extracted/parsed/"
DEFAULT_OUT_ROOT = Path("logs/eval/docling")
DOCLING_CONTAINER = "paperbox-docling"

# --------------------------------------------------------------------------- #
# pure helpers (unit-tested in tests/test_acceptance_parser.py)
# --------------------------------------------------------------------------- #


def table_blocks(markdown: str) -> int:
    """Count markdown tables: one per run of consecutive ``|``-prefixed lines."""
    blocks = 0
    inside = False
    for line in markdown.split("\n"):
        row = line.strip().startswith("|")
        if row and not inside:
            blocks += 1
        inside = row
    return blocks


def formula_blocks(markdown: str) -> int:
    """Count ``$$``-delimited LaTeX blocks (docling writes them inline)."""
    return markdown.count("$$") // 2


def describe_markdown(markdown: str, *, pages: int, page_break: str) -> dict[str, Any]:
    """The numbers the acceptance table and the report need for one markdown."""
    marker = page_break.strip()
    headings = [
        len(line) - len(line.lstrip("#"))
        for line in markdown.split("\n")
        if line.startswith("#")
    ]
    markers = markdown.count(marker) if marker else 0
    return {
        "pages": pages,
        "chars": len(markdown),
        "lines": markdown.count("\n") + 1 if markdown else 0,
        "headings": len(headings),
        # A heading level > 6 would be a docling export bug (plan decision 12).
        "max_heading_level": max(headings) if headings else 0,
        "tables": table_blocks(markdown),
        "table_marks": markdown.count(TABLE_FALLBACK_MARKER),
        "formulas": formula_blocks(markdown),
        "page_markers": markers,
        "page_markers_expected": max(pages - 1, 0),
        "page_markers_ok": markers == max(pages - 1, 0),
    }


def summarize_diff(
    left: str, right: str, *, name_left: str, name_right: str
) -> tuple[str, dict[str, int]]:
    """Line diff plus its size, for the report (full text is written to disk)."""
    lines = list(
        difflib.unified_diff(
            left.split("\n"),
            right.split("\n"),
            fromfile=name_left,
            tofile=name_right,
            lineterm="",
            n=2,
        )
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return "\n".join(lines), {"diff_lines": len(lines), "added": added, "removed": removed}


def parse_mem_usage(text: str) -> float | None:
    """``"2.06GiB / 8GiB"`` (``docker stats``) -> bytes, or ``None``."""
    first = text.split("/")[0].strip()
    for unit, factor in (
        ("GiB", 1024**3),
        ("MiB", 1024**2),
        ("KiB", 1024),
        ("GB", 1000**3),
        ("MB", 1000**2),
        ("kB", 1000),
        ("B", 1),
    ):
        if first.endswith(unit):
            try:
                return float(first[: -len(unit)]) * factor
            except ValueError:
                return None
    return None


def human_bytes(value: float | None) -> str:
    if not value:
        return "-"
    return f"{value / 1024**3:.2f}GiB"


# --------------------------------------------------------------------------- #
# container memory sampler
# --------------------------------------------------------------------------- #


class MemWatch:
    """Poll ``docker stats`` and keep the peak of one container.

    The docling container may live in WSL (``--mem-ssh`` unset) or on another
    host -- the NAS deployment is reached over SSH, e.g.
    ``--mem-ssh fishsky@192.168.31.53:65422``.
    """

    def __init__(
        self, container: str = DOCLING_CONTAINER, interval: float = 3.0, ssh: str | None = None
    ) -> None:
        self.container = container
        self.interval = interval
        self.ssh = ssh
        self.samples: list[float] = []
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _command(self) -> list[str]:
        stats = "docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}'"
        if not self.ssh:
            return ["wsl.exe", "--", "docker", "stats", "--no-stream", "--format",
                    "{{.Name}}\t{{.MemUsage}}"]
        target, _, port = self.ssh.partition(":")
        cmd = ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes"]
        if port:
            cmd += ["-p", port]
        return cmd + [target, stats]

    def _sample_once(self) -> None:
        cmd = self._command()
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        except (OSError, subprocess.SubprocessError) as exc:  # docker/WSL missing
            self.error = f"{type(exc).__name__}: {exc}"
            self._stop.set()
            return
        if done.returncode != 0:
            self.error = (done.stderr or done.stdout).strip()[:200]
            self._stop.set()
            return
        for line in done.stdout.splitlines():
            name, _, usage = line.partition("\t")
            if self.container in name:
                value = parse_mem_usage(usage)
                if value:
                    self.samples.append(value)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(self.interval)

    def start(self) -> "MemWatch":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    @property
    def peak(self) -> float | None:
        return max(self.samples) if self.samples else None


# --------------------------------------------------------------------------- #
# one parse, one report row
# --------------------------------------------------------------------------- #


@dataclass
class Run:
    """Everything the acceptance reports about one (input, backend) pair."""

    input_name: str
    #: the backend we asked for; ``actual_backend`` says who answered
    backend: str
    seconds: float
    stats: dict[str, Any]
    actual_backend: str = ""
    cache_hit: bool = False
    degraded_reason: str | None = None
    parser_version: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    markdown: str = ""
    markdown_path: str = ""
    diff_path: str = ""
    diff: dict[str, int] = field(default_factory=dict)
    degradations: list[dict[str, Any]] = field(default_factory=list)
    cache_first_s: float | None = None
    cache_second_s: float | None = None
    error: str | None = None

    @property
    def fell_back(self) -> bool:
        """True when docling was asked for and pypdf answered instead."""
        return self.backend == "docling" and self.actual_backend == "pypdf"

    def as_dict(self) -> dict[str, Any]:
        return {
            "input": self.input_name,
            "backend": self.backend,
            "actual_backend": self.actual_backend or self.backend,
            "seconds": round(self.seconds, 2),
            "stats": self.stats,
            "cache_hit": self.cache_hit,
            "cache_first_s": self.cache_first_s,
            "cache_second_s": self.cache_second_s,
            "degraded_reason": self.degraded_reason,
            "parser_version": self.parser_version,
            "timings": self.timings,
            "diff": self.diff,
            "degradations": self.degradations,
            "markdown_path": self.markdown_path,
            "diff_path": self.diff_path,
            "error": self.error,
        }


def _sink_into(events: list[dict[str, Any]]):
    def sink(stage: str, code: str, detail: dict[str, Any]) -> None:
        events.append({"stage": stage, "code": code, "detail": detail})

    return sink


def parse_backend(
    data: bytes,
    *,
    filename: str,
    backend: str,
    page_break: str,
    page_range: str | None = None,
    paper_id: str | None = None,
    cache_probe: bool = False,
    label: str | None = None,
) -> Run:
    """Parse once with one backend and describe the result."""
    events: list[dict[str, Any]] = []
    sink = _sink_into(events)
    first_seconds: float | None = None
    second_seconds: float | None = None

    started = time.perf_counter()
    try:
        if paper_id and cache_probe:
            bundle = parser_service.parse_paper_file(
                paper_id,
                data,
                filename=filename,
                backend=backend,
                page_range=page_range,
                on_degrade=sink,
            )
            first_seconds = time.perf_counter() - started
            started = time.perf_counter()
            replayed = parser_service.parse_paper_file(
                paper_id,
                data,
                filename=filename,
                backend=backend,
                page_range=page_range,
                on_degrade=sink,
            )
            second_seconds = time.perf_counter() - started
            seconds = first_seconds if not bundle.cache_hit else second_seconds
            bundle = replayed if not bundle.cache_hit else bundle
        else:
            bundle = parser_service.parse_pdf(
                data,
                filename=filename,
                backend=backend,
                page_range=page_range,
                on_degrade=sink,
            )
            seconds = time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001 - reported, never fatal for the batch
        return Run(
            input_name=label or filename,
            backend=backend,
            seconds=time.perf_counter() - started,
            stats={},
            error=f"{type(exc).__name__}: {exc}",
            degradations=events,
        )

    stats = describe_markdown(bundle.markdown, pages=bundle.page_count, page_break=page_break)
    return Run(
        input_name=label or filename,
        backend=backend,
        actual_backend=bundle.backend,
        seconds=seconds,
        stats=stats,
        cache_hit=bundle.cache_hit,
        degraded_reason=bundle.degraded_reason,
        parser_version=bundle.parser_version,
        timings=bundle.timings,
        markdown=bundle.markdown,
        degradations=events,
        cache_first_s=first_seconds,
        cache_second_s=second_seconds,
    )


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #


@dataclass
class Input:
    """One thing to parse: a file on disk, or a library paper's original PDF."""

    name: str
    filename: str
    paper_id: str | None = None
    path: Path | None = None

    def read(self) -> bytes:
        if self.path is not None:
            return self.path.read_bytes()
        record = _paper_file(self.paper_id or "")
        return object_storage.download_bytes(record)


def _paper_file(paper_id: str) -> str:
    """The stored primary PDF's object key of one live paper (read-only)."""
    from app.db.models import Paper
    from app.db.session import SessionLocal
    from app.services import paper_service

    with SessionLocal() as session:
        paper = session.get(Paper, paper_id)
        if paper is None or paper.deleted_at is not None:
            raise LookupError(f"paper {paper_id} not found (or deleted)")
        record = paper_service.original_file(paper)
        if record is None:
            raise LookupError(f"paper {paper_id} has no stored PDF")
        return record.object_key


def _paper_label(paper_id: str) -> str:
    from app.db.models import Paper
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        paper = session.get(Paper, paper_id)
        return f"{paper_id[:8]} {paper.title[:40]}" if paper is not None else paper_id


def artifacts_under(paper_id: str) -> set[str]:
    """Keys under one paper's parse-artifact prefix (used to spot our writes)."""
    prefix = PARSE_PREFIX.format(paper_id=paper_id)
    return {obj.object_name for obj in object_storage.list_objects(prefix=prefix)}


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


HEADER = (
    f"{'input':<26} {'pages':>5} {'backend':<7} {'chars':>7} {'head':>5} "
    f"{'tables':>6} {'tmarks':>6} {'pmark':>5} {'formulas':>8} {'seconds':>8}"
)


def print_row(run: Run) -> None:
    if run.error:
        print(f"{run.input_name:<26} {run.backend:<7} ERROR {run.error}")
        return
    s = run.stats
    marker = "" if s["page_markers_ok"] else f" (expected {s['page_markers_expected']})"
    label = f"{run.backend}*" if run.fell_back else run.backend
    print(
        f"{run.input_name:<26} {s['pages']:>5} {label:<7} {s['chars']:>7} "
        f"{s['headings']:>5} {s['tables']:>6} {s['table_marks']:>6} "
        f"{str(s['page_markers']) + marker:>5} {s['formulas']:>8} {run.seconds:>8.2f}"
    )


def consistency_totals() -> dict[str, Any] | None:
    """Three-way store totals, or ``None`` when the check itself is unavailable."""
    try:
        return consistency_service.check_consistency().as_dict()["totals"]
    except Exception as exc:  # noqa: BLE001 - a broken store must not stop T8
        print(f"[WARN] consistency check unavailable: {type(exc).__name__}: {exc}")
        return None


def totals_delta(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> dict[str, Any]:
    if not before or not after:
        return {}
    return {
        key: (before.get(key), after.get(key))
        for key in before
        if before[key] != after.get(key)
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--pdf", nargs="+", help="PDF files to parse (read-only, no MinIO/PG writes)"
    )
    parser.add_argument(
        "--paper-id", nargs="+", help="library papers to parse (original PDF, read-only)"
    )
    parser.add_argument(
        "--out",
        help=f"output directory (default: {DEFAULT_OUT_ROOT}/acceptance-<stamp>)",
    )
    parser.add_argument("--page-range", help="passed through to docling (e.g. 1-3)")
    parser.add_argument(
        "--page-break", default=settings.docling_page_break or PAGE_BREAK_DEFAULT
    )
    parser.add_argument(
        "--cleanup", action="store_true", help="delete the MinIO artifacts this run wrote"
    )
    parser.add_argument(
        "--cache-probe",
        dest="cache_probe",
        action="store_true",
        default=None,
        help="time parse_paper_file twice (default: on for --paper-id)",
    )
    parser.add_argument("--no-cache-probe", dest="cache_probe", action="store_false")
    parser.add_argument(
        "--mem-ssh",
        default=os.environ.get("DOCLING_MEM_SSH"),
        help="user@host[:port] of the docker host running docling (default: local WSL; "
        "the NAS deployment needs fishsky@<host>:65422)",
    )
    parser.add_argument(
        "--no-consistency", action="store_true", help="skip the before/after store totals"
    )
    parser.add_argument(
        "--backends", nargs="+", default=list(BACKENDS), choices=list(BACKENDS)
    )
    args = parser.parse_args()

    if not args.pdf and not args.paper_id:
        parser.error("give at least one --pdf or --paper-id")

    cache_probe = args.cache_probe if args.cache_probe is not None else bool(args.paper_id)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else DEFAULT_OUT_ROOT / f"acceptance-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs: list[Input] = []
    for name in args.pdf or []:
        path = Path(name)
        if not path.is_file():
            print(f"[FAIL] no such file: {path}")
            return 2
        inputs.append(Input(name=path.stem, filename=path.name, path=path))
    for paper_id in args.paper_id or []:
        try:
            inputs.append(
                Input(
                    name=_paper_label(paper_id),
                    filename=f"{paper_id}.pdf",
                    paper_id=paper_id,
                )
            )
        except LookupError as exc:
            print(f"[FAIL] {exc}")
            return 2

    print(
        f"docling: {settings.docling_url} (timeout {settings.docling_timeout:.0f}s, "
        f"document {settings.docling_document_timeout:.0f}s, formulas "
        f"{settings.docling_formula_enrichment})\n"
        f"pypdf  : fallback path, same markdown dialect\n"
        f"out    : {out_dir}\n"
    )

    before = None if args.no_consistency else consistency_totals()
    staged_before = {i.paper_id: artifacts_under(i.paper_id) for i in inputs if i.paper_id}

    watch = MemWatch(ssh=args.mem_ssh).start() if "docling" in args.backends else None
    runs: list[Run] = []
    for item in inputs:
        try:
            data = item.read()
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] cannot read {item.name}: {type(exc).__name__}: {exc}")
            continue
        print(f"-- {item.name} ({len(data) / 1024:.0f} KiB)")
        per_backend: dict[str, Run] = {}
        for backend in args.backends:
            run = parse_backend(
                data,
                label=item.name,
                filename=item.filename,
                backend=backend,
                page_break=args.page_break,
                page_range=args.page_range,
                paper_id=item.paper_id,
                cache_probe=cache_probe,
            )
            per_backend[backend] = run
            runs.append(run)
            print_row(run)
            if run.fell_back:
                print(f"    degraded_reason = {run.degraded_reason}")
            if run.degradations:
                pairs = ", ".join(
                    sorted({f"{d['stage']}/{d['code']}" for d in run.degradations})
                )
                print(f"    ledger = {pairs}")
            if run.cache_first_s is not None:
                hit = "hit" if run.cache_hit else "miss"
                print(
                    f"    cache  = first {run.cache_first_s:.2f}s / second "
                    f"{run.cache_second_s:.2f}s ({hit})"
                )
        if "docling" in per_backend and "pypdf" in per_backend:
            left, right = per_backend["docling"], per_backend["pypdf"]
            if left.fell_back:
                print("    [WARN] docling fell back to pypdf -> markdown comparison skipped")
            elif left.markdown or right.markdown:
                text, summary = summarize_diff(
                    left.markdown,
                    right.markdown,
                    name_left=f"{item.name}.docling.md",
                    name_right=f"{item.name}.pypdf.md",
                )
                for run in (left, right):
                    run.diff = summary
                (out_dir / f"{item.name}.diff.txt").write_text(text, encoding="utf-8")
                left.diff_path = right.diff_path = f"{item.name}.diff.txt"
                print(
                    f"    diff   = +{summary['added']} / -{summary['removed']} lines "
                    f"-> {item.name}.diff.txt"
                )

    for run in runs:
        if run.markdown:
            name = f"{run.input_name}.{run.backend}.md"
            (out_dir / name).write_text(run.markdown, encoding="utf-8")
            run.markdown_path = name

    if watch is not None:
        watch.stop()
        if watch.error:
            print(f"\n[WARN] container memory not sampled: {watch.error}")
        else:
            print(
                f"\ndocling container peak = {human_bytes(watch.peak)} "
                f"({len(watch.samples)} samples)"
            )

    staged_after = {i.paper_id: artifacts_under(i.paper_id) for i in inputs if i.paper_id}
    written: dict[str, list[str]] = {
        paper_id: sorted(staged_after[paper_id] - staged_before.get(paper_id, set()))
        for paper_id in staged_after
    }
    for paper_id, keys in written.items():
        if keys:
            print(f"artifacts written for {paper_id[:8]}: {len(keys)} object(s)")
            for key in keys:
                print(f"    {key}")

    after = None if args.no_consistency else consistency_totals()
    delta = totals_delta(before, after)
    if before and after:
        verdict = "OK" if not delta else "FAIL"
        tail = f": {delta}" if delta else f" ({after})"
        print(f"\n[{verdict}] store totals unchanged{tail}")

    if args.cleanup:
        removed = 0
        for paper_id, keys in written.items():
            for key in keys:
                object_storage.delete_object(key)
                removed += 1
        print(f"cleanup: removed {removed} object(s) written by this run")
    elif any(written.values()):
        print("note: artifacts kept (re-run with --cleanup to remove them)")

    failures = [r for r in runs if r.error]
    degraded = [r for r in runs if r.fell_back]
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "docling_url": settings.docling_url,
        "parser_backend_setting": settings.parser_backend,
        "page_break": args.page_break,
        "out_dir": str(out_dir),
        "runs": [r.as_dict() for r in runs],
        "docling_mem_peak_bytes": watch.peak if watch else None,
        "docling_mem_samples": watch.samples if watch else [],
        "mem_watch_error": watch.error if watch else "not started",
        "written_artifacts": written,
        "consistency_before": before,
        "consistency_after": after,
        "consistency_delta": delta,
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nreport -> {out_dir / 'report.json'}")
    print(
        f"runs={len(runs)} failures={len(failures)} docling_fallbacks={len(degraded)} "
        f"marker_mismatch={sum(1 for r in runs if r.stats and not r.stats['page_markers_ok'])}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
