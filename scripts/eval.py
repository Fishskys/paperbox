#!/usr/bin/env python3
"""Retrieval evaluation runner for the paperbox search API (SPEC-P1 section E).

Scores a *live* paperbox instance (real HTTP, never the internal retrieval
modules) against the query set and the relevance labels in ``evals/`` and
writes a JSON report plus a Markdown report:

    uv run python scripts/eval.py
    uv run python scripts/eval.py --modes hybrid --rerank both --k 1,3,5,10
    uv run python scripts/eval.py --out evals/report-baseline.json

The query set is ``evals/queries.jsonl`` (``{"id","query","language","note"}``)
and the labels are ``evals/labels.jsonl`` (``{"query_id","paper_id","grade"}``,
grade 2 = highly relevant, 1 = relevant). Metrics come from
``app.eval.metrics``: Hit Rate@K, Recall@K, MRR and NDCG@K, reported per
``mode|rerank`` group.

``--rerank both`` runs every query twice (``rerank=false`` and ``rerank=true``)
so the report shows whether the cross-encoder helps. A query that fails is
recorded with its ``error`` and does not abort the run; the tail of the run
prints how many queries succeeded and how many failed.

The run is read-only: it only issues ``POST /api/search``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.eval.metrics import (  # noqa: E402  (path bootstrap above)
    hit_rate_at_k,
    mrr,
    ndcg_at_k,
    recall_at_k,
)

ENV = ROOT / ".env"
DEFAULT_QUERIES = ROOT / "evals" / "queries.jsonl"
DEFAULT_LABELS = ROOT / "evals" / "labels.jsonl"
DEFAULT_BASE_URL = "http://127.0.0.1:8077"
DEFAULT_K = "1,3,5,10"
DEFAULT_TOP_K = 10
MODES = ("keyword", "semantic", "hybrid")
RERANK_MODES = ("both", "on", "off")
SEARCH_TIMEOUT = 60.0
PAPER_IDS_PER_ROW = 5


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
def load_env(path: Path = ENV) -> dict[str, str]:
    """Minimal ``KEY=VALUE`` reader (same convention as the other scripts)."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a ``.jsonl`` file, skipping blank lines."""
    rows: list[dict[str, Any]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: invalid JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{number}: expected a JSON object")
        rows.append(row)
    return rows


def load_queries(path: Path) -> list[dict[str, Any]]:
    queries = load_jsonl(path)
    for row in queries:
        if not row.get("id") or not row.get("query"):
            raise ValueError(f"{path}: every query needs 'id' and 'query'")
    return queries


def load_labels(path: Path) -> dict[str, dict[str, int]]:
    """Group labels by query id into ``{paper_id: grade}``."""
    labels: dict[str, dict[str, int]] = {}
    for row in load_jsonl(path):
        query_id = str(row.get("query_id") or "")
        paper_id = str(row.get("paper_id") or "")
        if not query_id or not paper_id:
            raise ValueError(f"{path}: every label needs 'query_id' and 'paper_id'")
        try:
            grade = int(row.get("grade", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: grade must be an integer ({exc})") from exc
        labels.setdefault(query_id, {})[paper_id] = grade
    return labels


def parse_k(raw: str) -> list[int]:
    values: list[int] = []
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid k: {chunk!r}") from exc
        if value <= 0:
            raise argparse.ArgumentTypeError(f"k must be positive, got {value}")
        if value not in values:
            values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("at least one k is required")
    return sorted(values)


def parse_modes(raw: str) -> list[str]:
    modes: list[str] = []
    for chunk in str(raw).split(","):
        chunk = chunk.strip().lower()
        if not chunk:
            continue
        if chunk not in MODES:
            raise argparse.ArgumentTypeError(
                f"unknown mode {chunk!r} (choose from {', '.join(MODES)})"
            )
        if chunk not in modes:
            modes.append(chunk)
    if not modes:
        raise argparse.ArgumentTypeError("at least one mode is required")
    return modes


def rerank_variants(raw: str) -> list[bool]:
    """``both`` -> ``[False, True]`` so the report shows the delta."""
    if raw == "on":
        return [True]
    if raw == "off":
        return [False]
    return [False, True]


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------- #
# search call
# --------------------------------------------------------------------------- #
def ranked_paper_ids(payload: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Collapse the API's paper-level results into best-first unique paper ids.

    ``POST /api/search`` already aggregates chunk hits per paper; this keeps the
    response order and de-duplicates defensively so a paper never occupies two
    ranks.
    """
    ids: list[str] = []
    seen: set[str] = set()
    results = payload.get("results") or []
    for item in results:
        if not isinstance(item, dict):
            continue
        paper_id = str(item.get("paper_id") or "")
        if not paper_id or paper_id in seen:
            continue
        seen.add(paper_id)
        ids.append(paper_id)
    return ids, [item for item in results if isinstance(item, dict)]


def search_once(
    client: httpx.Client,
    query: str,
    *,
    mode: str,
    rerank: bool,
    top_k: int,
) -> tuple[list[str], list[dict[str, Any]], int, dict[str, Any]]:
    """One ``POST /api/search`` call; returns ids, raw results, took_ms, payload."""
    body: dict[str, Any] = {
        "query": query,
        "mode": mode,
        "top_k": top_k,
        "rerank": rerank,
    }
    started = time.perf_counter()
    response = client.post("/api/search", json=body)
    took_ms = int((time.perf_counter() - started) * 1000)
    response.raise_for_status()
    payload = response.json()
    ids, results = ranked_paper_ids(payload)
    return ids, results, took_ms, payload


def score_query(
    ranked_ids: list[str], relevance: dict[str, int], ks: Iterable[int]
) -> dict[str, float]:
    """All four metrics; ``k``-suffixed names for the cut-off metrics."""
    metrics: dict[str, float] = {"mrr": mrr(ranked_ids, relevance)}
    for k in ks:
        metrics[f"hit_rate@{k}"] = hit_rate_at_k(ranked_ids, relevance, k)
        metrics[f"recall@{k}"] = recall_at_k(ranked_ids, relevance, k)
        metrics[f"ndcg@{k}"] = ndcg_at_k(ranked_ids, relevance, k)
    return metrics


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def group_summary(
    per_query: list[dict[str, Any]], metric_names: list[str]
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{"mode|rerank": {metric: {"mean", "n"}}}`` over successful queries."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in per_query:
        if row.get("error"):
            continue
        groups.setdefault(str(row["group"]), []).append(row)

    summary: dict[str, dict[str, dict[str, Any]]] = {}
    for group, rows in groups.items():
        observations: dict[str, list[float]] = {name: [] for name in metric_names}
        for row in rows:
            for name in metric_names:
                value = row["metrics"].get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                observations[name].append(float(value))
        summary[group] = {
            name: {
                "mean": (sum(values) / len(values)) if values else 0.0,
                "n": len(values),
            }
            for name, values in observations.items()
        }
    return summary


def format_markdown(report: dict[str, Any]) -> str:
    ks = report["params"]["k"]
    metric_names = report["params"]["metric_names"]
    rows = ["hit_rate", "recall", "ndcg"]

    lines = [
        "# paperbox 检索评测报告",
        "",
        f"- 生成时间：`{report['generated_at']}`",
        f"- 服务地址：`{report['base_url']}`",
        f"- top_k：`{report['top_k']}`；K：`{', '.join(str(k) for k in ks)}`",
        f"- 查询成功/失败：**{report['success']} / {report['failed']}**"
        f"（共 {report['total']} 行 = 查询 × mode × rerank）",
        "",
        "## 指标汇总（每个 mode|rerank 组的均值）",
        "",
    ]

    header = "| mode | rerank | metric | " + " | ".join(f"K={k}" for k in ks) + " | MRR |"
    lines.append(header)
    lines.append("|---|---|" + "---|" * (len(ks) + 2))
    for group in sorted(report["summary"]):
        mode, _, rerank = group.partition("|")
        means = report["summary"][group]
        for metric in rows:
            cells = []
            for k in ks:
                entry = means.get(f"{metric}@{k}")
                cells.append(
                    "-" if not entry or not entry["n"] else f"{entry['mean']:.3f}"
                )
            mrr_entry = means.get("mrr") or {"mean": 0.0, "n": 0}
            mrr_cell = "-" if not mrr_entry["n"] else f"{mrr_entry['mean']:.3f}"
            lines.append(
                f"| {mode} | {rerank} | {metric} | " + " | ".join(cells) + f" | {mrr_cell} |"
            )

    lines += [
        "",
        f"## 每查询明细（ranked 前 {PAPER_IDS_PER_ROW} 的 paper_id；✅=命中 / ❌=未命中）",
        "",
        "| query | mode | rerank | ranked top-5 | hit@1 | hit@5 | MRR | took_ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in report["per_query"]:
        if row.get("error"):
            lines.append(
                f"| {row['query_id']} | {row['mode']} | {row['rerank']} | "
                f"`error: {row['error']}` | - | - | - | {row['took_ms']} |"
            )
            continue
        relevance = row["relevance"]
        marks = []
        for paper_id in row["ranked_ids"][:PAPER_IDS_PER_ROW]:
            if paper_id in relevance and relevance[paper_id] > 0:
                marks.append(f"✅`{paper_id[:8]}`(g{relevance[paper_id]})")
            else:
                marks.append(f"❌`{paper_id[:8]}`")
        metrics = row["metrics"]
        lines.append(
            f"| {row['query_id']} | {row['mode']} | {row['rerank']} | "
            + (" ".join(marks) if marks else "-")
            + f" | {metrics.get('hit_rate@1', 0.0):.0f}"
            + f" | {metrics.get('hit_rate@5', 0.0):.0f}"
            + f" | {metrics.get('mrr', 0.0):.3f}"
            + f" | {row['took_ms']} |"
        )

    failed = [row for row in report["per_query"] if row.get("error")]
    if failed:
        lines += ["", "## 失败的查询", ""]
        for row in failed:
            lines.append(f"- `{row['query_id']}` ({row['mode']}/rerank={row['rerank']}): {row['error']}")

    lines.append("")
    return "\n".join(lines)


def format_console(report: dict[str, Any]) -> str:
    ks = report["params"]["k"]
    out: list[str] = []
    for group in sorted(report["summary"]):
        means = report["summary"][group]
        out.append(f"{group}")
        for metric in ("hit_rate", "recall", "ndcg"):
            cells = []
            for k in ks:
                entry = means.get(f"{metric}@{k}") or {"mean": 0.0, "n": 0}
                cells.append(f"K={k}: {entry['mean']:.3f} (n={entry['n']})")
            out.append(f"  {metric:<9} " + "  ".join(cells))
        mrr_entry = means.get("mrr") or {"mean": 0.0, "n": 0}
        out.append(f"  {'mrr':<9} {mrr_entry['mean']:.3f} (n={mrr_entry['n']})")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES), help="query set (.jsonl)")
    parser.add_argument("--labels", default=str(DEFAULT_LABELS), help="relevance labels (.jsonl)")
    parser.add_argument("--k", default=DEFAULT_K, type=parse_k, help="cut-offs, e.g. 1,3,5,10")
    parser.add_argument("--modes", default=",".join(MODES), type=parse_modes, help="keyword,semantic,hybrid")
    parser.add_argument("--rerank", default="both", choices=RERANK_MODES, help="both|on|off")
    parser.add_argument("--top-k", default=DEFAULT_TOP_K, type=int, help="top_k sent to POST /api/search")
    parser.add_argument("--out", default=None, help="JSON report path (default evals/report-<utc>.json)")
    parser.add_argument("--markdown", default=None, help="Markdown report path (default: same stem + .md)")
    parser.add_argument("--base-url", default=None, help="paperbox base url (default .env PAPER_API_BASE)")
    parser.add_argument("--api-key", default=None, help="bearer token (default .env PAPER_API_KEY)")
    parser.add_argument("--timeout", default=SEARCH_TIMEOUT, type=float, help="per-request timeout (s)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = load_env()

    base_url = (
        args.base_url
        or env.get("PAPER_API_BASE")
        or env.get("PAPER_API_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")
    api_key = args.api_key or env.get("PAPER_API_KEY") or ""

    queries_path = Path(args.queries)
    labels_path = Path(args.labels)
    if not queries_path.is_absolute():
        queries_path = (ROOT / queries_path) if not queries_path.exists() else queries_path
    if not labels_path.is_absolute():
        labels_path = (ROOT / labels_path) if not labels_path.exists() else labels_path

    queries = load_queries(queries_path)
    labels = load_labels(labels_path)

    stamp = _stamp()
    out_path = Path(args.out) if args.out else ROOT / "evals" / f"report-{stamp}.json"
    md_path = Path(args.markdown) if args.markdown else out_path.with_suffix(".md")

    ks: list[int] = args.k
    metric_names: list[str] = ["mrr"]
    for k in ks:
        metric_names += [f"hit_rate@{k}", f"recall@{k}", f"ndcg@{k}"]

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    per_query: list[dict[str, Any]] = []
    variants = rerank_variants(args.rerank)
    print(f"eval: {len(queries)} queries x {len(args.modes)} modes x {len(variants)} rerank -> {base_url}")

    with httpx.Client(base_url=base_url, headers=headers, timeout=args.timeout) as client:
        for query_row in queries:
            query_id = str(query_row["id"])
            query_text = str(query_row["query"])
            relevance = labels.get(query_id, {})
            for mode in args.modes:
                for rerank in variants:
                    row: dict[str, Any] = {
                        "query_id": query_id,
                        "query": query_text,
                        "mode": mode,
                        "rerank": rerank,
                        "group": f"{mode}|{'on' if rerank else 'off'}",
                        "ranked_ids": [],
                        "ranked_papers": [],
                        "metrics": {},
                        "took_ms": None,
                        "error": None,
                        "labels": relevance,
                        "relevance": relevance,
                    }
                    try:
                        ids, results, took_ms, _payload = search_once(
                            client,
                            query_text,
                            mode=mode,
                            rerank=rerank,
                            top_k=args.top_k,
                        )
                    except Exception as exc:  # noqa: BLE001 - one query must not kill the run
                        row["error"] = f"{type(exc).__name__}: {exc}"
                        row["metrics"] = score_query([], relevance, ks)
                        print(f"  FAIL {query_id} {mode} rerank={rerank}: {row['error']}")
                    else:
                        row["ranked_ids"] = ids
                        row["ranked_papers"] = [
                            {
                                "paper_id": str(item.get("paper_id") or ""),
                                "title": item.get("title"),
                                "score": item.get("score"),
                                "retrieval_score": item.get("retrieval_score"),
                                "rerank_score": item.get("rerank_score"),
                            }
                            for item in results
                        ]
                        row["took_ms"] = took_ms
                        row["metrics"] = score_query(ids, relevance, ks)
                    per_query.append(row)

    successes = [row for row in per_query if not row["error"]]
    failures = [row for row in per_query if row["error"]]
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "top_k": args.top_k,
        "total": len(per_query),
        "success": len(successes),
        "failed": len(failures),
        "params": {
            "k": ks,
            "modes": args.modes,
            "rerank": args.rerank,
            "rerank_variants": ["on" if v else "off" for v in variants],
            "queries": str(queries_path),
            "labels": str(labels_path),
            "metric_names": metric_names,
            "labelled_queries": len(labels),
        },
        "summary": group_summary(per_query, metric_names),
        "per_query": per_query,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(format_markdown(report), encoding="utf-8")

    print()
    print(format_console(report) or "(no successful queries)")
    print()
    print(f"success/failed: {len(successes)}/{len(failures)}")
    print(f"json: {out_path}")
    print(f"markdown: {md_path}")
    return 0 if successes else 1


if __name__ == "__main__":
    raise SystemExit(main())
