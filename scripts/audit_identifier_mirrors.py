#!/usr/bin/env python3
"""Audit the ``papers.doi`` / ``papers.arxiv_id`` mirrors against ``paper_identifiers``.

    uv run python scripts/audit_identifier_mirrors.py
    uv run python scripts/audit_identifier_mirrors.py --json
    uv run python scripts/audit_identifier_mirrors.py --limit 500 --no-fail

The mirrors are kept in step by the application (``mirror_legacy_columns``), not
by the database, so drift is possible in principle. This is a strictly read-only
watchdog: it reports drift and exits non-zero (``--no-fail`` to report only), and
it never writes or repairs. The same report is served by ``GET
/api/consistency/doi``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import identifier_mirror_audit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the raw JSON report")
    parser.add_argument("--limit", type=int, default=50, help="max drift rows to list")
    parser.add_argument("--no-fail", action="store_true", help="exit 0 even on drift")
    args = parser.parse_args()

    report = identifier_mirror_audit.audit_identifier_mirrors(limit=args.limit)
    data = report.as_dict()

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"checked at {data['checked_at']}")
        print(f"  papers: {data['papers_total']} ({data['papers_live']} live)")
        print(f"  mirrors set        : {data['mirrors_set']}")
        print(f"  identifiers (live) : {data['identifiers_total']}")
        if data["drift_totals"]:
            print(f"  DRIFT: {data['drift_totals']}")
            for drift in data["drifts"]:
                print(
                    f"    - {drift['kind']} [{drift['scheme']}] {drift['paper_id']}"
                    f"  mirror={drift['mirror_value']!r}"
                    f"  identifiers={drift['identifier_values']}"
                    f"  ({drift['detail']})"
                )
            if data["truncated"]:
                print(f"    (listing capped at {args.limit}; totals above are exact)")
        else:
            print("  DRIFT: none — papers.doi/arxiv_id agree with paper_identifiers")
        for error in data["errors"]:
            print(f"  error: {error}")

    if data["errors"]:
        return 1
    if data["drift_totals"] and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
