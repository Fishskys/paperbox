> **⚠️ 工具契约已迁移（2026-10-04）**：本文 §1 的 5 个工具定义与 §4 的"后续"已并入
> `docs/architecture/11-mcp-agent-interface.md`（MCP 工具契约真源，含参数/返回/权限/传输安全）。
> 本文保留**价值在于 §2 的调用范式**（中文查询怎么问、evidence 的 page/section 怎么引用），
> 工具 schema 与新增工具一律以 11 号文档为准，别在这里加新工具。

# Hermes 集成（plan §18 / §33）

Hermes 不直接连接 PostgreSQL / OpenSearch / MinIO，只通过 paperbox 的 REST API 拿事实。
本文件给出可直接使用的工具定义与调用范式；把下面的 schema 交给任何支持 function calling
的 Agent（或写成 Hermes 的 MCP server，见文末「后续」）即可获得这些能力。

- 基地址：`http://127.0.0.1:8077`（本机）；跨机时用运行 paperbox 的主机 IP
- 鉴权：`Authorization: Bearer <PAPER_API_KEY>`
- 所有响应 JSON；错误体统一 `{"detail": "..."}`

## 1. 工具定义

### paper_search

```json
{
  "name": "paper_search",
  "description": "Search the paper library by keyword and/or meaning. Returns papers with evidence passages (page + section) that can be cited.",
  "parameters": {
    "type": "object",
    "properties": {
      "query":     {"type": "string", "description": "natural language or keyword query (Chinese or English)"},
      "mode":      {"type": "string", "enum": ["hybrid", "keyword", "semantic"], "default": "hybrid"},
      "top_k":     {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
      "year_from": {"type": ["integer", "null"]},
      "year_to":   {"type": ["integer", "null"]},
      "authors":   {"type": ["array", "null"], "items": {"type": "string"}},
      "venue":     {"type": ["array", "null"], "items": {"type": "string"}},
      "doi":       {"type": ["string", "null"]},
      "arxiv_id":  {"type": ["string", "null"]},
      "tag":       {"type": ["array", "null"], "items": {"type": "string"}}
    },
    "required": ["query"]
  }
}
```

映射到 API：`POST /api/search`
`{"query","mode","top_k","filters":{"year_from","year_to","authors","venue","doi","arxiv_id","tag"}}`

输出（Hermes 直接可用 `evidence` 生成带出处的回答；顶层是 `results` 数组，不是 `papers`）：

```json
{"query":"...", "rewritten_query":null, "mode":"hybrid", "total":N, "took_ms":ms,
 "rerank": {"enabled":bool, "model":null, "took_ms":null},
 "rewrite": {"enabled":bool, "applied":bool, "model":null, "took_ms":null},
 "results": [{"paper_id","title","authors","year","doi","score","relevance",
              "evidence": [{"page","section","text"}]}]}
```

>`rewritten_query`/`rewrite` 与查询改写开关（`QUERY_REWRITE_ENABLED`，默认关闭）相关：
>服务端开启后，含 CJK 的查询会自动改写成英文再检索（响应里能看到实际检索文本，方便核对），
>纯英文查询零额外调用；LLM 不可达/超时时自动降级为原查询（`rewrite.applied=false`，仍 200）。

### paper_get

```json
{
  "name": "paper_get",
  "description": "Get full metadata of one paper by paper_id.",
  "parameters": {"type": "object", "properties": {"paper_id": {"type": "string"}}, "required": ["paper_id"]}
}
```

映射到 API：`GET /api/papers/{paper_id}`（软删除的论文返回 404）

### paper_ingest

```json
{
  "name": "paper_ingest",
  "description": "Add a paper from a PDF URL (asynchronous; returns a job_id).",
  "parameters": {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "direct PDF url, e.g. https://arxiv.org/pdf/1706.03762"}},
    "required": ["url"]
  }
}
```

映射到 API：`POST /api/papers/ingest` → `{"job_id","status":"RECEIVED"}`
用 `GET /api/jobs/{job_id}` 跟踪：`RECEIVED → DOWNLOADING → STORED → PARSING → CHUNKING →
EMBEDDING → INDEXING → COMPLETED`（异常为 `FAILED`，`error_message` 说明原因）。

本地文件走 `POST /api/papers/ingest/file`（multipart `file=`）。

### paper_get_chunks

```json
{
  "name": "paper_get_chunks",
  "description": "Get relevant content chunks of one paper (use after paper_search to drill into a specific paper).",
  "parameters": {
    "type": "object",
    "properties": {"paper_id": {"type": "string"},
                   "query": {"type": ["string", "null"]},
                   "top_k": {"type": "integer", "default": 5}},
    "required": ["paper_id"]
  }
}
```

当前实现：`GET /api/papers/{paper_id}/chunks` 返回该论文已索引的 chunk 列表；
带 `query` 时等价于 `POST /api/search` 加 `filters.arxiv_id`/`paper_id` 收敛到该篇
（见下方范式）。

### paper_get_file

```json
{
  "name": "paper_get_file",
  "description": "Stream the original PDF of a paper from object storage.",
  "parameters": {"type": "object", "properties": {"paper_id": {"type": "string"}}, "required": ["paper_id"]}
}
```

映射到 API：`GET /api/papers/{paper_id}/file`（返回 `application/pdf`，附件下载）

## 2. 推荐的调用范式（plan §19）

> **中文（或其他非英文）查询 —— 实测依据（`docs/progress/project.md` §11/§12）**：同一批 10 条中文查询，
> 原样检索 top-1 命中率 0.30；改写成英文检索式后 **0.90–1.00**（semantic 或 hybrid+rerank）。
> 中文查询的 bigram 在英文正文里不存在，BM25 无解；跨语言向量能召回（R@10 0.883）但排序精度不够。
>
> 两条等价的正确做法：
> 1. **服务端开关**（推荐，P1 I）：`.env` 设 `QUERY_REWRITE_ENABLED=true` +
>    `QUERY_REWRITE_URL`/`QUERY_REWRITE_API_KEY`/`QUERY_REWRITE_MODEL`，之后照常传中文 query，
>    服务端自动改写并在响应里回显 `rewritten_query`；Elastic 查询仍用 `query=中文原句`。
> 2. **Hermes 侧改写**：`POST /api/search` 前把用户的中文问题改写成一句英文检索式，作为 `query` 传入。
>    `mode` 用 `semantic`（实测最优）或 `hybrid` + `rerank=true`。适合不想给 paperbox 配 LLM key 的场景。

用户问：「找出 2022 年之后低功耗 FinFET SRAM 的代表性论文。」

```
Hermes
  → 中文问题改写为英文检索式 "low power FinFET SRAM 2022"
  → paper_search(query="low power FinFET SRAM", mode="hybrid", year_from=2022, top_k=10)
  ← {results:[{title, year, authors, score, relevance, evidence:[{page, section, text}]}]}
  → 需要细节时：paper_get_chunks(paper_id, query="leakage power", top_k=5)
  → 直接把 evidence 的 page/section 写进回答：「该结论见第 7 页 III-B 节」
```

Hermes 不需要知道 OpenSearch DSL、embedding 维度、bucket 名或数据库 schema。

## 3. 直接从命令行验证（Hermes 的 terminal 工具即可）

```bash
API_KEY=$(grep PAPER_API_KEY /d/hermes/paperbox/.env | cut -d= -f2)

curl -s -X POST http://127.0.0.1:8077/api/search \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"attention mechanism","mode":"hybrid","top_k":3}'

curl -s -X POST http://127.0.0.1:8077/api/papers/ingest \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"source_type":"url","source":"https://arxiv.org/pdf/1706.03762"}'
```

## 4. 后续（plan §30/§39 规划项，当前 MVP 不做）

- 把上面五个工具（`paper_search` / `paper_get` / `paper_ingest` / `paper_get_chunks` / `paper_get_file`）打包成 MCP server（stdio）后用 `hermes mcp add` 注册，使工具进入
  Hermes 的常驻工具集；在此之前用本文件的 REST 调用方式即可。
- Reranker、异步队列（Redis）、引用关系、AI 精读属于 plan 的 P1/P2。
