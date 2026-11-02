#!/usr/bin/env python3
"""Preview/apply explicitly reviewed PDF conflicts; always requires an audit report.

uv run python scripts/reconcile_pdf_metadata.py --plan <review.json> --report logs/eval/pdf-review.json
Add --apply to commit and refresh OpenSearch author keywords. Identical retries
are idempotent and retry index refresh after a previous post-commit failure.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import SessionLocal  # noqa: E402
from app.services import metadata_quality_review  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    decisions = json.loads(args.plan.read_text(encoding="utf-8"))
    if not isinstance(decisions, list):
        raise ValueError("review plan must be a JSON array")
    with SessionLocal() as session:
        report = metadata_quality_review.apply_review(session, decisions, apply=args.apply)
        if args.apply:
            session.commit()
        else:
            session.rollback()
        # Write the database result before starting a separately retryable index step.
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        if args.apply:
            report["index"] = metadata_quality_review.refresh_authors(session, report["paper_ids"])
            args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "paper_ids"}, ensure_ascii=False))
        return int(bool(report.get("index", {}).get("failed")))


if __name__ == "__main__":
    raise SystemExit(main())
