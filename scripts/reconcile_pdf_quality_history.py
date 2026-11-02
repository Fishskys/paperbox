#!/usr/bin/env python3
"""Preview historical PDF evidence, then apply the guarded field-only report.

uv run python scripts/reconcile_pdf_quality_history.py --report logs/eval/pdf-history-preview.json
uv run python scripts/reconcile_pdf_quality_history.py --apply --plan logs/eval/pdf-history-preview.json --report logs/eval/pdf-history-result.json

An optional --candidates report supplies a previously captured items[] list.
Extraction caches and raw previews belong under ignored logs/eval/.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services import metadata_history as history, metadata_index_sync, metadata_policy  # noqa: E402


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


def recipe() -> str:
    paths = ["app/parsing/pdf.py", "app/parsing/mupdf.py", "app/parsing/metadata_fields.py", "app/services/pdf_byline.py",
             "app/services/metadata_service.py", "app/services/metadata_policy.py", "app/services/metadata_reextract.py"]
    return hashlib.sha256(b"".join((ROOT / path).read_bytes() for path in paths)).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--field", choices=history.FIELDS)
    parser.add_argument("--workers", type=int, default=settings.pdf_process_concurrency)
    args = parser.parse_args()
    if not 1 <= args.workers <= settings.pdf_process_concurrency:
        parser.error("workers must respect PDF_PROCESS_CONCURRENCY")
    if args.apply:
        if not args.plan:
            parser.error("--apply requires an explicit preview --plan")
        preview = json.loads(args.plan.read_text(encoding="utf-8"))
        if preview["rule_version"] != metadata_policy.QUALITY_VERSION or preview["recipe"] != recipe():
            parser.error("code or quality rules changed; regenerate the preview")
        results = []
        for item in preview["items"]:
            if args.field and item["snapshot"]["field"] != args.field:
                continue
            if "error" in item:
                results.append({"status": "skipped", "reason": item["error"]})
                continue
            with SessionLocal() as session:
                try:
                    result = history.apply(session, item["snapshot"], item["extracted"], item["decision"])
                    session.commit()
                    index = metadata_index_sync.flush_pending(session, paper_id=item["snapshot"]["paper_id"])
                    results.append({"paper_id": item["snapshot"]["paper_id"], **result, "index": index})
                except Exception as exc:
                    session.rollback()
                    results.append({"paper_id": item["snapshot"]["paper_id"], "status": "failed", "error": type(exc).__name__})
            write(args.report, {"rule_version": metadata_policy.QUALITY_VERSION, "items": results})
        counts = dict(Counter(row["status"] for row in results))
        write(args.report, {"rule_version": metadata_policy.QUALITY_VERSION, "counts": counts, "items": results})
        print(json.dumps({"counts": counts, "changed": sum(bool(row.get("changed")) for row in results),
                          "pending": sum(bool(row.get("pending")) for row in results)}, ensure_ascii=False))
        return int(bool(counts.get("failed") or counts.get("skipped")))
    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))["items"] if args.candidates else None
    if args.field and candidates is not None:
        candidates = [candidate for candidate in candidates if candidate["field"] == args.field]
    with SessionLocal() as session:
        if session.bind.dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        snapshots = history.snapshots(session, candidates)
    if args.field:
        snapshots = [snapshot for snapshot in snapshots if snapshot["field"] == args.field]
    if args.limit is not None:
        snapshots = snapshots[:max(0, args.limit)]
    recipe_id = recipe()
    cache = args.report.with_suffix(".cache")
    def extract(snapshot):
        path = cache / f"{history.fingerprint([snapshot['file'], snapshot['title'], snapshot['url'], recipe_id])}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        result = history.extract(snapshot)
        write(path, result)
        return result
    items = []
    with ThreadPoolExecutor(max_workers=args.workers) as workers:
        futures = {workers.submit(extract, snapshot): snapshot for snapshot in snapshots}
        for future in as_completed(futures):
            snapshot = futures[future]
            try:
                extracted = future.result()
                items.append({"snapshot": snapshot, "extracted": extracted, "decision": history.plan(snapshot, extracted)})
            except Exception as exc:
                items.append({"snapshot": snapshot, "error": type(exc).__name__})
            if len(items) % 20 == 0:
                print(json.dumps({"completed": len(items), "total": len(snapshots)}), flush=True)
                write(args.report, {"rule_version": metadata_policy.QUALITY_VERSION, "recipe": recipe_id, "items": items})
    counts = {"fields": len(items), "failed": sum("error" in item for item in items),
              "changed": sum(item.get("decision", {}).get("changed", False) for item in items),
              "pending": sum(item.get("decision", {}).get("pending", False) for item in items)}
    write(args.report, {"rule_version": metadata_policy.QUALITY_VERSION, "recipe": recipe_id, "counts": counts, "items": items})
    print(json.dumps(counts, ensure_ascii=False))
    return int(bool(counts["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
