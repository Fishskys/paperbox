"""Probe what docling-serve's Markdown export looks like for our papers.

Read-only: it sends PDFs to the local docling service and writes the responses
under ``logs/eval/docling/<stamp>/``. No database, no MinIO, no index changes.

The output of this script is the input to task T3 of
``.hermes/plans/2026-09-28_160551-docling-parser-backend.md``: the markdown
dialect gets frozen from what we actually see here, *before* any of the parser
integration code is written.

IMPORTANT — request shape (verified against the running container, 2026-09-28):
``POST /v1/convert/file`` takes **multipart form fields only** (see
``/openapi.json`` -> ``Body_process_file_v1_convert_file_post``, 54 fields).
Query parameters are silently ignored, so every option below has to travel in the
multipart body; passing them as ``params=`` makes docling run on pure defaults and
look "broken" (no page markers, no heading levels, OCR silently on).
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8091"
DEFAULT_PAGE_BREAK = "<!-- page-break -->"

#: Multipart fields matching the parameter mapping table in the plan (§1.3).
#: Values are strings because they all travel as form fields; pydantic coerces.
COMMON_FIELDS: list[tuple[str, str]] = [
    # The server default is True: OCR has to be switched off explicitly.
    ("do_ocr", "false"),
    ("force_ocr", "false"),
    ("table_mode", "accurate"),
    ("table_cell_matching", "true"),
    ("do_table_structure", "true"),
    # Server default is False, which leaves every heading at the same level.
    ("do_pdf_heading_hierarchy", "true"),
    ("include_images", "false"),
    ("image_export_mode", "placeholder"),
    ("abort_on_error", "false"),
    # Server default is "no timeout": a CPU-bound conversion can then keep burning
    # every core long after the caller gave up (measured: a 5-page paper did exactly
    # that for 17+ minutes and wedged the WSL VM). Always send a deadline.
    ("document_timeout", "150"),
]


def convert(
    client: httpx.Client,
    url: str,
    pdf: Path,
    *,
    ocr: bool,
    page_break: str,
    formats: list[str],
    page_range: str | None = None,
    document_timeout: float | None = 150.0,
) -> dict[str, Any]:
    """Send one PDF to docling-serve and return the parsed response."""
    fields: dict[str, Any] = dict(COMMON_FIELDS)
    # httpx wants a mapping here; list values become repeated multipart fields.
    fields["to_formats"] = formats
    fields["md_page_break_placeholder"] = page_break
    if ocr:
        fields["do_ocr"] = "true"
    if document_timeout is not None:
        fields["document_timeout"] = str(document_timeout)
    else:
        fields.pop("document_timeout", None)
    if page_range:
        fields["page_range"] = [part.strip() for part in page_range.split(",")]

    started = time.perf_counter()
    response = client.post(
        f"{url}/v1/convert/file",
        data=fields,
        files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
        timeout=1800.0,
    )
    wall = time.perf_counter() - started
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    payload["_wall_seconds"] = round(wall, 3)
    return payload


def _stats(markdown: str, page_break: str) -> dict[str, Any]:
    lines = markdown.splitlines()
    return {
        "chars": len(markdown),
        "lines": len(lines),
        "page_markers": markdown.count(page_break),
        "headings": sum(1 for line in lines if line.startswith("#")),
        "heading_levels": sorted({len(line) - len(line.lstrip("#")) for line in lines if line.startswith("#")}),
        "table_rows": sum(1 for line in lines if line.startswith("|")),
        "image_placeholders": markdown.count("<!-- image -->"),
        "blank_lines": sum(1 for line in lines if not line.strip()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--ocr", action="store_true", help="send do_ocr=true (default: off)")
    parser.add_argument("--page-break", default=DEFAULT_PAGE_BREAK)
    parser.add_argument("--page-range", default=None, help='e.g. "1-3" (docling page_range)')
    parser.add_argument(
        "--formats",
        default="md",
        help="comma-separated to_formats, e.g. md,json,doctags (server default: md)",
    )
    parser.add_argument(
        "--document-timeout",
        type=float,
        default=150.0,
        help="server-side deadline in seconds (0/negative = none; keep it set!)",
    )
    parser.add_argument("--out", type=Path, default=None, help="output directory")
    parser.add_argument("--no-json", action="store_true", help="skip writing the DoclingDocument JSON")
    args = parser.parse_args()

    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "-ocr" if args.ocr else ""
    outdir = args.out or Path("logs/eval/docling") / f"probe-{stamp}{suffix}"
    outdir.mkdir(parents=True, exist_ok=True)

    summary: list[dict[str, Any]] = []
    with httpx.Client() as client:
        for pdf in args.pdfs:
            try:
                data = convert(
                    client,
                    args.url,
                    pdf,
                    ocr=args.ocr,
                    page_break=args.page_break,
                    formats=formats,
                    page_range=args.page_range,
                    document_timeout=args.document_timeout if args.document_timeout > 0 else None,
                )
            except httpx.HTTPError as exc:
                print(f"!! {pdf.name}: {exc}")
                summary.append({"pdf": pdf.name, "error": str(exc)})
                continue

            document = data.get("document") or {}
            markdown = document.get("md_content") or ""
            stem = pdf.stem
            (outdir / f"{stem}.md").write_text(markdown, encoding="utf-8")
            raw = document.get("json_content")
            if not args.no_json and raw:
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except json.JSONDecodeError:
                        pass
                text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, indent=2)
                (outdir / f"{stem}.json").write_text(text, encoding="utf-8")

            row = {
                "pdf": pdf.name,
                "status": data.get("status"),
                "processing_time": data.get("processing_time"),
                "wall_seconds": data.get("_wall_seconds"),
                "errors": data.get("errors"),
                "formats_returned": sorted(k for k, v in document.items() if v),
                **_stats(markdown, args.page_break),
            }
            summary.append(row)
            print(
                f"{pdf.name:<22} status={row['status']:<8} "
                f"pages_marked={row['page_markers']:<3} headings={row['headings']:<3} "
                f"levels={row['heading_levels']} tables={row['table_rows']:<4} "
                f"chars={row['chars']:<7} docling={row['processing_time']}s wall={row['wall_seconds']}s"
            )

    (outdir / "summary.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "ocr": args.ocr,
                "formats": formats,
                "page_break": args.page_break,
                "document_timeout": args.document_timeout,
                "runs": summary,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwritten to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
