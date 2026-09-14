#!/usr/bin/env python3
"""End-to-end acceptance check for the paperbox MVP (plan section 38).

Runs against a *live* paperbox instance plus its real dependencies and prints one
line per acceptance criterion:

    uv run python scripts/acceptance.py                 # API at PAPER_API_HOST:PORT
    uv run python scripts/acceptance.py --api http://<server>:8077

Criteria (plan section 38):
  1 URL/PDF ingestion works (job reaches COMPLETED)
  2 the original PDF is in MinIO (streamed back through the API)
  3 metadata is in PostgreSQL (title/year/authors present)
  4 chunks carry page range + section
  5 every chunk has an embedding in OpenSearch
  6 OpenSearch answers both keyword and vector queries
  7 POST /api/search returns papers with evidence
  8 Hermes can call it over HTTP with the bearer token (this script is that call)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def load_env(path: Path) -> dict[str, str]:
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


class Checker:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, criterion: str, status: str, detail: str = "") -> None:
        self.rows.append((criterion, status, detail))
        print(f"[{status}] {criterion}" + (f" - {detail}" if detail else ""))

    def summary(self) -> int:
        failed = [row for row in self.rows if row[1] == FAIL]
        passed = sum(1 for row in self.rows if row[1] == PASS)
        print(f"\n{passed}/{len(self.rows)} criteria passed, {len(failed)} failed")
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=None, help="paperbox base url")
    parser.add_argument(
        "--url",
        default=None,
        help="PDF url to ingest when the library is empty (defaults to an arXiv paper)",
    )
    args = parser.parse_args()

    env = load_env(ENV)
    base = (args.api or f"http://127.0.0.1:{env.get('PAPER_API_PORT', '8077')}").rstrip("/")
    api_key = env.get("PAPER_API_KEY", "")
    os_url = env.get("OPENSEARCH_URL", "http://localhost:9200").rstrip("/")
    alias = env.get("OPENSEARCH_ALIAS", "paper_chunks_current")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    source_url = args.url or "https://arxiv.org/pdf/1706.03762"

    check = Checker()
    client = httpx.Client(base_url=base, headers=headers, timeout=180.0)

    health = client.get("/health").json()
    services = health.get("services", {})
    bad = [name for name, state in services.items() if state != "ok"]
    check.add(
        "0 dependencies healthy (/health)",
        PASS if not bad else FAIL,
        json.dumps(services),
    )

    # --- 1. ingestion -------------------------------------------------------
    job = client.post(
        "/api/papers/ingest",
        json={"source_type": "url", "source": source_url},
    ).json()
    job_id = job.get("job_id")
    status = job.get("status")
    deadline = time.time() + 900
    paper_id = job.get("paper_id")
    while job_id and time.time() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        status = payload.get("stage")
        paper_id = payload.get("paper_id") or paper_id
        if status in {"COMPLETED", "FAILED"}:
            break
        time.sleep(8)
    ok = status == "COMPLETED" and bool(paper_id)
    check.add(
        "1 URL/PDF ingestion reaches COMPLETED",
        PASS if ok else FAIL,
        f"job={job_id} stage={status} paper={paper_id}"
        + (" (duplicate of an existing paper)" if job.get("duplicate") else ""),
    )
    if not ok:
        return check.summary()

    # --- 2. original file in MinIO -----------------------------------------
    file_response = client.get(f"/api/papers/{paper_id}/file")
    body = file_response.content
    ok = (
        file_response.status_code == 200
        and body[:4] == b"%PDF"
        and len(body) > 1024
    )
    check.add(
        "2 original PDF stored and streamed back",
        PASS if ok else FAIL,
        f"HTTP {file_response.status_code}, {len(body)} bytes, magic={body[:4]!r}",
    )

    # --- 3. metadata in PostgreSQL -----------------------------------------
    paper = client.get(f"/api/papers/{paper_id}").json()
    authors = paper.get("authors") or []
    ok = bool(paper.get("title")) and bool(authors)
    check.add(
        "3 metadata in PostgreSQL",
        PASS if ok else FAIL,
        f"title={str(paper.get('title'))[:48]!r} year={paper.get('year')} "
        f"authors={len(authors)} status={paper.get('status')}",
    )

    # --- 4. chunks with page range + section -------------------------------
    chunk_payload = client.get(f"/api/papers/{paper_id}/chunks", params={"limit": 5}).json()
    chunks = chunk_payload.get("chunks") or []
    ok = bool(chunks) and all(
        chunk.get("page_start") and chunk.get("page_end") and chunk.get("section")
        for chunk in chunks
    )
    sample = chunks[0] if chunks else {}
    check.add(
        "4 chunks carry page range + section",
        PASS if ok else FAIL,
        f"total={chunk_payload.get('total')} sample=p{sample.get('page_start')}-"
        f"{sample.get('page_end')} section={sample.get('section')!r}",
    )

    # --- 5. embeddings present in OpenSearch -------------------------------
    try:
        count = httpx.get(
            f"{os_url}/{alias}/_count",
            params={"q": "embedding:*"},
            timeout=30,
        ).json()
        total = count.get("count", 0)
        all_count = httpx.get(f"{os_url}/{alias}/_count", timeout=30).json().get("count", 0)
        ok = all_count > 0 and total == all_count
        check.add(
            "5 every indexed chunk has an embedding",
            PASS if ok else FAIL,
            f"{total}/{all_count} documents with an embedding field",
        )
    except Exception as exc:  # noqa: BLE001
        check.add("5 every indexed chunk has an embedding", FAIL, str(exc))

    # --- 6. keyword + vector retrieval in OpenSearch -----------------------
    try:
        keyword = client.post(
            "/api/search", json={"query": "attention", "mode": "keyword", "top_k": 3}
        ).json()
        vector = client.post(
            "/api/search", json={"query": "attention", "mode": "semantic", "top_k": 3}
        ).json()
        ok = bool(keyword.get("results")) and bool(vector.get("results"))
        check.add(
            "6 OpenSearch answers keyword and vector queries",
            PASS if ok else FAIL,
            f"keyword={len(keyword.get('results', []))} papers, "
            f"semantic={len(vector.get('results', []))} papers",
        )
    except Exception as exc:  # noqa: BLE001
        check.add("6 OpenSearch answers keyword and vector queries", FAIL, str(exc))

    # --- 7. /api/search returns papers + evidence --------------------------
    response = client.post(
        "/api/search", json={"query": "neural network architecture", "mode": "hybrid", "top_k": 5}
    ).json()
    results = response.get("results") or []
    ok = bool(results) and all(
        item.get("paper_id") and item.get("title") and item.get("evidence")
        for item in results
    )
    first = results[0] if results else {}
    evidence = (first.get("evidence") or [{}])[0]
    check.add(
        "7 POST /api/search returns papers with evidence",
        PASS if ok else FAIL,
        f"{len(results)} papers, top={str(first.get('title'))[:40]!r} "
        f"score={first.get('score')} relevance={first.get('relevance')} "
        f"evidence p{evidence.get('page')} {str(evidence.get('section'))[:24]!r}",
    )

    # --- 8. bearer auth + Hermes-callable contract -------------------------
    unauthorized = httpx.post(
        f"{base}/api/search", json={"query": "x", "mode": "keyword", "top_k": 1}, timeout=30
    )
    ok = unauthorized.status_code == 401 and bool(results)
    check.add(
        "8 HTTP contract usable by Hermes (bearer auth enforced)",
        PASS if ok else FAIL,
        f"unauthenticated request -> HTTP {unauthorized.status_code}",
    )

    client.close()
    return check.summary()


if __name__ == "__main__":
    sys.exit(main())
