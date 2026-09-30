# docs/architecture/MVP-SPEC.md — paperbox 精简实现规范（codex 唯一实现依据）

> 仓库根目录 `.hermes/plans/2026-09-10_215600-paperbox-master-plan.md` 是权威需求文档（1343 行、英文 HTML）。本文件是其 **可执行摘要**：
> 所有实现以此为准，.hermes/plans/2026-09-10_215600-paperbox-master-plan.md 仅在字段语义有歧义时查阅。数据库模型已在
> `app/db/models.py` 全部就绪（alembic head 已应用到真实库），**不要改动已建表**（T7.3 追加的 `paper_degradations` 是新的增量表，见 `migrations/versions/c3f1a7d94e02_paper_degradations.py`）。
>
> **状态（2026-09-12）**：Phase 0–5 已全部实现并通过端到端验收；本文件保留作实现依据与
> 字段语义参考，但文中带「本阶段」的阶段化表述（如「返回 501」「暂不删 MinIO」）**均已过期**，
> **当前实际状态一律以 `docs/progress/project.md` 为准**。

## 0. 全局约定
- 所有 `/api/*` 路由除 `/health` 外都需要 `Authorization: Bearer <PAPER_API_KEY>`（`app/core/security.py` 已有实现）。
- `paper_id` = UUID 字符串。论文状态：`PENDING / PROCESSING / INDEXED / FAILED / DELETED`（软删除用 `deleted_at`）。
- 配置统一走 `app/core/config.py`（pydantic-settings），从根目录 `.env` 读取；禁止硬编码地址/密钥。
- 依赖服务地址（Windows 宿主经 `127.0.0.1` 直连 WSL 端口，需在 WSL 的 ufw 白名单内；不要写 `localhost`，IPv6 回环不转发会每次多等 8 秒）：
  - PG `POSTGRES_DSN`（127.0.0.1:5432）· OpenSearch `OPENSEARCH_URL`（http://127.0.0.1:9200，无安全认证，单节点）
  - MinIO `MINIO_ENDPOINT`（127.0.0.1:9000，bucket=`paperbox`）· Embedding `EMBEDDING_URL`（http://127.0.0.1:8090，POST /embed）
- 异步用 FastAPI `BackgroundTasks` + `ingestion_jobs` 表，**不用** Redis/Celery（MVP 范围，plan §3.2/§14）。

## 1. 已就绪模块（勿重写）
`app/core/{config,logging,security}.py`、`app/db/{session,models}.py`（九表，含
`papers.fingerprint` 的**部分唯一索引** `uq_papers_fingerprint_live (fingerprint) WHERE deleted_at IS NULL`、`paper_chunks`、`ingestion_jobs`、`paper_files` 的完整字段与
关系）、`app/services/object_storage.py`（minio 客户端 + `ensure_bucket`，对象路径
`papers/<paper_id>/original.pdf`，函数签名：`put_object(storage_key, data, content_type) /
get_object(storage_key) -> BytesIO / delete_object(storage_key) / bucket 存在性检查`）。

## 2. API 一览（对齐 plan §32，响应风格对齐 §34；**下列全部已实现**，实测 `/openapi.json`）
```
GET    /health
GET    /api/consistency             三端只读对账（PG / MinIO / OpenSearch）：逐篇核对文件行↔对象、
                                    chunk 行↔文档，报缺失/孤儿/删除残留；只读且永不抛（store 故障进 errors）（2026-09-22）
GET    /api/papers                  论文列表（分页 / 按状态 / 标题搜索；另支持 venue、year_from/year_to、
                                    paper_type、tag 过滤，读 PG 当前值）（2026-09-22）
POST   /api/papers/ingest           {"source_type":"url","source":"https://…/x.pdf"}
POST   /api/papers/ingest/file      multipart/form-data, 字段名 file（单文件，2026-09-19 起为 /ingest/files 的薄封装）
POST   /api/papers/ingest/files     multipart/form-data, 字段名 files 可重复 1..20（2026-09-19；逐文件结果 + 429 准入）
POST   /api/papers/ingest/dir       {"root","glob","recursive","limit","dry_run"}（2026-09-19；服务端读目录，零传输；白名单空→404、越界→403）
POST   /api/papers/ingest/compressed multipart/form-data, 字段名 file（2026-09-19；仅 zip，非 zip→415；zip-slip/zip bomb 防护）
GET    /api/jobs                    最近任务列表（limit/offset 分页 + stage/paper_id 过滤；回显窗口与 total）
GET    /api/jobs/queue              导入队列深度（concurrency / running / queued / queued_high / queued_low + 作业 id）
GET    /api/jobs/{job_id}           任务状态
GET    /api/papers/{paper_id}       论文元数据（含 authors/venue/files/status）
GET    /api/papers/{paper_id}/file  MinIO 原文流式返回（Content-Disposition: attachment）
GET    /api/papers/{paper_id}/chunks 论文 chunks 列表（分页）
GET    /api/papers/{paper_id}/degradations 该论文的降级留痕（T7.3；?include_resolved=true 连已消解的一起看）
DELETE /api/papers/{paper_id}       删除：先清 OpenSearch 文档与 MinIO 对象，再标记删除（204）；
                                    同时释放该论文占用的 paper_identifiers（标识符只属于一篇论文）
POST   /api/papers/{paper_id}/reindex 重建单篇索引
GET    /api/papers/{paper_id}/metadata      当前值 + 每字段来源与历史（2026-09-21）
PATCH  /api/papers/{paper_id}/metadata      单篇手动改元数据（写 decided_by='manual' 的 provenance）
POST   /api/papers/{paper_id}/metadata/rollback {"field","provenance_id"} 回滚某字段到历史主张
POST   /api/metadata/import         外部元数据导入（multipart file 或 JSON；IEEE raw / CSL-JSON / 通用）
GET    /api/metadata/review         复核清单（pending/ambiguous 来源 + 已登记的字段冲突）
POST   /api/metadata/sources/{source_id}/attach {"paper_id"} 人工归属一条来源记录
POST   /api/metadata/apply          按报告批量应用人工决定（mode=fill|overwrite）
POST   /api/search                  检索（keyword / semantic / hybrid）
```

**元数据端点约定（2026-09-21）**：`POST /api/metadata/import` 默认 **dry_run=true**（只匹配并报告，
不落库），只有 `apply=true`（或 `dry_run=false`）才写；`source_type` 默认 `import_file`，
可选 `ieee_api`/`arxiv_api`/`crossref`/`pdf_embedded`/`pdf_heuristic`/`manual`，非法值 → 422；
请求体不是 multipart/form-data 也不是 application/json → 415；JSON 解析失败 → 422。
重复导入同一记录（`UNIQUE(source_type, source_ref)`）计入报告 `unchanged`，不产生第二行来源。
`PATCH` 的未知字段在响应 `rejected` 里回显（不静默丢弃）；改 `doi`/`arxiv_id` 会替换标识符并升级指纹。
`rollback` 对不属于该论文/该字段的 `provenance_id` 返回 404。

**批量上传三入口的状态/错误约定（2026-09-19）**：`accepted`（建作业并入队）/ `duplicate`
（命中库内相同内容：不产生新论文、不写 staging，`/dir` 与 `/compressed` 不建作业）/ `rejected`
（逐文件失败，带 `error_code`，不影响同请求其它文件）。请求级错误：`422`（文件数超限、压缩包超上限）、
`413`（单请求总字节超限，未写 staging）、`415`（非 zip）、`403`（目录越界）、`404`（目录端点未启用）、
`429 + Retry-After`（在途上传超 `INGEST_UPLOAD_CONCURRENCY`，或多文件请求遇积压超
`INGEST_QUEUE_HIGH_WATERMARK`）。`/files` 单文件=交互优先级、≥2=批优先级，交互作业插队。

### GET /health 响应
```json
{"status":"ok","version":"0.1.0","services":{"postgres":"ok|error","opensearch":"ok|error","minio":"ok|error","embedding":"ok|error"}}
```
（各依赖做轻量探测，不可用时该字段为 `"error"`，整体 status 仍为 ok；embedding 探测 GET /health）

### POST /api/papers/ingest 逻辑（核心）
1. 校验 `source_type=url` 且 `source` 以 http(s) 开头；创建 `ingestion_jobs` 行 `stage=RECEIVED, progress=0`，
   随后 `mark_queued()` 置 `stage=QUEUED` 并入队（`app/workers/queue.py`，2026-09-19）。
2. 队列按 `INGEST_CONCURRENCY`（默认 2）派发，worker 执行 `run_ingestion_job(job_id)`（worker 函数放 `app/workers/tasks.py`）：
   - `stage=DOWNLOADING, progress=10`：httpx 下载（follow_redirects，timeout 120s，上限 100MB），记录 `GET /api/papers/ingest/file` 同理：读上传文件字节。
   - 计算 SHA256 → 去重：查 `papers.fingerprint` 或已有 `paper_files.sha256`：
     - 命中：`stage=COMPLETED, progress=100, paper_id=已存在论文`，响应与 job 均带上 `duplicate=true`（job 新加列 `error_message` 置空；去重结果写在 job 的 `paper_id`）。
     - 未命中：创建 `papers`（status=PENDING，title 先取文件名或 URL 尾部，abstract、year 等留空待解析阶段回填）、`paper_files`（storage_key=`papers/<paper_id>/original.pdf`）；上传 MinIO；`stage=STORED, progress=30`。
   - **本阶段 stop at STORED**（解析/embedding 是后续阶段）：更新 paper.status→`PENDING`，job `stage=COMPLETED, progress=100`。
   - 任何异常：job `stage=FAILED` + `error_message`；若已建 paper 则 `status=FAILED`。
3. 所有 DB 操作单事务内完成（同一 Session），异常回滚。
4. 响应立即返回：`{"job_id": "...", "paper_id": "…|null", "status": "RECEIVED|COMPLETED|DUPLICATE"}`

### GET /api/jobs/{job_id}
```json
{"job_id":"…","paper_id":"…|null","stage":"…","progress":0.0,"duplicate":false,
 "error_message":null,"created_at":"…","updated_at":"…"}
```

### GET /api/papers/{paper_id}
```json
{"paper_id":"…","title":"…","abstract":null,"language":null,"year":null,"doi":null,
 "arxiv_id":null,"url":"…","venue":null,"authors":["…"],"status":"PENDING",
 "fingerprint":"…","files":[{"storage_key":"papers/…/original.pdf","sha256":"…",
 "size_bytes":123,"mime_type":"application/pdf","url":"原始url|null"}],"created_at":"…","updated_at":"…"}
```
- 404：`{"detail":"paper not found"}`；软删除的论文也视为 404。

### DELETE /api/papers/{paper_id}
删除（2026-09-11 起为完整语义，见 `docs/progress/project.md`）：PG 取出该论文行 → 删 OpenSearch 全部 chunk 文档 → 删 MinIO `papers/<id>/` 全部对象 → 标记 `deleted_at=now` + `status=DELETED`，返回 204。两个清理步骤幂等；任一步失败返回 503 且**不标记删除**（可安全重试）。软删行保留原 fingerprint 用于追溯，但指纹已释放（不阻塞重导）。

## 3. 响应/错误约定
- 统一 `{"detail": "…"}` 错误体；文件过大/类型非 PDF（mime 或后缀）→ 422。
- 每篇论文新增 `paper_files` 行时 `file_type='original'`；`url` 列为来源 URL。

## 4. 单元测试（tests/）
- **注意**：规划中的 `tests/test_dedup.py` 从未创建——fingerprint 生成/去重与 metadata normalization 目前**无单测**（见 `docs/progress/project.md` §4 第 11 项）。实际测试文件见 `README.md` §7。
- 测试不得连真实数据库/服务（纯函数 + mock）。
- 运行：`uv run pytest`（tests 已有 `__init__.py`）。

## 5. 提交纪律
- 每完成一个可独立验证的里程碑立即 `git add -A && git commit`（Conventional Commits），
  例如「feat(ingest): url/file ingestion with dedup + jobs」。
- 不要提交 `.env`、`*.log`；
- 不要用 `Remove-Item / del / rm`（沙箱策略会拒绝并终止会话）；不需要清理任何临时文件。
## 6. PDF 解析与分块（Phase 2）
- `app/parsing/pdf.py`：`extract_pages(data: bytes) -> list[PageText]`（用 pypdf，1-based 页码 + 每页纯文本；解析失败抛 `PdfParseError`）
- `app/parsing/structure.py`：`detect_sections(pages) -> list[Section]`，识别标题行：`1 Introduction`、`2.1 Architecture`、`III-B. Circuit Design`、`Abstract`、`References`、`Conclusion` 等（正则 `^(\d+(\.\d+)*|[IVX]+(-[A-Z])?)[.)]?\s+\S` 或全大写短行）；每段带 `number/title/page_start/page_end`
- `app/parsing/chunking.py`：`chunk_document(pages, sections, target_tokens=400, overlap_tokens=48) -> list[Chunk]`
  - token 估算用 `max(1, len(text)//4)`；**必须 ≤450**（embedding 模型上限 512）
  - chunk 不跨 section；单个 section 超长时在段落边界继续切并带 overlap
  - 字段：`chunk_index, text, page_start, page_end, section, section_title, token_count, char_count`
- `tests/test_parsing.py`（规划名 `test_chunking.py`）：不跨 section、页码范围正确、token 目标 ±30%、overlap 生效、无 section/空文本兜底

## 7. Embedding + OpenSearch（Phase 3）
- `app/services/embedding_service.py`：`embed_texts(texts, batch_size=32, retries=2) -> list[list[float]]`，POST `{EMBEDDING_URL}/embed`，校验维度 1024，失败重试+退避；记录 `embedding_model/embedding_dimension`
- `app/search/mappings.py`：索引 `paper_chunks_v3`（当前生产索引；`dynamic: "strict"`；旧 `paper_chunks_v1`/`v2` 已于 2026-09-30 删除，`settings.index.knn=true`）；字段：`chunk_id/keyword, paper_id/keyword, title/text(cjk 分词器), authors/keyword, year/integer, venue/keyword, doi/keyword, arxiv_id/keyword, tags/keyword, section/keyword, section_title/text(cjk), page_start/integer, page_end/integer, chunk_index/integer, text/text(cjk), embedding/knn_vector(dim=1024, method=hnsw, space=l2, engine=lucene)`（文本字段用内置 `cjk` 分词器（analyzer + search_analyzer），中文 bigram、拉丁按词；换分词器必须新建索引，`scripts/create_index.py --index paper_chunks_v4 --migrate-from paper_chunks_v3` 服务端 `_reindex` 整批拷贝后原子切别名，不重新 embedding）
- `app/search/opensearch.py`：无认证客户端；`ensure_index()`（不存在则创建并把别名 `paper_chunks_current` 指向它）、`bulk_index_chunks(rows)`（每批 100~500，`refresh=true`）、`delete_by_paper_id(paper_id)`
- `scripts/create_index.py`（幂等建索引+别名）、`scripts/reindex.py`（全量：未删除论文 → 重解析 → chunk → embedding → bulk）；降级留痕（`paper_degradations`，T7.3）由流水线各阶段经同一个 sink 写入，重跑用 `scripts/reindex.py --degraded`
- 流水线延伸 `app/workers/tasks.py`：`STORED(30) → PARSING(45) → CHUNKING(60) → EMBEDDING(80) → INDEXING(95) → COMPLETED(100)`
  - 写 `paper_chunks` 表 **并** 同步 OpenSearch；`papers.status`: PENDING→PROCESSING→INDEXED（失败 FAILED）
  - 从解析结果回填：`title`（首页首个非空短行/最长标题行）、`abstract`（Abstract 段）、`year`（正文 `19\d\d|20\d\d` 众数）、`authors`（作者行按 `,`/`and` 拆分）、`arxiv_id`（若来源 URL 含 `arxiv.org/abs/<id>`）
- `DELETE /api/papers/{id}`：`delete_by_paper_id`（OpenSearch）+ 删 MinIO 对象（2026-09-11 修复，原「MVP 保留 MinIO」的安排已废弃）

## 8. 检索（Phase 4）
- `app/search/ranking.py`：`rrf_fuse(rank_lists: list[list[str]], k=60) -> list[tuple[str,float]]`
- `app/search/hybrid.py`：`search_chunks(query, mode, top_k, filters) -> list[ChunkHit]`
  - `keyword`：BM25 `multi_match`（`title^2`, `text`）
  - `semantic`：`knn` on `embedding`（k=top_k*3，先 filter 后 knn）
  - `hybrid`：两路各取 `top_k*5` 后 RRF（默认模式）
  - filters → bool filter：`year_from/year_to`(range)、`authors`(terms)、`venue`(terms)、`doi`(term)、`arxiv_id`(term)、`tag`(terms)；
    元数据快照字段（2026-09-22）：`venue_year`(terms，会议/期刊那一届的年份)、`paper_type`(terms)、
    `identifier`(terms，`scheme:value`，scheme 不在白名单→422)、按 `papers_tags.kind` 分列的
    `ieee_terms` / `author_terms` / `dynamic_index_terms` / `source_tags`(terms)
- `app/services/search_service.py`：论文级聚合 —— chunk 按 `paper_id` 分组，论文分 = 组内最高分，`relevance` 阈值 `high≥0.9 / medium≥0.6 / low`；每篇最多 3 条 evidence（`chunk_id/page/section/text` 截断 500 字）；按分排序取 top_k 篇
- `POST /api/search` 请求：`{query, mode(keyword|semantic|hybrid 默认), top_k(默认10,1..50), filters{year_from,year_to,authors,venue,doi,arxiv_id,tag,venue_year,paper_type,identifier,ieee_terms,author_terms,dynamic_index_terms,source_tags}, rerank(默认 false；true 时两阶段精排：候选扩大到 top_k*RERANK_CANDIDATES，交叉编码器重排后取 top_k*2，服务不可用**或超过 `RERANK_TIMEOUT`（默认 10s，多语言精排需 60s）**时降级为原顺序，此时响应 `rerank.model/took_ms` 与 `rerank_score` 均为 null), facets(默认 false；true 时**额外一次 `size:0` 聚合**，按**论文数**回显每个过滤键有哪些取值，见下)}`；响应：`{query(始终为原始值), rewritten_query(改写后的检索文本或 null), mode, total, candidates, facets, took_ms, rerank{enabled,model,took_ms}, rewrite{enabled,applied,model,took_ms}, results:[{paper_id,title,authors,year,doi,score,relevance,venue_year,paper_type,volume,issue,pages,publication_date,evidence:[{chunk_id,page,section,text}]}]}`（2026-09-22 起 results 从最佳 chunk 回显新元数据字段）。查询改写由服务端开关控制（`QUERY_REWRITE_ENABLED`，默认关闭）：开启时含 CJK 的 query 先改写成英文检索式再检索，LLM 不可达/超时降级为原查询（仍 200）
- **`facets=true` 的语义（2026-09-30，T-A3）**：多一次 `size: 0` 聚合，**体里不含查询腿**（BM25/kNN 都不带）—— facet 说的是"当前**过滤条件**下库里有什么"，因此不随 `top_k`/rerank/改写变化，并与 `GET /api/papers`（读 PG）的等价过滤计数逐桶一致。形状：`facets: {venue|paper_type|ieee_terms|author_terms|dynamic_index_terms|source_tags: [{key,count}], year: [{key,count}]}`（`year` 的 key 是字符串年份，可直接喂回 `year_from/year_to`；`terms` 是 top-N，上限 50）。**每个 count 是论文数**（`cardinality(paper_id)`），不是 chunk 数。没请求或聚合失败都是 `null`（失败只记 warning，不会把检索变成 503）。
- **过滤读索引快照，不读 PG**：改元数据不会自动改变检索过滤结果。只改元数据（不动分词器/模型）用
  `uv run python scripts/refresh_index_metadata.py`（批量改写快照，2883 文档实测秒级、不重算向量）；
  要连向量一起重算才用 `POST /api/papers/{id}/reindex`（≈1 chunk/s）
- `PaperOut`（`GET /api/papers`、`GET /api/papers/{id}`）2026-09-22 起增加
  `volume/issue/pages/publication_date/paper_type/venue_edition_id/venue_year`
- `tests/test_rrf.py`、`tests/test_filters.py`、`tests/test_aggregation.py`（纯函数断言，不连服务）
