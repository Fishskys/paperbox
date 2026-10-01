#!/usr/bin/env python3
"""Search Relevance Workbench (SRW) side-path setup for paperbox (plan M4 / T-D1–T-D4).

Builds everything the SRW experiments need, idempotently:

    uv run python scripts/srw_setup.py status     # read-only inventory
    uv run python scripts/srw_setup.py build      # create/reuse every object
    uv run python scripts/srw_setup.py cleanup    # delete what this script created

What it creates (names are stable, so the script can find them again):

* cluster settings for ml-commons: ``connector.private_ip_enabled=true`` and the
  local endpoint appended to ``trusted_connector_endpoints_regex`` (default list
  is preserved verbatim — the setting *replaces* the list, it does not append).
* connector ``paperbox-fastembed-e5`` + remote model ``paperbox-e5-local``:
  forwards to the local fastembed service (``http://embedding:8090/v1/embeddings``,
  reachable container-to-container). No model is loaded inside OpenSearch, so the
  1 GB heap is untouched.
* index ``paper_repr``: **one document per paper**, ``_id = paper_id``, so SRW
  judgments (which are keyed by document id) can hold our paper-level labels
  one-to-one. Reason it exists: ``collapse(paper_id)`` was measured to return a
  *different* chunk id per query, so a fixed paper→chunk id map cannot work.
  Fields mirror production analyzers (``cjk`` text fields, knn_vector dim 1024,
  hnsw/l2/lucene) and the index is ``dynamic: strict`` like ``paper_chunks_v3``.
* query sets: ``paperbox-all-60`` / ``paperbox-en-50`` / ``paperbox-zh-10``
  (the EN/ZH split is what lets the side path speak to the int8 rerank finding).
* judgments: ``paperbox-labels-all`` / ``-en`` / ``-zh`` imported from
  ``evals/labels.jsonl`` (grade 2 = highly relevant, 1 = relevant).
* search configurations: ``paperbox-bm25``, ``paperbox-knn``,
  ``paperbox-hybrid-minmax``, ``paperbox-hybrid-rrf60``.
* search pipelines: ``paperbox-norm-minmax`` (normalization-processor,
  arithmetic_mean + min_max) and ``paperbox-rrf60`` (score-ranker-processor,
  rank_constant 60 — the OpenSearch-side twin of the app's ``rrf_fuse`` k=60).

The manifest of created ids lives in ``evals/srw/manifest.json`` (gitignored);
``cleanup`` reads it, so nothing is guessed or bulk-deleted. ``paper_repr`` is a
synthetic surface: use it for *relative* ordering/parameter comparisons only —
never as an absolute project metric (production numbers come from ``scripts/eval.py``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402  (path bootstrap above)
from app.db.session import SessionLocal  # noqa: E402
from app.services import embedding_service  # noqa: E402

QUERIES = ROOT / "evals" / "queries.jsonl"
LABELS = ROOT / "evals" / "labels.jsonl"
MANIFEST = ROOT / "evals" / "srw" / "manifest.json"

REPR_INDEX = "paper_repr"
CONNECTOR_NAME = "paperbox-fastembed-e5"
MODEL_NAME = "paperbox-e5-local"
FASTENDPOINT = "embedding:8090"
PIPELINE_MINMAX = "paperbox-norm-minmax"
PIPELINE_RRF = "paperbox-rrf60"
REPR_TEXT_CHUNKS = 3          # representative body = first N chunks by chunk_index
REPR_TEXT_CHARS = 1800        # ... capped, so title+abstract stay the dominant signal
SRW = "/_plugins/_search_relevance"
TIMEOUT = 120.0


# --------------------------------------------------------------------------- #
# tiny REST helper
# --------------------------------------------------------------------------- #
def _client() -> httpx.Client:
    return httpx.Client(base_url=settings.opensearch_url, timeout=TIMEOUT)


def _call(client: httpx.Client, method: str, path: str, body: Any | None = None) -> Any:
    """One REST call; raises ``RuntimeError`` with the server message on 4xx/5xx."""
    resp = client.request(method, path, json=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:400]}")
    return resp.json() if resp.content else {}


def _try(client: httpx.Client, method: str, path: str, body: Any | None = None) -> Any | None:
    """Same, but 404/400 returns None instead of raising (used for lookups)."""
    resp = client.request(method, path, json=body)
    if resp.status_code >= 400:
        return None
    return resp.json() if resp.content else {}


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def load_manifest() -> dict[str, Any]:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def save_manifest(data: dict[str, Any]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def stamp(key: str, value: Any) -> None:
    data = load_manifest()
    data[key] = value
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_manifest(data)


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
def load_queries() -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in QUERIES.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if not row.get("id") or not row.get("query"):
            raise ValueError(f"{QUERIES}: every row needs 'id' and 'query'")
    return rows


def load_labels() -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    for line in LABELS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        grouped.setdefault(str(row["query_id"]), {})[str(row["paper_id"])] = int(row["grade"])
    return grouped


# --------------------------------------------------------------------------- #
# 1) cluster settings (ml-commons needs to trust the local endpoint)
# --------------------------------------------------------------------------- #
def apply_cluster_settings(client: httpx.Client) -> dict[str, Any]:
    current = _call(client, "GET", "/_cluster/settings?include_defaults=true&flat_settings=true")
    key = "plugins.ml_commons.trusted_connector_endpoints_regex"
    # 已持久化过就出现在 persistent 里（defaults 只剩未被覆盖的键），两处都看
    existing = (current.get("persistent") or {}).get(key) or (current.get("defaults") or {}).get(key) or []
    ours = f"^http://{FASTENDPOINT}/.*$"
    wanted = list(existing) + ([ours] if ours not in existing else [])
    body = {
        "persistent": {
            "plugins.ml_commons.connector.private_ip_enabled": True,
            "plugins.ml_commons.trusted_connector_endpoints_regex": wanted,
        }
    }
    ack = _call(client, "PUT", "/_cluster/settings", body)
    result = {
        "applied": bool(ack.get("acknowledged")),
        "entries_before": len(existing),
        "entries_now": len(wanted),
        "local_pattern": ours,
    }
    stamp("cluster_settings", result)
    print(f"  ml-commons: private_ip_enabled=true, 端点正则 {len(existing)} -> {len(wanted)} 条（{ours}）")
    return result


# --------------------------------------------------------------------------- #
# 2) connector + remote model
# --------------------------------------------------------------------------- #
def connector_body() -> dict[str, Any]:
    # ml-commons rejects non-ASCII descriptions ("only letters, numbers, spaces,
    # and basic punctuation") and refuses an empty credential — both learned live.
    return {
        "name": CONNECTOR_NAME,
        "description": (
            "Local fastembed service, OpenAI-compatible /v1/embeddings at "
            f"{FASTENDPOINT}. No auth required. Credential is a placeholder."
        ),
        "version": "1",
        "protocol": "http",
        "parameters": {"endpoint": FASTENDPOINT, "model": settings.embedding_model},
        "credential": {"api_key": "local-no-auth"},
        # 默认 max_connection=30 / read_timeout=30 / max_retry_times=0 —— 优化器按查询数并发打
        # 远程模型时会把连接池打满，2026-10-01 实测 en 扫描 3300 变体里 1642 个 ERROR
        # （"Acquire operation took longer than the configured maximum time"）。这里放大池并加重试。
        "client_config": {
            "max_connection": 300,
            "connection_timeout": 30,
            "read_timeout": 600,
            "max_retry_times": 3,
            "retry_backoff_policy": "exponential_equal_jitter",
            "retry_backoff_millis": 500,
            "retry_timeout_seconds": 120,
        },
        "actions": [
            {
                "action_type": "predict",
                "method": "POST",
                "url": "http://${parameters.endpoint}/v1/embeddings",
                "headers": {"Content-Type": "application/json"},
                "request_body": '{ "input": ${parameters.input}, "model": "${parameters.model}" }',
                "pre_process_function": "connector.pre_process.openai.embedding",
                "post_process_function": "connector.post_process.openai.embedding",
            }
        ],
    }


def find_connector(client: httpx.Client) -> str | None:
    found = _try(
        client,
        "POST",
        "/_plugins/_ml/connectors/_search",
        {"size": 50, "query": {"term": {"name.keyword": CONNECTOR_NAME}}},
    )
    hits = ((found or {}).get("hits") or {}).get("hits") or []
    return hits[0]["_id"] if hits else None


def find_model(client: httpx.Client) -> str | None:
    found = _try(
        client,
        "POST",
        "/_plugins/_ml/models/_search",
        {"size": 50, "query": {"term": {"name.keyword": MODEL_NAME}}},
    )
    hits = ((found or {}).get("hits") or {}).get("hits") or []
    return hits[0]["_id"] if hits else None


def ensure_model(client: httpx.Client) -> str:
    cid = find_connector(client)
    if cid:
        print(f"  connector 复用: {cid}")
        existing = _call(client, "GET", f"/_plugins/_ml/connectors/{cid}")
        if existing.get("client_config") != connector_body()["client_config"]:
            # 改 connector 前必须先把用它的模型退服（否则 400：models are still using this connector）
            mid_old = find_model(client)
            if mid_old:
                _call(client, "POST", f"/_plugins/_ml/models/{mid_old}/_undeploy")
                import time as _t

                _t.sleep(3)
            _call(client, "PUT", f"/_plugins/_ml/connectors/{cid}", {"client_config": connector_body()["client_config"]})
            print("  connector client_config 已更新（模型将在下面重新部署）")
    else:
        created = _call(client, "POST", "/_plugins/_ml/connectors/_create", connector_body())
        cid = created["connector_id"]
        print(f"  connector 新建: {cid}")
    mid = find_model(client)
    if not mid:
        registered = _call(
            client,
            "POST",
            "/_plugins/_ml/models/_register",
            {
                "name": MODEL_NAME,
                "function_name": "remote",
                "description": "Local fastembed e5-large via remote connector. No model inside OpenSearch.",
                "connector_id": cid,
            },
        )
        mid = registered["model_id"]
    state = _call(client, "GET", f"/_plugins/_ml/models/{mid}")
    if state.get("model_state") != "DEPLOYED":
        task = _call(client, "POST", f"/_plugins/_ml/models/{mid}/_deploy")
        tid = task.get("task_id")
        for _ in range(60):
            st = _call(client, "GET", f"/_plugins/_ml/tasks/{tid}")
            if st.get("state") in ("COMPLETED", "FAILED"):
                break
            import time as _time

            _time.sleep(2)
        state = _call(client, "GET", f"/_plugins/_ml/models/{mid}")
    print(f"  model {mid}: state={state.get('model_state')}")
    stamp("connector_id", cid)
    stamp("model_id", mid)
    return mid


def probe_model(client: httpx.Client, mid: str) -> dict[str, Any]:
    """One predict call: proves the OS→fastembed hop works and the dim is right."""
    out = _call(client, "POST", f"/_plugins/_ml/models/{mid}/_predict", {"parameters": {"input": ["probe"]}})
    vec = out["inference_results"][0]["output"][0]["data"]
    info = {"dimension": len(vec), "expected": settings.embedding_dimension, "ok": len(vec) == settings.embedding_dimension}
    stamp("model_probe", info)
    print(f"  /_predict: dim={info['dimension']}（期望 {info['expected']}）-> {'OK' if info['ok'] else '不一致'}")
    return info


# --------------------------------------------------------------------------- #
# 3) representative index (one doc per paper, _id = paper_id)
# --------------------------------------------------------------------------- #
def repr_mapping() -> dict[str, Any]:
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "paper_id": {"type": "keyword"},
                "title": {"type": "text", "analyzer": "cjk", "search_analyzer": "cjk"},
                "abstract": {"type": "text", "analyzer": "cjk", "search_analyzer": "cjk"},
                "text": {"type": "text", "analyzer": "cjk", "search_analyzer": "cjk"},
                "year": {"type": "integer"},
                "arxiv_id": {"type": "keyword"},
                "chunk_count": {"type": "integer"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": settings.embedding_dimension,
                    "method": {"name": "hnsw", "space_type": "l2", "engine": "lucene"},
                },
            },
        },
    }


def collect_paper_rows(client: httpx.Client) -> list[dict[str, Any]]:
    """Paper metadata from PostgreSQL + representative body text from the chunk index."""
    with SessionLocal() as session:
        rows = session.execute(
            __import__("sqlalchemy").text(
                "SELECT id::text AS paper_id, title, abstract, year, arxiv_id "
                "FROM papers WHERE deleted_at IS NULL ORDER BY id"
            )
        ).all()
    papers = [
        {
            "paper_id": str(r.paper_id),
            "title": (r.title or "").strip(),
            "abstract": (r.abstract or "").strip(),
            "year": int(r.year) if r.year else None,
            "arxiv_id": (r.arxiv_id or "").strip() or None,
        }
        for r in rows
    ]
    # first N chunks per paper, in reading order, from the live chunk index
    chunks = _call(
        client,
        "POST",
        f"/{settings.opensearch_alias}/_search?size=2500",
        {
            "_source": ["paper_id", "chunk_index", "text"],
            "query": {"match_all": {}},
            "sort": [{"paper_id": "asc"}, {"chunk_index": "asc"}],
        },
    )["hits"]["hits"]
    per_paper: dict[str, list[str]] = {}
    for hit in chunks:
        src = hit["_source"]
        bucket = per_paper.setdefault(str(src["paper_id"]), [])
        if len(bucket) < REPR_TEXT_CHUNKS:
            bucket.append(str(src.get("text") or ""))
    for paper in papers:
        body = "\n\n".join(per_paper.get(paper["paper_id"], []))
        paper["text"] = body[:REPR_TEXT_CHARS]
        paper["chunk_count"] = len(per_paper.get(paper["paper_id"], []))
        paper["_chunks_total"] = None
    total = _call(client, "POST", f"/{settings.opensearch_alias}/_search?size=0", {"query": {"match_all": {}}})
    doc_counts: dict[str, int] = {}
    agg = _call(
        client,
        "POST",
        f"/{settings.opensearch_alias}/_search?size=0",
        {"query": {"match_all": {}}, "aggs": {"p": {"terms": {"field": "paper_id", "size": 100}}}},
    )
    for bucket in agg["aggregations"]["p"]["buckets"]:
        doc_counts[str(bucket["key"])] = int(bucket["doc_count"])
    for paper in papers:
        paper["_chunks_total"] = doc_counts.get(paper["paper_id"])
    print(f"  PG 论文 {len(papers)} 篇；每篇取前 {REPR_TEXT_CHUNKS} 个 chunk 作代表正文（≤{REPR_TEXT_CHARS} 字符）")
    print(f"  chunk 索引共 {total['hits']['total']['value']} docs")
    return papers


def build_repr_index(client: httpx.Client) -> int:
    exists = client.head(f"/{REPR_INDEX}").status_code == 200
    if not exists:
        _call(client, "PUT", f"/{REPR_INDEX}", repr_mapping())
        print(f"  索引 {REPR_INDEX} 新建")
    else:
        print(f"  索引 {REPR_INDEX} 已存在（先删后建，保证与 PG/向量一致）")
        _call(client, "DELETE", f"/{REPR_INDEX}")
        _call(client, "PUT", f"/{REPR_INDEX}", repr_mapping())
    papers = collect_paper_rows(client)
    # one embedding per paper over title+abstract (same e5 model as production)
    texts = [f"{p['title']}\n\n{p['abstract']}".strip() for p in papers]
    vectors = embedding_service.embed_texts(texts)
    embedding_service.validate_dimension(vectors, settings.embedding_dimension)

    lines: list[str] = []
    for paper, vector in zip(papers, vectors):
        doc = {
            "paper_id": paper["paper_id"],
            "title": paper["title"],
            "abstract": paper["abstract"],
            "text": paper["text"],
            "year": paper["year"],
            "arxiv_id": paper["arxiv_id"],
            "chunk_count": paper["_chunks_total"] or paper["chunk_count"],
            "embedding": vector,
        }
        lines.append(json.dumps({"index": {"_index": REPR_INDEX, "_id": paper["paper_id"]}}))
        lines.append(json.dumps(doc, ensure_ascii=False))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    resp = client.post("/_bulk?refresh=true", content=payload, headers={"Content-Type": "application/x-ndjson"})
    bulk = resp.json()
    if bulk.get("errors"):
        first = next(i for i in bulk["items"] if i.get("index", {}).get("error"))
        raise RuntimeError(f"bulk 写入失败: {json.dumps(first, ensure_ascii=False)[:400]}")
    count = _call(client, "POST", f"/{REPR_INDEX}/_count", {})["count"]
    stamp("repr_index", {"index": REPR_INDEX, "docs": count, "papers": len(papers)})
    print(f"  写入 {REPR_INDEX}: {count} docs（_id = paper_id）")
    return count


# --------------------------------------------------------------------------- #
# 4) query sets + judgments
# --------------------------------------------------------------------------- #
def find_by_name(client: httpx.Client, kind: str, name: str) -> dict[str, Any] | None:
    for hit in _call(client, "GET", f"{SRW}/{kind}/_search", {})["hits"]["hits"]:
        if hit["_source"].get("name") == name:
            return hit
    return None


def ensure_query_set(client: httpx.Client, name: str, description: str, queries: list[dict[str, Any]]) -> str:
    # 同名复用：SRW 拒绝删除「已被实验引用」的对象（实测 500），
    # 所以这里绝不 delete——要重建先跑 cleanup（它先删实验再删对象）。
    existing = find_by_name(client, "query_sets", name)
    if existing:
        count = len(existing["_source"].get("querySetQueries") or [])
        print(f"  查询集 {name}: 复用 {existing['_id']}（{count} 条）")
        return existing["_id"]
    created = _call(
        client,
        "PUT",  # PUT = 手工上传（POST 是 UBI 采样式建集，缺 ubi_queries 索引会 409）
        f"{SRW}/query_sets",
        {
            "name": name,
            "description": description,
            "sampling": "manual",
            "querySetQueries": [{"queryText": q["query"]} for q in queries],
        },
    )
    print(f"  查询集 {name}: {len(queries)} 条 -> {created['query_set_id']}")
    return created["query_set_id"]


def ensure_judgments(
    client: httpx.Client, name: str, description: str, queries: list[dict[str, Any]], labels: dict[str, dict[str, int]]
) -> str:
    existing = find_by_name(client, "judgments", name)
    if existing:
        lists = len(existing["_source"].get("judgmentRatings") or [])
        print(f"  判定 {name}: 复用 {existing['_id']}（{lists} 查询）")
        return existing["_id"]
    ratings = []
    pairs = 0
    for row in queries:
        graded = labels.get(row["id"]) or {}
        if not graded:
            continue
        ratings.append(
            {
                "query": row["query"],
                "ratings": [{"docId": pid, "rating": f"{grade:.3f}"} for pid, grade in graded.items()],
            }
        )
        pairs += len(graded)
    created = _call(
        client,
        "PUT",  # 判定导入是 PUT（本集群 POST -> 405，允许 [GET, PUT]）
        f"{SRW}/judgments",
        {"name": name, "description": description, "type": "IMPORT_JUDGMENT", "judgmentRatings": ratings},
    )
    print(f"  判定 {name}: {len(ratings)} 查询 / {pairs} 对 -> {created['judgment_id']}")
    return created["judgment_id"]


# --------------------------------------------------------------------------- #
# 5) search pipelines + search configurations
# --------------------------------------------------------------------------- #
def ensure_pipeline(client: httpx.Client, name: str, body: dict[str, Any]) -> None:
    _call(client, "PUT", f"/_search/pipeline/{name}", body)
    print(f"  管道 {name} 就绪")


def bm25_query() -> dict[str, Any]:
    return {
        "query": {
            "bool": {
                "should": [
                    {"match": {"title": {"query": "%SearchText%", "boost": 2.0}}},
                    {"match": {"abstract": {"query": "%SearchText%"}}},
                    {"match": {"text": {"query": "%SearchText%"}}},
                ]
            }
        },
        "_source": ["paper_id", "title", "year"],
        "size": 10,
    }


def knn_query(model_id: str) -> dict[str, Any]:
    return {
        "query": {"neural": {"embedding": {"query_text": "%SearchText%", "model_id": model_id, "k": 10}}},
        "_source": ["paper_id", "title", "year"],
        "size": 10,
    }


def hybrid_query(model_id: str) -> dict[str, Any]:
    return {
        "query": {
            "hybrid": {
                "queries": [
                    {
                        "bool": {
                            "should": [
                                {"match": {"title": {"query": "%SearchText%", "boost": 2.0}}},
                                {"match": {"abstract": {"query": "%SearchText%"}}},
                                {"match": {"text": {"query": "%SearchText%"}}},
                            ]
                        }
                    },
                    {"neural": {"embedding": {"query_text": "%SearchText%", "model_id": model_id, "k": 10}}},
                ]
            }
        },
        "_source": ["paper_id", "title", "year"],
        "size": 10,
    }


def ensure_search_config(client: httpx.Client, name: str, description: str, query: dict[str, Any], pipeline: str | None = None) -> str:
    wanted_query = json.dumps(query, ensure_ascii=False)
    existing = find_by_name(client, "search_configurations", name)
    if existing:
        src = existing["_source"]
        same = src.get("query") == wanted_query and (src.get("searchPipeline") or None) == pipeline and src.get("index") == REPR_INDEX
        print(f"  检索配置 {name}: 复用 {existing['_id']}{'' if same else '（注意：内容与当前代码不一致，需 cleanup 后重建）'}")
        return existing["_id"]
    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "query": json.dumps(query, ensure_ascii=False),
        "index": REPR_INDEX,
    }
    if pipeline:
        body["searchPipeline"] = pipeline
    created = _call(client, "PUT", f"{SRW}/search_configurations", body)
    print(f"  检索配置 {name}: 目标 {REPR_INDEX}{' + 管道 ' + pipeline if pipeline else ''} -> {created['search_configuration_id']}")
    return created["search_configuration_id"]


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_build(args: argparse.Namespace) -> int:
    with _client() as client:
        print("[1/6] ml-commons 集群设置")
        apply_cluster_settings(client)
        print("[2/6] connector + 远程嵌入模型")
        mid = ensure_model(client)
        probe_model(client, mid)
        print("[3/6] 代表索引 paper_repr")
        build_repr_index(client)
        print("[4/6] 查询集")
        queries = load_queries()
        labels = load_labels()
        qs_all = ensure_query_set(client, "paperbox-all-60", "paperbox 60 queries (50 EN + 10 ZH)", queries)
        qs_en = ensure_query_set(client, "paperbox-en-50", "paperbox English queries", [q for q in queries if q.get("language") == "en"])
        qs_zh = ensure_query_set(client, "paperbox-zh-10", "paperbox Chinese queries", [q for q in queries if q.get("language") == "zh"])
        print("[5/6] 判定（论文级标签 1:1 导入，_id = paper_id）")
        # 只为出现在该子集里的查询导入判定，避免 SRW 因未知 query 报错
        jd_all = ensure_judgments(client, "paperbox-labels-all", "paperbox human labels, paper level", queries, labels)
        jd_en = ensure_judgments(client, "paperbox-labels-en", "paperbox human labels, English", [q for q in queries if q.get("language") == "en"], labels)
        jd_zh = ensure_judgments(client, "paperbox-labels-zh", "paperbox human labels, Chinese", [q for q in queries if q.get("language") == "zh"], labels)
        print("[6/6] 检索管道与检索配置")
        ensure_pipeline(
            client,
            PIPELINE_MINMAX,
            {
                "description": "paperbox: hybrid score normalization (arithmetic_mean + min_max)",
                "phase_results_processors": [
                    {
                        "normalization-processor": {
                            "normalization": {"technique": "min_max"},
                            "combination": {"technique": "arithmetic_mean", "parameters": {"weights": [0.5, 0.5]}},
                        }
                    }
                ],
            },
        )
        ensure_pipeline(
            client,
            PIPELINE_RRF,
            {
                "description": "paperbox: hybrid rank fusion, rank_constant 60 (app-side rrf_fuse k=60 twin)",
                "phase_results_processors": [{"score-ranker-processor": {"combination": {"technique": "rrf", "rank_constant": 60}}}],
            },
        )
        cfg_bm25 = ensure_search_config(client, "paperbox-bm25", "BM25 only: title^2 + abstract + text", bm25_query())
        cfg_knn = ensure_search_config(client, "paperbox-knn", "kNN only via local e5 remote model", knn_query(mid))
        cfg_plain = ensure_search_config(
            client,
            "paperbox-hybrid-plain",
            "hybrid BM25+kNN without a pipeline (hybrid optimizer supplies the combination itself)",
            hybrid_query(mid),
        )
        cfg_minmax = ensure_search_config(client, "paperbox-hybrid-minmax", "hybrid BM25+kNN, arithmetic_mean/min_max", hybrid_query(mid), PIPELINE_MINMAX)
        cfg_rrf = ensure_search_config(client, "paperbox-hybrid-rrf60", "hybrid BM25+kNN, RRF rank_constant 60", hybrid_query(mid), PIPELINE_RRF)
        stamp(
            "srw_objects",
            {
                "query_sets": {"all": qs_all, "en": qs_en, "zh": qs_zh},
                "judgments": {"all": jd_all, "en": jd_en, "zh": jd_zh},
                "search_configs": {
                    "bm25": cfg_bm25,
                    "knn": cfg_knn,
                    "hybrid_plain": cfg_plain,
                    "hybrid_minmax": cfg_minmax,
                    "hybrid_rrf60": cfg_rrf,
                },
                "pipelines": [PIPELINE_MINMAX, PIPELINE_RRF],
            },
        )
        print("完成。下一步：uv run python scripts/srw_experiments.py run")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with _client() as client:
        print(f"OpenSearch: {settings.opensearch_url}")
        for kind in ("query_sets", "search_configurations", "judgments", "experiments"):
            hits = _call(client, "GET", f"{SRW}/{kind}/_search", {})["hits"]["hits"]
            print(f"  {kind}: {len(hits)} 个")
            for hit in hits:
                src = hit["_source"]
                extra = ""
                if kind == "query_sets":
                    extra = f" queries={len(src.get('querySetQueries') or [])}"
                elif kind == "judgments":
                    extra = f" rating_lists={len(src.get('judgmentRatings') or [])}"
                elif kind == "search_configurations":
                    extra = f" index={src.get('index')} pipeline={src.get('searchPipeline') or '-'}"
                print(f"    - {src.get('name')}{extra}")
        exists = client.head(f"/{REPR_INDEX}").status_code == 200
        count = _call(client, "POST", f"/{REPR_INDEX}/_count", {})["count"] if exists else 0
        print(f"  {REPR_INDEX}: exists={exists} docs={count}")
        model = find_model(client)
        state = _call(client, "GET", f"/_plugins/_ml/models/{model}").get("model_state") if model else None
        print(f"  remote model: {model} state={state}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    objects = manifest.get("srw_objects") or {}
    with _client() as client:
        # SRW 不允许删除被实验引用的对象 —— 先删实验（顺序不能反）
        for run in manifest.get("runs") or []:
            if _try(client, "DELETE", f"{SRW}/experiments/{run['experiment_id']}") is not None:
                print(f"  删除实验 {run['kind']} {run.get('config')}/{run.get('set')} {run['experiment_id']}")
        for kind, ids in (
            ("query_sets", (objects.get("query_sets") or {}).values()),
            ("judgments", (objects.get("judgments") or {}).values()),
            ("search_configurations", (objects.get("search_configs") or {}).values()),
        ):
            for oid in ids:
                if oid and _try(client, "DELETE", f"{SRW}/{kind}/{oid}") is not None:
                    print(f"  删除 {kind}/{oid}")
        for name in objects.get("pipelines") or []:
            if _try(client, "DELETE", f"/_search/pipeline/{name}") is not None:
                print(f"  删除管道 {name}")
        if args.with_model:
            mid = manifest.get("model_id")
            cid = manifest.get("connector_id")
            if mid:
                _try(client, "POST", f"/_plugins/_ml/models/{mid}/_undeploy")
                _try(client, "DELETE", f"/_plugins/_ml/models/{mid}")
                print(f"  删除远程模型 {mid}")
            if cid:
                _try(client, "DELETE", f"/_plugins/_ml/connectors/{cid}")
                print(f"  删除 connector {cid}")
        if args.with_index:
            if _try(client, "DELETE", f"/{REPR_INDEX}") is not None:
                print(f"  删除索引 {REPR_INDEX}")
    print("cleanup 完成（集群设置与 SRW 系统索引保持不动；两者都不影响 paperbox 检索）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="create/reuse every SRW object (idempotent)")
    sub.add_parser("status", help="read-only inventory of SRW objects")
    clean = sub.add_parser("cleanup", help="delete what this script created (uses the manifest)")
    clean.add_argument("--with-model", action="store_true", help="also undeploy/delete the remote model + connector")
    clean.add_argument("--with-index", action="store_true", help="also delete the paper_repr index")
    args = parser.parse_args()
    return {"build": cmd_build, "status": cmd_status, "cleanup": cmd_cleanup}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
