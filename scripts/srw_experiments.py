#!/usr/bin/env python3
"""Run and collect SRW experiments, then cross-validate against ``scripts/eval.py`` (plan M4 / T-D3, T-D5, T-D6).

    uv run python scripts/srw_experiments.py run            # POINTWISE_EVALUATION matrix (4 configs x 3 query sets)
    uv run python scripts/srw_experiments.py run --optimizer  # HYBRID_OPTIMIZER sweep (per query set)
    uv run python scripts/srw_experiments.py collect          # aggregate metrics -> evals/report-srw.json|md
    uv run python scripts/srw_experiments.py compare           # cross-check vs evals/report-baseline.json

Matching pairs used by ``compare`` (SRW config ↔ production ``scripts/eval.py`` mode, rerank off):

    paperbox-bm25         <-> keyword
    paperbox-knn          <-> semantic
    paperbox-hybrid-rrf60 <-> hybrid        (both fuse BM25 + kNN with RRF rank_constant 60)

What the comparison does and does not claim: the two tools score **different
surfaces** — ``scripts/eval.py`` scores the live app over ``paper_chunks_v3`` with
chunk→paper aggregation, while SRW scores the synthetic ``paper_repr`` index
(one doc per paper). So the check is (a) per-query difficulty agreement
(Spearman on NDCG@10) and (b) config *ordering* agreement, never absolute equality.
Production numbers stay authoritative; SRW disagreements are logged for review.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402

MANIFEST = ROOT / "evals" / "srw" / "manifest.json"
REPORT_JSON = ROOT / "evals" / "report-srw.json"
REPORT_MD = ROOT / "evals" / "report-srw.md"
DEFAULT_BASELINE = ROOT / "evals" / "report-m4-baseline.json"
SRW = "/_plugins/_search_relevance"
EVAL_RESULT_INDEX = "search-relevance-evaluation-result"
VARIANT_INDEX = "search-relevance-experiment-variant"
POINTWISE_CONFIGS = ("bm25", "knn", "hybrid_minmax", "hybrid_rrf60")
SETS = ("all", "en", "zh")
METRIC = "NDCG@10"
TIMEOUT = 180.0


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def client() -> httpx.Client:
    return httpx.Client(base_url=settings.opensearch_url, timeout=TIMEOUT)


def call(cli: httpx.Client, method: str, path: str, body: Any | None = None) -> Any:
    resp = cli.request(method, path, json=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.content else {}


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def create_experiment(cli: httpx.Client, body: dict[str, Any]) -> str:
    created = call(cli, "PUT", f"{SRW}/experiments", body)
    return created["experiment_id"]


def wait_for(cli: httpx.Client, experiment_id: str, poll_s: float = 2.0, timeout_s: float = 900.0) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        doc = call(cli, "GET", f"{SRW}/experiments/{experiment_id}")
        src = (doc.get("hits", {}).get("hits") or [{}])[0].get("_source", {})
        if src.get("status") in ("COMPLETED", "FAILED", "ERROR"):
            return src
        time.sleep(poll_s)
    raise TimeoutError(f"experiment {experiment_id} 未在 {timeout_s}s 内结束")


def cmd_run(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    objects = manifest["srw_objects"]
    runs: list[dict[str, Any]] = []
    want_optimizer = args.optimizer or args.all
    with client() as cli:
        if not args.optimizer or args.all:
            for set_name in (SETS if args.sets == "all" else args.sets.split(",")):
                for cfg_name in POINTWISE_CONFIGS:
                    body = {
                        "name": f"srw-pointwise-{cfg_name}-{set_name}",
                        "description": f"pointwise: {cfg_name} on paper_repr, {set_name} judgments",
                        "type": "POINTWISE_EVALUATION",
                        "querySetId": objects["query_sets"][set_name],
                        "searchConfigurationList": [objects["search_configs"][cfg_name]],
                        "judgmentList": [objects["judgments"][set_name]],
                        "size": 10,
                    }
                    eid = create_experiment(cli, body)
                    doc = wait_for(cli, eid)
                    print(f"  点测 {cfg_name}/{set_name}: {doc.get('status')} ({len(doc.get('results') or [])} queries) id={eid}")
                    runs.append({"kind": "pointwise", "config": cfg_name, "set": set_name, "experiment_id": eid, "status": doc.get("status")})
        if want_optimizer:
            for set_name in (SETS if args.sets == "all" else args.sets.split(",")):
                body = {
                    "name": f"srw-optimizer-hybrid-{set_name}",
                    "description": "HYBRID_OPTIMIZER over normalization/combination/weights and rank_constant",
                    "type": "HYBRID_OPTIMIZER",
                    "querySetId": objects["query_sets"][set_name],
                    "searchConfigurationList": [objects["search_configs"]["hybrid_plain"]],
                    "judgmentList": [objects["judgments"][set_name]],
                    "size": 10,
                }
                eid = create_experiment(cli, body)
                doc = wait_for(cli, eid, poll_s=5.0, timeout_s=3600.0)
                print(f"  优化器 {set_name}: {doc.get('status')} id={eid}")
                runs.append({"kind": "optimizer", "config": "hybrid_plain", "set": set_name, "experiment_id": eid, "status": doc.get("status")})
    manifest["runs"] = (manifest.get("runs") or []) + runs
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {len(runs)} 个实验已登记到 {MANIFEST.relative_to(ROOT)}")
    print("下一步：uv run python scripts/srw_experiments.py collect")
    return 0


# --------------------------------------------------------------------------- #
# collect
# --------------------------------------------------------------------------- #
def scan(cli: httpx.Client, index: str, query: dict[str, Any], page: int = 1000) -> list[dict[str, Any]]:
    """Read every hit of ``index`` matching ``query`` via the scroll API.

    A plain ``?size=N`` silently truncates: the hybrid optimizer writes
    ``queries x combos`` result docs (3300 for the 50-query English set), and a
    capped fetch made per-combo samples look like ``n=33/50`` and entire
    parameter combinations disappear from the report. Scroll keeps the counts honest.
    """
    body = {"size": page, "query": query}
    first = call(cli, "POST", f"/{index}/_search?scroll=2m", body)
    hits = list(first["hits"]["hits"])
    total = first["hits"]["total"]["value"]
    scroll_id = first.get("_scroll_id")
    while scroll_id and len(hits) < total:
        page_res = call(
            cli, "POST", "/_search/scroll", {"scroll": "2m", "scroll_id": scroll_id}
        )
        batch = page_res["hits"]["hits"]
        scroll_id = page_res.get("_scroll_id") or scroll_id
        if not batch:
            break
        hits.extend(batch)
    if scroll_id:
        try:
            call(cli, "DELETE", "/_search/scroll", {"scroll_id": [scroll_id]})
        except Exception:  # 清理失败不影响结果
            pass
    if len(hits) != total:
        print(f"  ⚠ {index}: 期望 {total} 条、实取 {len(hits)} 条（scroll 未取全）")
    return hits


def fetch_experiment_rows(cli: httpx.Client, experiment_id: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Per-query rows (variant params + metrics, joined by result id) and variant status counts.

    A variant can be ``ERROR`` (remote-model pool exhaustion, etc.). Those produce no
    metrics, so a "best parameters" ranking computed over the survivors is biased —
    the caller records the counts next to the ranking.
    """
    variants = scan(cli, VARIANT_INDEX, {"term": {"experimentId": experiment_id}})
    statuses: dict[str, int] = {}
    for variant in variants:
        key = str(variant["_source"].get("status"))
        statuses[key] = statuses.get(key, 0) + 1
    result_ids = [
        v["_source"]["results"]["evaluationResultId"]
        for v in variants
        if (v["_source"].get("results") or {}).get("evaluationResultId")
    ]
    if not result_ids:
        return [], statuses
    results = scan(cli, EVAL_RESULT_INDEX, {"terms": {"id": result_ids}})
    by_id = {h["_source"]["id"]: h["_source"] for h in results}
    rows: list[dict[str, Any]] = []
    for variant in variants:
        src = variant["_source"]
        rsrc = by_id.get(src["results"]["evaluationResultId"])
        if not rsrc:
            continue
        rows.append(
            {
                "query": rsrc.get("searchText"),
                "status": src.get("status"),
                "parameters": src.get("parameters") or {},
                "metrics": {m["metric"]: m["value"] for m in (rsrc.get("metrics") or [])},
                "document_ids": rsrc.get("documentIds") or [],
            }
        )
    return rows, statuses


def query_set_texts(cli: httpx.Client, query_set_id: str) -> list[str]:
    doc = call(cli, "GET", f"{SRW}/query_sets/{query_set_id}")
    src = (doc.get("hits", {}).get("hits") or [{}])[0].get("_source", {})
    return [entry.get("queryText") for entry in (src.get("querySetQueries") or [])]


def summarise(rows: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    names = sorted({m for row in rows for m in row["metrics"]})
    for name in names:
        values = [row["metrics"][name] for row in rows if name in row["metrics"]]
        if values:
            out[name] = round(statistics.fmean(values), 4)
    out["queries"] = len(rows)
    return out


def cmd_collect(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    objects = manifest.get("srw_objects") or {}
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "index": "paper_repr (synthetic, one doc per paper)",
        "pointwise": {},
        "optimizer": {},
        "runs": manifest.get("runs") or [],
    }
    with client() as cli:
        for run in manifest.get("runs") or []:
            rows, statuses = fetch_experiment_rows(cli, run["experiment_id"])
            if not rows:
                print(f"  {run['kind']} {run['config']}/{run['set']}: 无结果行")
                continue
            if run["kind"] == "pointwise":
                key = f"{run['config']}|{run['set']}"
                expected = query_set_texts(cli, objects["query_sets"][run["set"]])
                seen = {row["query"] for row in rows}
                missing = [text for text in expected if text not in seen]
                block = {
                    "summary": summarise(rows),
                    "per_query": rows,
                    "queries_in_set": len(expected),
                    "queries_with_metrics": len(rows),
                    "no_result_queries": missing,
                }
                report["pointwise"][key] = block
                note = f"，{len(missing)} 条该配置零命中（无评测结果）" if missing else ""
                print(f"  点测 {key}: {METRIC}={block['summary'].get(METRIC)} (n={len(rows)}/{len(expected)}{note})")
            else:
                buckets: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    key = json.dumps(row["parameters"], sort_keys=True)
                    buckets.setdefault(key, []).append(row)
                expected_n = len(query_set_texts(cli, objects["query_sets"][run["set"]]))
                combos = [
                    {
                        "parameters": json.loads(key),
                        "summary": summarise(items),
                        "queries": len(items),
                        "queries_in_set": expected_n,
                        "incomplete": len(items) < expected_n,
                        "errors_for_this_combo": sum(
                            1
                            for r in rows
                            if json.dumps(r["parameters"], sort_keys=True) == key and r.get("status") == "ERROR"
                        ),
                    }
                    for key, items in buckets.items()
                ]
                combos.sort(key=lambda c: -(c["summary"].get(METRIC) or 0.0))
                report["optimizer"][run["set"]] = {
                    "combos": combos,
                    "n_combos": len(combos),
                    "variant_statuses": statuses,
                    "experiment_id": run["experiment_id"],
                }
                top = next((c for c in combos if not c["incomplete"]), combos[0] if combos else None)
                err = statuses.get("ERROR", 0)
                short = [c for c in combos if c["incomplete"]]
                print(
                    f"  优化器 {run['set']}: {len(combos)} 个参数组合（每组 {expected_n} 条查询）；变体状态 {statuses}；"
                    f"最佳完整组合 {METRIC}={top['summary'].get(METRIC) if top else None}"
                    + (f"  ⚠ {err} 个变体 ERROR（排名只基于幸存者，有选择偏差）" if err else "")
                    + (f"  ⚠ {len(short)} 个组合样本不全（该组有查询零命中）" if short else "")
                )
                if top:
                    print(f"    {json.dumps(top['parameters'], ensure_ascii=False)[:200]}")
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_MD.write_text(render_markdown(report), encoding="utf-8")
    print(f"\n写入 {REPORT_JSON.relative_to(ROOT)} 与 {REPORT_MD.relative_to(ROOT)}")
    return 0


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# SRW 评测旁路报告（Search Relevance Workbench）",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        "> 目标索引是**合成代表索引** `paper_repr`（每篇论文一文档，`_id = paper_id`）。",
        "> 它的用途是**相对比较**（配置排序、参数扫描），项目指标仍以 `scripts/eval.py` 为准。",
        "",
        "## 点测（POINTWISE_EVALUATION）",
        "",
        "| 配置 \\ 查询集 | NDCG@10 | MRR | MAP@10 | Recall@10 | n |",
        "|---|---|---|---|---|---|",
    ]
    for key in sorted(report["pointwise"]):
        s = report["pointwise"][key]["summary"]
        blk = report["pointwise"][key]
        lines.append(
            f"| {key.replace('|', ' / ')} | {s.get('NDCG@10')} | {s.get('MRR')} | {s.get('MAP@10')} | "
            f"{s.get('Recall@10')} | {blk.get('queries_with_metrics')}/{blk.get('queries_in_set')} |"
        )
    if report["optimizer"]:
        lines += ["", "## Hybrid optimizer（参数扫描 Top 5）", ""]
        for set_name, data in report["optimizer"].items():
            lines += [
                f"### 查询集 {set_name}（{data['n_combos']} 个组合）",
                "",
                "| # | NDCG@10 | MRR | 参数 |",
                "|---|---|---|---|",
            ]
            for i, combo in enumerate(data["combos"][:5], 1):
                lines.append(
                    f"| {i} | {combo['summary'].get('NDCG@10')} | {combo['summary'].get('MRR')} | "
                    f"`{json.dumps(combo['parameters'], ensure_ascii=False)}` |"
                )
            lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# compare (T-D3)
# --------------------------------------------------------------------------- #
def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation without scipy (ties get average ranks)."""
    if len(xs) < 3 or len(xs) != len(ys):
        return None

    def rank(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx, ry = rank(xs), rank(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return round(num / den, 3) if den else None


def cmd_compare(args: argparse.Namespace) -> int:
    if not REPORT_JSON.exists():
        raise SystemExit(f"先跑 collect（缺 {REPORT_JSON.relative_to(ROOT)}）")
    baseline_path = Path(args.baseline)
    if not baseline_path.is_absolute():
        baseline_path = ROOT / baseline_path
    if not baseline_path.exists():
        raise SystemExit(f"缺生产基线 {baseline_path}（uv run python scripts/eval.py --modes keyword,semantic,hybrid --rerank off）")
    srw = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    pairs = {"bm25": "keyword", "knn": "semantic", "hybrid_rrf60": "hybrid"}

    # 生产基线：per_query 行 -> {mode: {query_id: ndcg@10}}
    prod: dict[str, dict[str, float]] = {}
    for row in baseline.get("per_query") or []:
        mode = str(row.get("mode") or "")
        if row.get("rerank"):
            continue
        metric = (row.get("metrics") or {}).get("ndcg@10")
        if metric is None:
            continue
        prod.setdefault(mode, {})[str(row.get("query_id") or row.get("id"))] = float(metric)

    queries = {}
    for line in (ROOT / "evals" / "queries.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            queries[row["query"]] = row

    comparisons: list[dict[str, Any]] = []
    for srw_cfg, mode in pairs.items():
        key = f"{srw_cfg}|en"          # EN 子集：生产报告里区分语言靠 query_id 前缀 q/z
        block = srw["pointwise"].get(key)
        if not block:
            continue
        xs, ys, missing = [], [], 0
        for row in block["per_query"]:
            meta = queries.get(row["query"])
            if not meta or meta.get("language") != "en":
                continue
            qid = meta["id"]
            a = row["metrics"].get(METRIC)
            b = prod.get(mode, {}).get(qid)
            if a is None or b is None:
                missing += 1
                continue
            xs.append(float(a))
            ys.append(float(b))
        comparisons.append(
            {
                "srw_config": srw_cfg,
                "production_mode": mode,
                "n": len(xs),
                "spearman_ndcg10": spearman(xs, ys),
                "srw_mean_ndcg10": round(statistics.fmean(xs), 4) if xs else None,
                "production_mean_ndcg10": round(statistics.fmean(ys), 4) if ys else None,
                "missing": missing,
            }
        )

    # 配置排序一致性（SRW EN 均值排序 vs 生产 EN 均值排序）
    srw_order = sorted(
        (cfg for cfg in pairs if srw["pointwise"].get(f"{cfg}|en")),
        key=lambda cfg: -(srw["pointwise"][f"{cfg}|en"]["summary"].get(METRIC) or 0.0),
    )
    prod_env = {mode: statistics.fmean(v.values()) for mode, v in prod.items() if v}
    prod_order = sorted((pairs[cfg] for cfg in srw_order if pairs[cfg] in prod_env), key=lambda m: -prod_env[m])
    mapped_srw_order = [pairs[cfg] for cfg in srw_order if pairs.get(cfg) in prod_env]
    order_ok = mapped_srw_order == prod_order

    # 参照系：在同一工具 + 同一表面上，不同 mode 之间的逐查询相关性。
    # 用来判断「0.4x」到底是跨工具差异，还是这类评测里本来就有的常态。
    def en_series(mode: str) -> dict[str, float]:
        return {qid: v for qid, v in (prod.get(mode) or {}).items() if qid.startswith("q")}

    internal: list[dict[str, Any]] = []
    for left, right in (("keyword", "semantic"), ("keyword", "hybrid"), ("semantic", "hybrid")):
        a, b = en_series(left), en_series(right)
        shared = sorted(set(a) & set(b))
        internal.append(
            {
                "pair": f"{left}<->{right}",
                "n": len(shared),
                "spearman_ndcg10": spearman([a[q] for q in shared], [b[q] for q in shared]),
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metric": METRIC,
        "production_internal_spearman": internal,
        "baseline": str(baseline_path.relative_to(ROOT)) if baseline_path.is_relative_to(ROOT) else str(baseline_path),
        "subset": "en (50 queries)",
        "pairs": comparisons,
        "srw_config_order_en": srw_order,
        "production_mode_order_en": prod_order,
        "order_agrees": order_ok,
        "note": "两个工具评的是不同表面（生产=应用+chunk 聚合，SRW=paper_repr 合成索引），所以看的是趋势与排序一致性，绝对数值不等价。",
    }
    out = ROOT / "evals" / "srw" / "compare.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'SRW 配置':<16}{'生产 mode':<10}{'n':>4}{'Spearman':>10}{'SRW 均值':>10}{'生产均值':>10}")
    for row in comparisons:
        print(
            f"{row['srw_config']:<16}{row['production_mode']:<10}{row['n']:>4}"
            f"{str(row['spearman_ndcg10']):>10}{str(row['srw_mean_ndcg10']):>10}{str(row['production_mean_ndcg10']):>10}"
        )
    print("\n参照系（同一工具/同一表面内部，逐查询 NDCG@10 相关性）:")
    for row in internal:
        print(f"  {row['pair']:<22} n={row['n']:<4} Spearman={row['spearman_ndcg10']}")
    print(f"\nSRW 配置排序（EN）: {srw_order}")
    print(f"生产 mode 排序（EN）: {prod_order}")
    print(f"排序一致: {order_ok}")
    print(f"写入 {out.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="create + wait for experiments")
    run.add_argument("--optimizer", action="store_true", help="run HYBRID_OPTIMIZER instead of the pointwise matrix")
    run.add_argument("--all", action="store_true", help="pointwise matrix + optimizer")
    run.add_argument("--sets", default="all", help="comma separated subset of all,en,zh (default: all)")
    sub.add_parser("collect", help="aggregate metrics into evals/report-srw.json|md")
    cmp_parser = sub.add_parser("compare", help="cross-check against the production baseline")
    cmp_parser.add_argument("--baseline", default="evals/report-m4-baseline.json", help="production eval report to compare with")
    args = parser.parse_args()
    return {"run": cmd_run, "collect": cmd_collect, "compare": cmd_compare}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
