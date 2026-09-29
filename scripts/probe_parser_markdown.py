"""Render the pypdf path as the shared markdown dialect, and diff it with T0.

Read-only by construction: it opens PDFs from the filesystem, runs
``extract_pages`` + ``app.parsing.layout.prepare_pages`` +
``app.parsing.markdown.render_markdown`` and writes one markdown file plus one
JSON report per run. No database, no object storage, no index, no network.

Two jobs:

* **T5 acceptance** -- the two synthetic two-column pages
  (``logs/eval/two-column/``) must come out *left column then right column*; the
  ``column-major`` one must keep its order, the ``interleaved`` one must stop
  fusing both columns onto one line. Both are printed as a before/after.
* **T8 input** -- per paper: page markers, heading levels, characters, fallback
  tables and ``degraded_reason``, next to the pre-change ``sample_lines`` kept in
  ``logs/eval/docling/baseline*.json``.

Usage::

    uv run python scripts/probe_parser_markdown.py logs/eval/docling/corpus/*.pdf \
        --baseline logs/eval/docling/baseline.json --out logs/eval/docling/probe-t5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.logging import get_logger  # noqa: E402
from app.parsing import layout  # noqa: E402
from app.parsing.markdown import (  # noqa: E402
    TABLE_FALLBACK_MARKER,
    render_markdown,
)
from app.parsing.pdf import extract_pages  # noqa: E402

logger = get_logger(__name__)

SAMPLE_LINES = 6


def _first_body_page(page_count: int) -> int:
    """The page a two-column paper's body starts on (page 2 when there is one)."""
    return 2 if page_count >= 2 else 1


def probe(path: Path, *, page_break: str) -> dict[str, Any]:
    """Parse one PDF with the fallback path and describe the markdown."""
    data = path.read_bytes()
    started = time.perf_counter()
    pages = extract_pages(data)
    extract_seconds = time.perf_counter() - started

    started = time.perf_counter()
    prepared, report, furniture = layout.prepare_pages(data, pages)
    layout_seconds = time.perf_counter() - started

    bundle = render_markdown(
        pages,
        pdf_bytes=data,
        page_break=page_break,
        timings={
            "extract_pages_s": round(extract_seconds, 3),
            "layout_s": round(layout_seconds, 3),
        },
    )

    index = _first_body_page(len(pages))
    before = pages[index - 1].text.split("\n") if pages else []
    after = prepared[index - 1].text.split("\n") if prepared else []
    return {
        "pdf": path.name,
        "pages": len(pages),
        "page_markers": bundle.markdown.count(page_break.strip()),
        "headings": bundle.headings,
        "chars": len(bundle.markdown),
        "table_markers": bundle.markdown.count(TABLE_FALLBACK_MARKER),
        "degraded_reason": bundle.degraded_reason,
        "columns": report.columns,
        "reordered_pages": report.reordered_pages,
        "unverified_pages": report.unverified_pages,
        "dropped_furniture": furniture.dropped,
        "dropped_page_numbers": furniture.page_numbers,
        "timings": bundle.timings,
        "sample_page": index,
        "sample_lines_before": before[:SAMPLE_LINES],
        "sample_lines_after": [line for line in after if line.strip()][:SAMPLE_LINES],
        "markdown_head": "\n".join(line for line in bundle.markdown.split("\n")[:12] if line.strip()),
        "_markdown": bundle.markdown,
    }


def _compare_with_baseline(report: dict[str, Any], baseline: dict[str, Any]) -> str:
    """One line saying how many of the baseline sample lines changed."""
    entry = next(
        (item for item in baseline.get("reports", []) if item.get("pdf") == report["pdf"]),
        None,
    )
    if entry is None:
        return "no baseline entry"
    before = [line.strip() for line in entry.get("sample_lines", [])]
    after = [line.strip() for line in report["sample_lines_after"]]
    same = len(set(before) & set(after))
    return f"{len(before) - same}/{len(before)} baseline sample lines changed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", help="PDF files to render (read-only)")
    parser.add_argument("--out", help="directory for the markdown + report.json")
    parser.add_argument("--baseline", help="a baseline JSON from scripts/probe_parser_baseline.py")
    parser.add_argument("--page-break", default="<!-- page-break -->")
    parser.add_argument("--dump", action="store_true", help="write the markdown files")
    args = parser.parse_args()

    baseline = None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))

    reports: list[dict[str, Any]] = []
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    for name in args.pdfs:
        path = Path(name)
        report = probe(path, page_break=args.page_break)
        markdown = report.pop("_markdown")
        if out_dir:
            (out_dir / f"{path.stem}.md").write_text(markdown, encoding="utf-8")
        reports.append(report)
        print(
            f"{report['pdf']:<24} pages={report['pages']:<3} "
            f"markers={report['page_markers']:<3} headings={len(report['headings']):<3} "
            f"chars={report['chars']:<7} tables={report['table_markers']:<2} "
            f"columns={report['columns']} reordered={report['reordered_pages']} "
            f"unverified={report['unverified_pages']}"
        )
        print(f"    degraded_reason = {report['degraded_reason']}")
        print(f"    dropped furniture = {report['dropped_furniture']} numbers={report['dropped_page_numbers']}")
        if baseline is not None:
            print(f"    baseline: {_compare_with_baseline(report, baseline)}")
        if args.dump:
            for line in report["sample_lines_after"]:
                print(f"      | {line}")

    if out_dir:
        (out_dir / "report.json").write_text(
            json.dumps(
                {
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "backend": "pypdf",
                    "reports": reports,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
