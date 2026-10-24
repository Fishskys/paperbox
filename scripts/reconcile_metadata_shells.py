#!/usr/bin/env python3
"""Merge exact metadata shells into PDF papers and delete safe unmatched shells.

    uv run python scripts/reconcile_metadata_shells.py
    uv run python scripts/reconcile_metadata_shells.py --apply
    uv run python scripts/reconcile_metadata_shells.py --apply \
        --report logs/eval/shell-reconcile.json

The default is a transactional dry run. ``--apply`` is irreversible for shells
that have no exact non-shell match; the service refuses to touch rows carrying a
file, chunk, or ingestion job.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import SessionLocal  # noqa: E402
from app.services import metadata_shell_reconcile  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit merge/delete actions")
    parser.add_argument("--limit", type=int, default=None, help="process at most N shells")
    parser.add_argument("--report", type=Path, default=None, help="write a JSON report")
    args = parser.parse_args()

    if not args.apply:
        # Savepoint simulation calls the real write services. Their INFO records
        # would otherwise describe rolled-back attachments as if they persisted.
        for logger_name in (
            "app.services.metadata_identifiers",
            "app.services.metadata_sources",
        ):
            logging.getLogger(logger_name).setLevel(logging.WARNING)

    session = SessionLocal()
    try:
        report = metadata_shell_reconcile.reconcile_shells(
            session, apply=args.apply, limit=args.limit
        )
        if args.apply:
            session.commit()
        payload = report.as_dict()
        print(
            f"mode={'applied' if args.apply else 'dry run'} total={report.total} "
            f"merged={report.merged} deleted={report.deleted} "
            f"skipped={report.skipped} failed={report.failed}"
        )
        for item in report.items[:20]:
            print(
                f"  {item['action']:7} shell={item['shell_id']} "
                f"target={item.get('paper_id', '-')} via={item.get('match_method', '-')} "
                f"reason={item.get('reason', '-')}"
            )
        if len(report.items) > 20:
            print(f"  ... {len(report.items) - 20} more")
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            print(f"report written to {args.report}")
        return 1 if report.failed else 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
