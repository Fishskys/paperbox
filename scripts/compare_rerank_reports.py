"""Compare two ``scripts/eval.py`` reports and apply the rerank-swap gates (T-C3).

    uv run python scripts/compare_rerank_reports.py \\
        --before evals/report-int8-jina.json \\
        --after  evals/report-int8-mmarco.json

Both reports must come from the same query set and the same corpus (the frozen 30
papers); the language split is read from ``evals/queries.jsonl`` because the eval
reports themselves do not carry it. Prints overall and per-language metrics with
their deltas, then checks the three gates that decide whether the new model stays:

* overall HR@1 drop <= ``--max-hr1-drop`` (default 0.02)
* overall nDCG@10 drop <= ``--max-ndcg10-drop`` (default 0.02)
* Chinese-only HR@1 drop <= ``--max-zh-hr1-drop`` (default 0.05)

Exit code is 0 when every gate passes, 1 otherwise -- so the swap can be reverted
on the evidence rather than on an impression. This is a pure offline reader: no
HTTP, no database.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("hybrid|on", "hybrid|off")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _languages() -> dict[str, str]:
    path = ROOT / "evals" / "queries.jsonl"
    languages: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                languages[row["id"]] = row.get("language", "?")
    return languages


def _group(report: dict, group: str | None) -> list[dict]:
    rows = [row for row in report["per_query"] if row.get("error") is None]
    if group:
        rows = [row for row in rows if row.get("group") == group]
    return rows


def _mean(rows: list[dict], metric: str) -> float | None:
    values = [row["metrics"][metric] for row in rows if metric in (row.get("metrics") or {})]
    return sum(values) / len(values) if values else None


def _table(rows: list[dict], label: str, metrics: list[str]) -> dict[str, float | None]:
    return {metric: _mean(rows, metric) for metric in metrics}


def _fmt(value: float | None) -> str:
    return "  n/a " if value is None else f"{value:.3f}"


def _delta(before: float | None, after: float | None) -> str:
    if before is None or after is None:
        return "  n/a "
    diff = after - before
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.3f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument("--group", default="hybrid|on", help=f"report group to compare ({', '.join(GROUPS)})")
    parser.add_argument("--max-hr1-drop", type=float, default=0.02)
    parser.add_argument("--max-ndcg10-drop", type=float, default=0.02)
    parser.add_argument("--max-zh-hr1-drop", type=float, default=0.05)
    parser.add_argument("--metric-scale", type=float, default=100.0, help="multiply deltas for display (100 = points)")
    args = parser.parse_args()

    before, after = _load(args.before), _load(args.after)
    languages = _languages()
    metrics = ["hit_rate@1", "hit_rate@3", "hit_rate@10", "ndcg@1", "ndcg@10", "mrr"]

    rows_before = _group(before, args.group)
    rows_after = _group(after, args.group)
    if not rows_before or not rows_after:
        raise SystemExit(f"no rows for group {args.group!r} in one of the reports")

    print(f"before: {args.before}  ({before.get('generated_at')})")
    print(f"after:  {args.after}  ({after.get('generated_at')})")
    print(f"group:  {args.group}   scale: delta x{args.metric_scale:g}")
    print()

    header = f"{'subset':<8} {'n':>3} " + " ".join(f"{m:>11}" for m in metrics)
    print(header)
    print("-" * len(header))

    results: list[tuple[str, int, dict, dict]] = []
    for subset, predicate in (
        ("all", lambda _row: True),
        ("zh", lambda row: languages.get(row["query_id"]) == "zh"),
        ("en", lambda row: languages.get(row["query_id"]) == "en"),
    ):
        before_rows = [row for row in rows_before if predicate(row)]
        after_rows = [row for row in rows_after if predicate(row)]
        results.append(
            (subset, len(before_rows), _table(before_rows, subset, metrics), _table(after_rows, subset, metrics))
        )

    for subset, count, before_table, after_table in results:
        before_cells = " ".join(_fmt(before_table[m]) for m in metrics)
        print(f"{subset:<8} {count:>3} {before_cells}")
        after_cells = " ".join(_fmt(after_table[m]) for m in metrics)
        print(f"{'':<8} {'':>3} {after_cells}")
        delta_cells = " ".join(_delta(before_table[m], after_table[m]) for m in metrics)
        print(f"{'':<8} {'Δ':>3} {delta_cells}")
        print()

    overall = {row[0]: row for row in results}
    zh = overall["zh"]
    gates: list[tuple[str, float, float, bool]] = []
    for name, subset, metric, limit in (
        ("overall HR@1", "all", "hit_rate@1", args.max_hr1_drop),
        ("overall nDCG@10", "all", "ndcg@10", args.max_ndcg10_drop),
        ("zh HR@1", "zh", "hit_rate@1", args.max_zh_hr1_drop),
    ):
        subset_row = overall[subset]
        before_value, after_value = subset_row[2][metric], subset_row[3][metric]
        if before_value is None or after_value is None:
            gates.append((f"{name} ({subset}, n={subset_row[1]})", float("nan"), limit, True))
            continue
        drop = before_value - after_value
        gates.append((f"{name} ({subset}, n={subset_row[1]})", drop, limit, drop <= limit))

    print("gates (delta x100 = points):")
    all_passed = True
    for name, drop, limit, passed in gates:
        all_passed = all_passed and passed
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}: drop {drop * args.metric_scale:+.1f} pts, allowed {limit * args.metric_scale:.1f}")
    print()
    print("swap keeps: YES" if all_passed else "swap keeps: NO -> revert to the previous model")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
