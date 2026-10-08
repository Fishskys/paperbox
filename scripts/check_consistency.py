#!/usr/bin/env python3
"""Three-way consistency check on the real machine (PostgreSQL / MinIO / OpenSearch).

    uv run python scripts/check_consistency.py
    uv run python scripts/check_consistency.py --json
    uv run python scripts/check_consistency.py --limit 500 --no-fail
    uv run python scripts/check_consistency.py --parser-papers   # + per-backend paper ids

Prints the store totals, then every paper whose copies disagree, then objects and
documents that have no paper row. Exits non-zero when anything drifted (``--no-fail``
to report only). Strictly read-only - nothing is repaired here, and the fixes are
``POST /api/papers/{id}/reindex`` (missing documents), ``POST /api/papers/{id}/ingest/file``
(missing file) or ``scripts/purge_deleted.py`` (residue of deleted papers).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import consistency_service  # noqa: E402


def print_report(report) -> None:
    data = report.as_dict()
    totals = data["totals"]
    print(
        f"index {data['index']} (exists={data['index_exists']})  "
        f"checked at {data['checked_at']}"
    )
    print(
        f"  postgres  : {totals['papers']} papers "
        f"({totals['papers_live']} live, {totals['papers_deleted']} deleted), "
        f"{totals['files_pg']} file row(s), {totals['chunks_pg']} chunk row(s)"
    )
    print(
        f"  minio     : {totals['objects_minio']} paper object(s), "
        f"{totals['staging_objects']} staging object(s)"
    )
    print(f"  opensearch: {totals['documents_os']} document(s)")
    census = data.get("parser_backends") or {}
    print(
        f"  parser    : papers {census.get('papers') or {}}  "
        f"documents {census.get('documents') or {}}"
    )
    for backend, ids in (census.get("paper_ids") or {}).items():
        print(f"    {backend}: {len(ids)} paper(s)")
        for paper_id in ids:
            print(f"      {paper_id}")
    if census.get("paper_ids_truncated"):
        print("    note: the paper id list hit its cap")
    models = data.get("embedding_models") or {}
    print(
        f"  embedding : chunks {models.get('chunks') or {}}  "
        f"documents {models.get('documents') or {}}"
    )
    print(
        "consistent: "
        + ("yes" if data["consistent"] else "NO")
        + f"  (problems={totals['problems']}, orphan objects={totals['orphan_objects']}, "
        f"orphan documents={totals['orphan_documents']}, "
        f"cache objects={totals.get('cache_objects', 0)} (parse cache, not drift), "
        f"errors={len(data['errors'])}, "
        f"took {data['took_ms']}ms)"
    )

    if data["problems"]:
        print()
        print(f"{'paper_id':38} {'status':11} {'files/obj':10} {'chunks/docs':12} issues")
        for problem in data["problems"]:
            files_pg = problem["files_pg"]
            objects = problem["objects_minio"]
            chunks_pg = problem["chunks_pg"]
            chunks_os = problem["chunks_os"]
            print(
                f"{problem['paper_id']:38} {problem['status']:11} "
                f"{f'{files_pg}/{objects}':10} {f'{chunks_pg}/{chunks_os}':12} "
                f"{','.join(problem['issues'])}"
            )
            for key in problem["missing_objects"]:
                print(f"    missing object: {key}")
            for key in problem["orphan_objects"]:
                print(f"    orphan object : {key}")

    for label, key in (
        ("orphan object", "orphan_objects"),
        ("orphan document (paper_id)", "orphan_documents"),
    ):
        for value in data[key]:
            print(f"  {label}: {value}")

    for error in data["errors"]:
        print(f"  error: {error}")
    if data["truncated"]:
        print("  note: the paper_id aggregation hit its size limit - counts are incomplete")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the raw report as JSON")
    parser.add_argument(
        "--limit",
        type=int,
        default=consistency_service.DEFAULT_PROBLEM_LIMIT,
        help="how many problem papers to list",
    )
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="exit 0 even when drift was found",
    )
    parser.add_argument(
        "--parser-papers",
        action="store_true",
        help=(
            "also list the live paper ids behind each parser stamp -- the worklist "
            "for `scripts/reindex.py --parser-backend <name>`"
        ),
    )
    args = parser.parse_args()
    report = consistency_service.check_consistency(
        limit=args.limit, with_parser_papers=args.parser_papers
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print_report(report)
    if report.consistent or args.no_fail:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())