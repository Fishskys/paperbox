"""Record what the current pypdf parser produces, before a second backend exists.

Read-only by construction: it opens PDFs from the filesystem, runs the existing
parsing trio (``extract_pages`` / ``detect_sections`` / ``chunk_document``) and
writes one JSON report. No database, no object storage, no index.

The point is to have a *before* picture - section titles, chunk counts, timings
and the first visual lines of a body page - so the docling backend can be
compared against it in the acceptance run (see
``.hermes/plans/2026-09-28_160551-docling-parser-backend.md`` task T0/T8).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.logging import get_logger  # noqa: E402
from app.parsing import chunking  # noqa: E402
from app.parsing.pdf import extract_pages  # noqa: E402
from app.parsing.structure import detect_sections, merge_short_sections  # noqa: E402

logger = get_logger(__name__)

#: How many raw lines of a body page to keep for eyeballing column order.
SAMPLE_LINES = 10


def _body_page_index(page_count: int) -> int:
    """Pick a page that is likely body text (two-column papers start at page 2)."""
    return 2 if page_count >= 2 else 1


def probe(path: Path) -> dict[str, Any]:
    """Parse one PDF with the pypdf path and describe the result."""
    data = path.read_bytes()

    started = time.perf_counter()
    pages = extract_pages(data)
    parse_seconds = time.perf_counter() - started

    started = time.perf_counter()
    sections = merge_short_sections(detect_sections(pages))
    section_seconds = time.perf_counter() - started

    started = time.perf_counter()
    chunks = chunking.chunk_document(pages, sections)
    chunk_seconds = time.perf_counter() - started

    index = _body_page_index(len(pages))
    sample = pages[index - 1].text.split("\n") if pages else []

    return {
        "pdf": path.name,
        "bytes": len(data),
        "pages": len(pages),
        "timings": {
            "extract_pages_s": round(parse_seconds, 3),
            "detect_sections_s": round(section_seconds, 3),
            "chunk_document_s": round(chunk_seconds, 3),
        },
        "sections": [
            {
                "title": section.label,
                "page_start": section.page_start,
                "page_end": section.page_end,
                "paragraphs": len(section.paragraphs),
            }
            for section in sections
        ],
        "chunks": {
            "count": len(chunks),
            "tokens_total": sum(chunk.token_count for chunk in chunks),
            "tokens_max": max((chunk.token_count for chunk in chunks), default=0),
        },
        "sample_page": index,
        "sample_lines": [line for line in sample if line.strip()][:SAMPLE_LINES],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="report path (JSON)")
    args = parser.parse_args()

    out = args.out or Path("logs/eval/docling") / f"baseline-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    reports = []
    for path in args.pdfs:
        report = probe(path)
        reports.append(report)
        logger.info(
            "baseline probe done",
            extra={
                "extra_fields": {
                    "pdf": report["pdf"],
                    "pages": report["pages"],
                    "sections": len(report["sections"]),
                    "chunks": report["chunks"]["count"],
                    "seconds": report["timings"]["extract_pages_s"],
                }
            },
        )
        print(
            f"{report['pdf']:<22} pages={report['pages']:<3} "
            f"sections={len(report['sections']):<3} chunks={report['chunks']['count']:<4} "
            f"tokens={report['chunks']['tokens_total']:<6} "
            f"parse={report['timings']['extract_pages_s']}s"
        )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "backend": "pypdf",
        "pypdf_version": _pypdf_version(),
        "reports": reports,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten to {out}")
    return 0


def _pypdf_version() -> str:
    import pypdf  # noqa: PLC0415 - cheap, and only needed for the report

    return f"pypdf {pypdf.__version__}"


if __name__ == "__main__":
    raise SystemExit(main())
