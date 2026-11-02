#!/usr/bin/env python3
"""Replay PDF quality rules against previously reviewed cases without writes.

uv run python scripts/audit_pdf_metadata_quality.py --authors 50 --report logs/eval/pdf-quality.json

Uses explicit review snapshots from reconcile_pdf_metadata, not current database
values as presumed ground truth. All abstract decisions and a deterministic
author sample are checked. Source PDFs are downloaded read-only; reports belong
under ignored logs/. A missing reviewed baseline is an incomplete acceptance.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.parsing.pdf import extract_pages  # noqa: E402
from app.services import metadata_policy as policy, metadata_reextract, object_storage  # noqa: E402


def replay(raw: dict, candidates: list[tuple[str, object]], evidence: dict,
           kept_source: str | None) -> dict:
    """Compare automatic replacements to the reviewed value; ambiguity is explicit."""
    field, selected = raw["field"], raw["kept"]
    actions = []
    incorrect = False
    for source, candidate in candidates:
        if candidate in (None, "", []) or policy.equivalent_values(field, selected, candidate):
            continue
        choice = policy.pdf_choice(field, selected, candidate, kept_source, source,
                                   title=raw.get("title"), evidence=evidence)
        if choice is None:
            actions.append(["protected", "source_authority"])
            continue
        action, reason = choice
        actions.append([action, reason])
        if action == "incoming":
            selected, kept_source = candidate, source
            incorrect |= not policy.equivalent_values(field, selected, raw["value"])
    if incorrect:
        status = "incorrect_automatic_change"
    elif any(action == "review" for action, _ in actions):
        status = "needs_review"
    elif policy.equivalent_values(field, selected, raw["value"]):
        status = "matches_review"
    else:
        status = "needs_review"
    return {"paper_id": raw["paper_id"], "field": field, "status": status, "actions": actions,
            "text_state": evidence.get("text_state"), "byline_complete": evidence.get("byline_complete")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authors", type=int, default=50)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.authors < 1:
        parser.error("--authors must be positive")
    rows = []
    with SessionLocal() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        cases = session.execute(text("""
            SELECT s.raw, f.object_key, f.bucket, current_source.source_type AS kept_source,
                   rejected_source.source_type AS rejected_source
            FROM paper_sources s
            LEFT JOIN paper_files f ON f.paper_id=s.paper_id AND f.is_primary
            LEFT JOIN paper_sources current_source ON CAST(current_source.id AS text)=s.raw->>'original_current_source_id'
            LEFT JOIN paper_sources rejected_source ON CAST(rejected_source.id AS text)=s.raw->>'original_rejected_source_id'
            WHERE s.importer='scripts/reconcile_pdf_metadata.py'
            ORDER BY s.paper_id, s.raw->>'field'
        """)).mappings().all()
        author_count = 0
        for case in cases:
            raw = case["raw"]
            if raw["field"] == "abstract":
                rows.append(replay(raw, [(case["rejected_source"] or "pdf_embedded", raw["rejected"])], {}, case["kept_source"]))
            elif raw["field"] == "authors" and author_count < args.authors:
                author_count += 1
                try:
                    pdf = object_storage.download_bytes(case["object_key"], bucket=case["bucket"])
                    pages = extract_pages(pdf)
                    embedded, body, evidence = metadata_reextract.pdf_candidates(pages, pdf_bytes=pdf, title=raw.get("title"))
                    rows.append(replay(raw, [("pdf_embedded", embedded.authors), ("pdf_heuristic", (body or {}).get("authors"))], evidence, case["kept_source"]))
                except Exception as exc:
                    rows.append({"paper_id": raw["paper_id"], "field": "authors", "status": "error", "error": type(exc).__name__})
        session.rollback()
    counts = dict(Counter(row["status"] for row in rows))
    report = {"rule_version": policy.QUALITY_VERSION, "read_only": True, "counts": counts,
              "fields": dict(Counter(row["field"] for row in rows)), "cases": rows}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))
    return int(not rows or counts.get("error", 0) > 0 or counts.get("incorrect_automatic_change", 0) > 0)


if __name__ == "__main__":
    raise SystemExit(main())