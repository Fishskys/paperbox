#!/usr/bin/env python3
"""Paired A/B of the retrieval backends, with a noise floor (plan §7 T-E5).

    uv run python scripts/compare_backends.py --report evals/report-m5-backend-ab.json

Reads one ``scripts/eval.py`` report that ran **both** backends against the same
server (``--backend python,native``) and compares them query by query.

Why a paired comparison instead of two means
--------------------------------------------
The plan's gate was "整体与 EN/ZH 分组 HR@1/ndcg@10 差异 ≤0.01". Two means cannot
carry that judgement: with 60 queries (10 in Chinese) a single query that flips
moves a group mean by 0.1, and NDCG differences of 0.01 are inside the sampling
noise. So every metric comes with a **bootstrap 95% confidence interval over
queries** (paired deltas, resampled with replacement) and the verdict is:

* CI entirely above 0 -> the change is a win at this sample size;
* CI entirely below 0 -> a loss;
* CI crossing 0      -> **indistinguishable**: the honest answer, not "slightly
  better". A group with few queries (ZH: n=10) will land here often, and that is
  the finding.

The tool also reports what the two paths *return*, not just how they score:
the Python path aggregates ``top_k`` chunks into however many papers they cover
(measured: 1-5 papers for ``top_k=10``), the native path collapses to distinct
papers and fills the page. A longer list inflates NDCG@10 and recall@10 without
ranking anything better, so the report includes **common-depth** metrics
(``ndcg@K*``/``recall@K*`` recomputed at the depth both lists actually reached)
next to the raw ones. If the two disagree, trust the common-depth pair.

Exit code is the verdict: ``0`` when the native backend is not a regression
(CI not below the tolerance, and no more than 20% slower at p50), ``1``
otherwise. ``--rerank`` picks which rerank setting to judge (default ``off``:
the pure fusion difference, before the cross-encoder washes it out).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.eval.metrics import (  # noqa: E402  (path bootstrap above)
    hit_rate_at_k,
    mrr,
    ndcg_at_k,
    recall_at_k,
)
from app.search.native import BACKENDS  # noqa: E402

#: Resamples for the bootstrap; 10k keeps the CI stable to ~0.001.
BOOTSTRAP_ITERATIONS = 10_000
#: Fixed seed: the report must be reproducible, not "a different CI every run".
BOOTSTRAP_SEED = 20261001
#: Metrics the verdict is based on (the others are context).
PRIMARY_METRICS = ("ndcg@10", "mrr", "hit_rate@1")
#: The path in production today; the candidate is compared against it and every
#: Δ is ``candidate - baseline``.
BASELINE_BACKEND = "python"
#: The path under test.
CANDIDATE_BACKEND = "native"
#: Regression tolerance on a primary metric (plan T-E5's 0.01, on the delta's CI).
REGRESSION_TOLERANCE = 0.01
#: Latency tolerance for the p50 comparison (plan T-E5: "不得更慢 20% 以上").
LATENCY_TOLERANCE = 0.20


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def language_by_query(report: dict) -> dict[str, str]:
    """``query_id -> language`` from the query set the report was built on."""
    path = Path((report.get("params") or {}).get("queries") or "")
    if not path.is_file():
        return {}
    languages: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        languages[str(row.get("id"))] = str(row.get("language") or "?")
    return languages


def pair_key(row: Mapping[str, Any]) -> str:
    """``mode|on`` / ``mode|off`` -- the A/B axis is the backend, not the title.

    ``scripts/eval.py --backend a,b`` suffixes its group label with the backend
    (``hybrid|off|native``) so a report reads well on its own; the comparison must
    rebuild the key from the row's own fields, otherwise every group holds exactly
    one backend and the pair is empty. Older reports without ``mode``/``rerank``
    fall back to the first two segments of the label.
    """
    mode = row.get("mode")
    if mode is None:
        return "|".join(str(row.get("group") or "").split("|")[:2])
    return f"{mode}|{'on' if row.get('rerank') else 'off'}"


def collect(report: dict) -> dict[str, dict[str, dict[str, dict]]]:
    """``mode|rerank -> query_id -> backend -> row`` for the successful rows only."""
    table: dict[str, dict[str, dict[str, dict]]] = {}
    for row in report.get("per_query") or []:
        if row.get("error"):
            continue
        backend = row.get("backend")
        if not backend:
            # A report taken without --backend cannot be an A/B: the server's
            # SEARCH_BACKEND chose for us and both arms would be the same path.
            continue
        table.setdefault(pair_key(row), {}).setdefault(
            str(row["query_id"]), {}
        )[str(backend)] = row
    return table


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #
def bootstrap_ci(
    values: Sequence[float],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, float | int | None]:
    """Percentile bootstrap CI of the mean of ``values`` (paired deltas)."""
    clean = [float(value) for value in values]
    if not clean:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    mean = sum(clean) / len(clean)
    if len(clean) == 1:
        return {"mean": mean, "lo": mean, "hi": mean, "n": 1}
    rng = random.Random(seed)
    size = len(clean)
    means: list[float] = []
    for _ in range(iterations):
        total = 0.0
        for _ in range(size):
            total += clean[rng.randrange(size)]
        means.append(total / size)
    means.sort()
    lo = means[int(0.025 * (len(means) - 1))]
    hi = means[int(0.975 * (len(means) - 1))]
    return {"mean": mean, "lo": lo, "hi": hi, "n": size}


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def verdict_for(ci: dict) -> str:
    """``win`` / ``loss`` / ``indistinguishable`` for one metric's CI."""
    lo, hi = ci.get("lo"), ci.get("hi")
    if lo is None or hi is None:
        return "no data"
    if lo > 0:
        return "win"
    if hi < 0:
        return "loss" if hi < -REGRESSION_TOLERANCE else "loss (within tolerance)"
    return "indistinguishable"


# --------------------------------------------------------------------------- #
# per-group comparison
# --------------------------------------------------------------------------- #
def metric_names(rows: Sequence[dict]) -> list[str]:
    names: set[str] = set()
    for row in rows:
        names |= set((row.get("metrics") or {}).keys())
    return sorted(names)


def common_depth_metrics(
    left: dict, right: dict, ks: Sequence[int]
) -> dict[str, dict[str, float]]:
    """Metrics at the depth **both** lists reached (``K* = min(len_a, len_b)``).

    NDCG@10 and recall@10 reward a longer result list even when the ranking is
    identical, and the two backends return very different lengths. Recomputing at
    the common depth removes that artefact; the value is therefore a per-query
    ``ndcg@K*`` averaged over queries, not a fixed ``@10``.
    """
    ids_a = [str(item) for item in left.get("ranked_ids") or []]
    ids_b = [str(item) for item in right.get("ranked_ids") or []]
    labels = {str(k): int(v) for k, v in (left.get("labels") or {}).items()}
    depth = min(len(ids_a), len(ids_b))
    if depth < 1:
        return {}
    hita = hit_rate_at_k(ids_a, labels, 1)
    hitb = hit_rate_at_k(ids_b, labels, 1)
    return {
        "depth": depth,
        "python": {
            "ndcg@K*": ndcg_at_k(ids_a, labels, depth),
            "recall@K*": recall_at_k(ids_a, labels, depth),
            "hit_rate@1": hita,
        },
        "native": {
            "ndcg@K*": ndcg_at_k(ids_b, labels, depth),
            "recall@K*": recall_at_k(ids_b, labels, depth),
            "hit_rate@1": hitb,
        },
    }


def compare_group(
    group: str,
    per_query: dict[str, dict[str, dict]],
    languages: dict[str, str],
    ks: Sequence[int],
) -> dict[str, Any]:
    """One ``mode|rerank`` group: metrics, CIs, list lengths, latency, verdict."""
    sources = sorted({backend for rows in per_query.values() for backend in rows})
    if len(sources) < 2:
        return {
            "group": group,
            "skipped": f"needs two backends, found {sources or 'none'}",
        }
    # The sign must not depend on alphabetical luck: ``left`` is the baseline
    # (the path in production today) and ``right`` the candidate, so every Δ is
    # "candidate − baseline" and a negative bound means the candidate regressed.
    left = BASELINE_BACKEND if BASELINE_BACKEND in sources else sources[0]
    right = next(backend for backend in sources if backend != left)
    paired = {
        query_id: rows
        for query_id, rows in per_query.items()
        if left in rows and right in rows
    }
    if not paired:
        return {"group": group, "skipped": "no query ran on both backends"}

    rows_l = [paired[qid][left] for qid in sorted(paired)]
    rows_r = [paired[qid][right] for qid in sorted(paired)]
    names = metric_names(rows_l + rows_r)

    metrics: dict[str, Any] = {}
    for name in names:
        deltas = [
            float(row_r["metrics"].get(name) or 0.0) - float(row_l["metrics"].get(name) or 0.0)
            for row_l, row_r in zip(rows_l, rows_r)
        ]
        ci = bootstrap_ci(deltas)
        metrics[name] = {
            "delta": ci,
            "verdict": verdict_for(ci),
            f"mean_{left}": _mean(rows_l, name),
            f"mean_{right}": _mean(rows_r, name),
            "wins": sum(1 for value in deltas if value > 0),
            "losses": sum(1 for value in deltas if value < 0),
            "ties": sum(1 for value in deltas if value == 0),
        }

    lengths = {
        backend: {
            "mean": _mean_rows(rows, "returned"),
            "min": min((row.get("returned") or 0) for row in rows),
            "max": max((row.get("returned") or 0) for row in rows),
        }
        for backend, rows in ((left, rows_l), (right, rows_r))
    }
    equal_length = sum(
        1 for a, b in zip(rows_l, rows_r) if (a.get("returned") or 0) == (b.get("returned") or 0)
    )

    depths = [
        item["depth"]
        for item in (
            common_depth_metrics(a, b, ks) for a, b in zip(rows_l, rows_r)
        )
        if item
    ]
    common: dict[str, Any] = {}
    for name in ("ndcg@K*", "recall@K*", "hit_rate@1"):
        deltas = []
        for a, b in zip(rows_l, rows_r):
            item = common_depth_metrics(a, b, ks)
            if not item:
                continue
            deltas.append(float(item[right][name]) - float(item[left][name]))
        ci = bootstrap_ci(deltas)
        common[name] = {"delta": ci, "verdict": verdict_for(ci)}

    latency = {
        backend: {
            "p50": percentile([row.get("took_ms") or 0 for row in rows], 0.50),
            "p95": percentile([row.get("took_ms") or 0 for row in rows], 0.95),
        }
        for backend, rows in ((left, rows_l), (right, rows_r))
    }
    p50_l = latency[left]["p50"] or 0.0
    p50_r = latency[right]["p50"] or 0.0
    slow_pct = ((p50_r - p50_l) / p50_l) if p50_l else 0.0

    by_language: dict[str, Any] = {}
    for language in sorted({languages.get(qid, "?") for qid in paired}):
        subset = [qid for qid in paired if languages.get(qid, "?") == language]
        subset_metrics: dict[str, Any] = {}
        for name in PRIMARY_METRICS:
            deltas = [
                float(paired[qid][right]["metrics"].get(name) or 0.0)
                - float(paired[qid][left]["metrics"].get(name) or 0.0)
                for qid in subset
            ]
            ci = bootstrap_ci(deltas)
            subset_metrics[name] = {"delta": ci, "verdict": verdict_for(ci)}
        by_language[language] = {"n": len(subset), "metrics": subset_metrics}

    return {
        "group": group,
        "left": left,
        "right": right,
        "queries": len(paired),
        "metrics": metrics,
        "common_depth": {"depths": sorted(set(depths)), "metrics": common},
        "returned": {**lengths, "equal_length_queries": equal_length},
        "latency_ms": {**latency, "p50_delta_pct": slow_pct},
        "by_language": by_language,
    }


def _mean(rows: Sequence[dict], name: str) -> float:
    values = [float(row["metrics"].get(name) or 0.0) for row in rows]
    return sum(values) / len(values) if values else 0.0


def _mean_rows(rows: Sequence[dict], key: str) -> float:
    values = [float(row.get(key) or 0) for row in rows]
    return sum(values) / len(values) if values else 0.0


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 检索后端 A/B：python（v2 双查询 + 进程内 RRF）vs native（原生 hybrid + collapse）")
    lines.append("")
    lines.append(f"生成时间：{result['generated_at']}")
    lines.append(f"报告来源：`{result['source']}`")
    lines.append("")
    lines.append(
        "口径：同一台服务、同一批查询、同一份人工标签；每个指标给的是**配对差值**"
        f"（候选 − 基线，即 {CANDIDATE_BACKEND} − {BASELINE_BACKEND}）"
        f"的 bootstrap 95% 置信区间（{BOOTSTRAP_ITERATIONS} 次重采样，种子 {BOOTSTRAP_SEED}）。"
        "CI 跨 0 = **无法区分**，不是「略好」。"
    )
    lines.append("")
    for group in result["groups"]:
        lines.append(f"## {group.get('group')}")
        if group.get("skipped"):
            lines.append(f"（跳过：{group['skipped']}）")
            lines.append("")
            continue
        left, right = group["left"], group["right"]
        lines.append(
            f"共同查询 **{group['queries']}** 条；左 `{left}`、右 `{right}`。"
        )
        lines.append("")
        lines.append(f"| 指标 | {left} | {right} | Δ（{right}−{left}） | 95% CI | 判定 | 赢/输/平 |")
        lines.append("|---|---|---|---|---|---|---|")
        for name, item in sorted(group["metrics"].items()):
            ci = item["delta"]
            lines.append(
                f"| `{name}` | {item[f'mean_{left}']:.4f} | {item[f'mean_{right}']:.4f} | "
                f"{ci['mean']:+.4f} | [{ci['lo']:+.4f}, {ci['hi']:+.4f}] | {item['verdict']} | "
                f"{item['wins']}/{item['losses']}/{item['ties']} |"
            )
        lines.append("")
        common = group["common_depth"]
        if common["metrics"]:
            lines.append(
                f"**同深度对比**（每条查询取 K\\* = min(两列表长)，实测 K\\* ∈ {common['depths']}）："
            )
            lines.append("")
            lines.append("| 指标 | Δ | 95% CI | 判定 |")
            lines.append("|---|---|---|---|")
            for name, item in sorted(common["metrics"].items()):
                ci = item["delta"]
                lines.append(
                    f"| `{name}` | {ci['mean']:+.4f} | [{ci['lo']:+.4f}, {ci['hi']:+.4f}] | {item['verdict']} |"
                )
            lines.append("")
        returned = group["returned"]
        lines.append(
            f"**返回篇数**：`{left}` 均值 {returned[left]['mean']:.1f}"
            f"（{returned[left]['min']}–{returned[left]['max']}）、"
            f"`{right}` 均值 {returned[right]['mean']:.1f}"
            f"（{returned[right]['min']}–{returned[right]['max']}）；"
            f"两条路径返回篇数相同的查询 {returned['equal_length_queries']}/{group['queries']}。"
        )
        lines.append("")
        latency = group["latency_ms"]
        lines.append(
            f"**延迟**：`{left}` p50 {latency[left]['p50']:.0f} ms / p95 {latency[left]['p95']:.0f} ms；"
            f"`{right}` p50 {latency[right]['p50']:.0f} ms / p95 {latency[right]['p95']:.0f} ms；"
            f"p50 差 {latency['p50_delta_pct'] * 100:+.1f}%。"
        )
        lines.append("")
        lines.append("**按语言分组**（主指标）")
        lines.append("")
        lines.append("| 组 | n | 指标 | Δ | 95% CI | 判定 |")
        lines.append("|---|---|---|---|---|---|")
        for language, item in sorted(group["by_language"].items()):
            for name, metric in sorted(item["metrics"].items()):
                ci = metric["delta"]
                lines.append(
                    f"| {language} | {item['n']} | `{name}` | {ci['mean']:+.4f} | "
                    f"[{ci['lo']:+.4f}, {ci['hi']:+.4f}] | {metric['verdict']} |"
                )
        lines.append("")
    verdict = result["verdict"]
    lines.append("## 结论")
    lines.append("")
    lines.append(f"- 判定：**{verdict['headline']}**")
    for reason in verdict["reasons"]:
        lines.append(f"- {reason}")
    caveats = verdict.get("caveats") or []
    if caveats:
        lines.append("")
        lines.append("**非门槛但显著（改代码前先看这一栏）**")
        lines.append("")
        for caveat in caveats:
            lines.append(f"- {caveat}")
    lines.append("")
    lines.append(
        "> 注：NDCG@10 / recall@10 天然偏向更长的结果列表（native 折叠后能填满 `top_k`，"
        f"python 只聚合命中的那几十个 chunk ⇒ 常返回 1–5 篇）。判断排序质量看 `mrr`/`hit_rate@1`"
        "与**同深度**那一组。"
    )
    lines.append("")
    return "\n".join(lines)


def decide(
    result: dict[str, Any], judge_groups: Sequence[str], metric: str
) -> dict[str, Any]:
    """Is the **candidate** worse than the baseline? (Every group, not one.)

    The gate (plan §7 T-E5) is "no regression on the headline metrics", so a
    single group cannot decide it: the production path reranks, so ``hybrid|on``
    matters as much as the fusion-only ``hybrid|off``. Metrics outside
    :data:`PRIMARY_METRICS` are not judged but reported as caveats -- ``ndcg@1``
    is exactly where the two backends diverge in practice.
    """
    wanted = {group for group in judge_groups if group} or set()
    groups = [
        item
        for item in result["groups"]
        if (not wanted or item.get("group") in wanted) and not item.get("skipped")
    ]
    if not groups:
        return {
            "headline": f"无法判定（{'/'.join(sorted(wanted)) or '没有可比组'} 缺数据）",
            "ok": True,
            "reasons": [],
            "caveats": [],
        }

    candidate = BASELINE_BACKEND
    reasons: list[str] = []
    caveats: list[str] = []
    ok = True
    for group in groups:
        name_of_group = group["group"]
        candidate = group["right"]
        baseline = group["left"]
        for name in PRIMARY_METRICS:
            item = group["metrics"].get(name)
            if not item:
                continue
            ci = item["delta"]
            if ci["hi"] is not None and ci["hi"] < -REGRESSION_TOLERANCE:
                ok = False
                reasons.append(
                    f"**[{name_of_group}] `{name}` 回退**：Δ({candidate}−{baseline}) 的 CI 上界 "
                    f"{ci['hi']:+.4f} 低于 −{REGRESSION_TOLERANCE}。"
                )
            elif item["verdict"] == "indistinguishable":
                reasons.append(
                    f"[{name_of_group}] `{name}` CI 跨 0（[{ci['lo']:+.4f}, {ci['hi']:+.4f}]）"
                    "⇒ 当前样本量下无法区分。"
                )
            else:
                reasons.append(
                    f"[{name_of_group}] `{name}` {item['verdict']}"
                    f"（Δ {ci['mean']:+.4f}）。"
                )

        for name, item in sorted(group["metrics"].items()):
            if name in PRIMARY_METRICS or name.endswith("@K*"):
                continue
            ci = item["delta"]
            if ci["hi"] is not None and ci["hi"] < -REGRESSION_TOLERANCE:
                caveats.append(
                    f"[{name_of_group}] `{name}` 基线更好（Δ {ci['mean']:+.4f}，"
                    f"CI [{ci['lo']:+.4f}, {ci['hi']:+.4f}]）—— 非门槛指标，但说明"
                    f"{baseline} 在这里排序更靠前。"
                )
            elif ci["lo"] is not None and ci["lo"] > REGRESSION_TOLERANCE:
                caveats.append(
                    f"[{name_of_group}] `{name}` {candidate} 更好"
                    f"（Δ {ci['mean']:+.4f}，CI [{ci['lo']:+.4f}, {ci['hi']:+.4f}]）。"
                )

        slow = group["latency_ms"]["p50_delta_pct"]
        if slow > LATENCY_TOLERANCE:
            ok = False
            reasons.append(
                f"[{name_of_group}] p50 慢了 {slow * 100:+.1f}%（门槛 {LATENCY_TOLERANCE * 100:.0f}%）。"
            )
        else:
            reasons.append(f"[{name_of_group}] p50 差 {slow * 100:+.1f}%，在门槛内。")

        zh = (group["by_language"].get("zh") or {}).get("metrics") or {}
        if zh:
            widest = max(
                (item for item in zh.values()),
                key=lambda item: (item["delta"]["hi"] or 0) - (item["delta"]["lo"] or 0),
            )
            width = (widest["delta"]["hi"] or 0) - (widest["delta"]["lo"] or 0)
            reasons.append(
                f"[{name_of_group}] 中文组 n={group['by_language']['zh']['n']}：最大 CI 宽度 "
                f"{width:.3f}（一条查询翻盘即 0.1）⇒ 只作方向性参考，不作判据。"
            )

    headline = (
        f"{candidate} 未出现门槛级回退（{len(groups)} 组：{'、'.join(g['group'] for g in groups)}）"
        if ok
        else f"{candidate} 存在门槛级回退（{'、'.join(g['group'] for g in groups)}）"
    )
    return {
        "headline": headline,
        "ok": ok,
        "reasons": reasons,
        "caveats": caveats,
        "metric": metric,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--report",
        default=str(ROOT / "evals" / "report-m5-backend-ab.json"),
        help="eval report that ran both backends",
    )
    parser.add_argument(
        "--out", default=None, help="JSON output (default: <report stem>-compare.json)"
    )
    parser.add_argument("--markdown", default=None, help="Markdown output")
    parser.add_argument(
        "--judge-group",
        action="append",
        default=None,
        help="group the verdict is based on; repeatable (default: every comparable group)",
    )
    parser.add_argument(
        "--iterations", default=BOOTSTRAP_ITERATIONS, type=int, help="bootstrap resamples"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report_path = Path(args.report)
    if not report_path.is_file():
        raise SystemExit(f"report not found: {report_path}")
    report = load_json(report_path)
    languages = language_by_query(report)
    table = collect(report)
    if not table:
        raise SystemExit(
            "no per-query rows with a backend: run scripts/eval.py --backend "
            f"{','.join(BACKENDS)} first"
        )

    ks = list((report.get("params") or {}).get("k") or [1, 3, 10])
    groups = [
        compare_group(group, per_query, languages, ks)
        for group, per_query in sorted(table.items())
    ]
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(report_path),
        "report_params": report.get("params"),
        "bootstrap": {"iterations": args.iterations, "seed": BOOTSTRAP_SEED},
        "groups": groups,
    }
    result["verdict"] = decide(
        result, args.judge_group or [], ",".join(PRIMARY_METRICS)
    )

    out = Path(args.out) if args.out else report_path.with_name(
        report_path.stem + "-compare.json"
    )
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = Path(args.markdown) if args.markdown else out.with_suffix(".md")
    markdown.write_text(render_markdown(result), encoding="utf-8")
    print(f"wrote {out} and {markdown}")
    print(result["verdict"]["headline"])
    for reason in result["verdict"]["reasons"]:
        print(f"  - {reason}")
    return 0 if result["verdict"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
