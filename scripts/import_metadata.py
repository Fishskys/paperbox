#!/usr/bin/env python3
"""Import external metadata from a JSON file (IEEE raw / CSL-JSON / generic).

    uv run python scripts/import_metadata.py ieee.json                 # dry run
    uv run python scripts/import_metadata.py ieee.json --apply
    uv run python scripts/import_metadata.py ieee.json --apply --report logs/eval/ieee-report.json

The format is detected from the payload. A dry run (the default) matches and
reports but writes nothing, which is the only safe way to look at a fresh export
before it touches the library. ``--report`` writes the same JSON the API returns,
so a dry-run report can be reviewed and then applied with
``POST /api/metadata/apply``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import SessionLocal  # noqa: E402
from app.services import metadata_import as importer  # noqa: E402
from app.services import metadata_sources as sources  # noqa: E402


def print_report(report: importer.ImportReport, path: Path) -> None:
    """Human-readable summary of an import report."""
    print(f"file:      {path}")
    print(f"format:    {report.format}")
    print(f"mode:      {'dry run (nothing written)' if report.dry_run else 'applied'}")
    # 一眼能看懂的三行：检测到多少、成功多少、失败多少（2026-10-10 owner 口径）。
    print(
        f"检测到元数据条目 {report.detected} 条，"
        f"导入成功 {report.total} 条，失败 {report.failed} 条"
        + (f"，因 limit 跳过 {report.skipped} 条" if report.skipped else "")
    )
    print(
        f"records:   {report.total} "
        f"(matched={report.matched} shell={report.created_shell} "
        f"ambiguous={report.ambiguous} unmatched={report.unmatched} unchanged={report.unchanged})"
    )
    if report.failures:
        print(f"失败条目（{len(report.failures)}）：")
        for item in report.failures[:20]:
            where = item.get("identifier") or "（无标识字段）"
            print(f"  #{item.get('index')} {where}  原因：{item.get('reason')}")
        if len(report.failures) > 20:
            print(f"  ... 另有 {len(report.failures) - 20} 条")
    if report.conflicts:
        print(f"conflicts: {len(report.conflicts)}")
        for item in report.conflicts[:20]:
            print(
                f"  {item.get('field')}: kept={item.get('kept')!r} "
                f"rejected={item.get('rejected')!r} ({item.get('reason')})"
            )
    for entry in report.sources[:20]:
        target = entry.get("paper_id") or "-"
        print(
            f"  {entry.get('source_ref')}: {entry.get('match_status')} "
            f"paper={target} via {entry.get('match_method')}"
        )
    if len(report.sources) > 20:
        print(f"  ... {len(report.sources) - 20} more")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="JSON file to import")
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--limit", type=int, default=None, help="import at most N records")
    parser.add_argument(
        "--source-type",
        default=sources.SOURCE_TYPE_IMPORT_FILE,
        choices=sources.SOURCE_TYPES,
        help="how to label the records (default: import_file)",
    )
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report here")
    args = parser.parse_args()

    if not args.path.is_file():
        print(f"no such file: {args.path}", file=sys.stderr)
        return 2

    session = SessionLocal()
    try:
        try:
            report = importer.import_file(
                session,
                args.path,
                apply=args.apply,
                source_type=args.source_type,
                importer="scripts/import_metadata.py",
                limit=args.limit,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"cannot read {args.path}: {exc}", file=sys.stderr)
            return 2
        if args.apply:
            session.commit()
        payload = report.as_dict()
        print_report(report, args.path)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            print(f"report written to {args.report}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())