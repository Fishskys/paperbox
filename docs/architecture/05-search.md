# 检索（hybrid / RRF / 过滤 / 聚合）

| 项 | 内容 |
|---|---|
| 状态 | 依据 commit `54048a3` 的工作树实测（2026-09-22） |
| 关键文件 | `app/search/hybrid.py`、`app/search/ranking.py`、`app/search/opensearch.py`、`app/search/mappings.py`、`app/services/search_service.py`、`app/services/search_log_service.py`、`app/api/search.py`、`app/api/search_logs.py`、`app/schemas/search.py`、`app/schemas/search_log.py` |
| 相关文档 | `AGENTS.md` §3.5/§3.6、`docs/old/SPEC-P1-20260912.md`（H1/H2/B/D2/I1）、`docs/progress/project.md` §10–§13/§17、`docs/architecture/MVP-SPEC.md` §8、本目录 `06-rerank-rewrite.md`（精排/改写细节）、`01-storage.md`（索引与迁移） |

## 1. 职责边界（做什么 / 不做什么）

**做**：

- 把一条自然语言查询变成**论文级**结果：`POST /api/search`（`app/api/search.py:50`）。
- 三种 mode 的查询构造：BM25 `multi_match` / kNN / 两者的 RRF 融合（`app/search/hybrid.py:292`、`:306`、`:708`）。
- 元数据过滤器 → OpenSearch `filter` 子句（`app/search/hybrid.py:208`）：`year_from/year_to`、`authors`、`venue`、`doi`、
  `arxiv_id`、`tag`，以及元数据层带来的 `venue_year`、`paper_type`、`identifier`（`scheme:value`，未知 scheme → 422）、
  按 `papers_tags.kind` 分列的 `ieee_terms` / `author_terms` / `dynamic_index_terms` / `source_tags`（`hybrid.py:208-244`）。
- **元数据快照**：把一篇论文的元数据写成 chunk 文档上的可过滤字段（`app/search/snapshot.py:74`
  `paper_metadata_snapshot`）——流水线与 `scripts/refresh_index_metadata.py` **共用同一个函数**，杜绝两处写出的字段不一致。
- 论文级聚合：按 `paper_id` 分组、算论文分、选 evidence（`app/services/search_service.py:195`）。
- 检索日志落库与回读：`search_queries` 表 + `GET /api/search-logs`。
- 索引读写的别名不变式、索引创建/`_reindex`/别名切换的工具函数（`app/search/opensearch.py`）。

**不做**：

- 精排模型调用细节、候选窗内降级策略、查询改写的 prompt/模型选择 → 见 06 号文档；本文只写**调用点与响应字段**。
- PDF 解析/切块（03）、embedding 容器（04）、元数据合并与 provenance（08）。
- 分页偏移：请求体没有 `offset`/`page`/`from`，只有 `top_k`（`app/schemas/search.py:130`）。这是设计选择而非缺口，但客户端无法翻页。
- 过滤不走 PostgreSQL：所有过滤都在 OpenSearch 文档上做（见 §5 第 2 条）。

## 2. 关键文件与函数（文件 → 函数/类 → 作用，带行号）

| 文件 | 函数/类 | 作用 |
|---|---|---|
| `app/search/hybrid.py` | `ChunkHit` `:92` | 单个 chunk 命中（含各腿分数、`retrieval_score`、`rerank_score`） |
| | `build_filters` `:188` | 过滤器 → `filter` 子句列表（纯函数） |
| | `_with_filters` `:229` | 把查询包进 `bool.must + bool.filter` |
| | `build_keyword_query` `:248` | `multi_match` over `title^2` + `text`，`best_fields` |
| | `build_semantic_query` `:262` | `knn` 子句，`k` + 可选 `filter`（filter-first） |
| | `_search` `:280` | 一次 OpenSearch `search`（默认 `index=ALIAS`） |
| | `rank_hits` `:295` | 响应 → `(chunk_id, score, source)` 列表 |
| | `_keyword_hits` `:538` / `_semantic_hits` `:560` | 单腿执行，各自把分数写进 `keyword_score` / `semantic_score` |
| | `search_chunks` `:587` | 三模式分派 + 过取 + RRF + 精排 + telemetry |
| | `_first_stage_k` `:700` | 一阶段候选窗：`top_k`（不精排）或 `top_k × RERANK_CANDIDATES` |
| | `_apply_rerank` `:708` / `_normalize_rerank_scores` `:758` | 交叉编码器重排 + min-max 归一 |
| | `build_count_body` `:438` / `count_papers` `:496` | **2026-09-30**：`size: 0` + `cardinality(paper_id)` —— 响应 `total` 的真值来源（hybrid 用 `bool.should` 合并两腿） |
| | `build_facet_body` `:365` / `facet_counts` `:403` | **2026-09-30**：`facets=true` 的桶（6 个 `terms` + `year` 直方图，每桶 `cardinality(paper_id)`）；体里**没有**查询腿 |
| `app/search/ranking.py` | `DEFAULT_RRF_K` `:19`、`rrf_score` `:22`、`rrf_fuse` `:31` | 纯函数 RRF（支持每腿权重） |
| `app/search/opensearch.py` | `ALIAS` `:25` / `INDEX` `:27` | 别名与物理索引名（来自配置） |
| | `get_client` `:39` | 进程级客户端（无鉴权、无 TLS、timeout=60） |
| | `ensure_index` `:69` | 幂等建索引 + 把别名指过来 |
| | `build_reindex_body` `:113` / `build_alias_swap_body` `:122` / `alias_swap_is_safe` `:136` | 迁移三件套（服务端拷贝、原子切别名、计数校验） |
| | `build_chunk_document` `:141` / `bulk_index_chunks` `:176` / `delete_by_paper_id` `:238` | 写入路径 |
| `app/search/mappings.py` | `build_mapping` `:104` | 索引 body（分词器 + `knn_vector` + settings） |
| `app/services/search_service.py` | `aggregate_papers` `:195` | 论文级聚合（分/evidence/`matched_chunks`） |
| | `_select_evidence` `:151` | evidence 选取规则（噪声段降权） |
| | `normalize_scores` `:250` | 论文分归一到 0..1 并重算 `relevance` |
| | `search_papers` `:323`（`SearchOutcome` `:300`） | 对外入口：检索 + 聚合 + 归一，返回 `SearchOutcome{results, total, candidates}` |
| `app/services/search_log_service.py` | `serialize_results` `:47`、`log_search` `:100`、`list_search_logs` `:184` | 日志压缩 / 落库（从不抛错）/ 回读 |
| `app/api/search.py` | `search` `:50`、`_maybe_rewrite` `:165`、`serialize_results` `:178`、`_log_search` `:197` | 路由 + 改写门控 + 日志专用 session |
| `app/api/search_logs.py` | `list_search_logs` `:28` | `GET /api/search-logs` |
| `app/workers/tasks.py` | `_index_rows` `:1108` | chunk 文档的**唯一构造点**（元数据部分来自 `snapshot.paper_metadata_snapshot`，`tasks.py:1121`） |
| `app/search/snapshot.py` | `paper_metadata_snapshot` `:74`、`tag_names_by_kind` `:39` | 元数据快照的**单一来源**（流水线 + 刷新脚本共用） |
| `app/db/models.py` | `SearchQuery` `:691` | `search_queries` 表 |

## 3. 数据结构（表/字段/索引，或内存结构）

**索引映射**（`app/search/mappings.py:104`），物理索引名来自 `OPENSEARCH_INDEX`：

| 字段 | 类型 | 备注 |
|---|---|---|
| `title` / `section_title` / `text` | `text` | `analyzer` = `search_analyzer` = `cjk`（`mappings.py:55`） |
| `authors` / `venue` / `doi` / `arxiv_id` / `tags` / `section` / `chunk_id` / `paper_id` | `keyword` | 精确匹配（`mappings.py:61`） |
| `year` / `page_start` / `page_end` / `chunk_index` | `integer` | `year` 供 range 过滤 |
| `venue_year` | `integer` | 会议/期刊**那一届**的年份（≠ 论文 `year`）；terms/range 过滤（`mappings.py:104-177`） |
| `paper_type` / `volume` / `issue` / `pages` / `identifiers` | `keyword` | 元数据层新列；`identifiers` 是 `scheme:value` 串（如 `doi:10.1109/…`）（`mappings.py:104-177`） |
| `publication_date` | `date` | ISO 日期（`mappings.py:104-177`） |
| `ieee_terms` / `author_terms` / `dynamic_index_terms` / `source_tags` | `keyword` | 按 `papers_tags.kind` 分列的索引词；`tags` 仍是四者的并集（`mappings.py:96`） |
| `embedding` | `knn_vector(1024)` | `hnsw` / `l2` / `lucene`，`ef_construction=128`、`m=16`（`mappings.py:122`） |
| `embedding_model` / `embedding_dimension` / `created_at` | `keyword` / `integer` / `date` | |
| `parser_backend` / `parser_version` | `keyword` | **哪条解析器产出了这条 chunk**（`mappings.py:137-138`，值来自 `papers` 的两个戳列，`tasks.py:1179-1180`）。可以按它筛出「后端切换没覆盖到」的论文（`parser_backend: pypdf`）；同一个字段也是 `GET /api/consistency` 的 `parser_backends` 普查依据（加 `?parser_papers=true` 连论文 id 清单一起给，`scripts/reindex.py --parser-backend pypdf\|unknown` 直接吃这份清单） |

索引 settings：`index.knn=true`、1 shard、0 replica；**`dynamic: "strict"`**（`mappings.py:164`，2026-09-30 由 `true` 收紧）。原来是靠纪律：新过滤字段必须**赶在第一个带该字段的文档之前**加进 `build_mapping()`，否则 `true` 会先把它映成 `text`（`pages`/`paper_type` 都踩过），而且**不会报错**；`strict` 让这种漂移变成写入报错（真机验证：往 `paper_chunks_v3` 写未声明字段 → HTTP 400）。配套不变量：`tests/test_index_snapshot.py::test_every_field_the_document_emits_is_declared` 钉住「`build_chunk_document` 吐出的每个字段都在 mapping 里声明」，所以 strict 不会误伤正常写入。文档 `_id` = `chunk_id`（`opensearch.py:320`）。

**命中回传字段**：`SOURCE_FIELDS`（`hybrid.py:68`）显式列出 15 个字段，**不含 `embedding`**——向量不会被检出。

**内存结构**：

- `ChunkHit`（`hybrid.py:92`）：`chunk_id`/`paper_id`/`score` + 15 个元数据字段 + `keyword_score`/`semantic_score`/`rank`/`retrieval_score`/`rerank_score`；`page` 属性返回 `page_start`（`hybrid.py:128`）。
- `PaperResult` / `Evidence`（`search_service.py:52`、`:74`）：论文分、`relevance`、`evidence`、`matched_chunks`、`retrieval_score`、`rerank_score`。
- 融合中间态：`by_id: dict[str, ChunkHit]`（`hybrid.py:698`）先按 chunk 去重合并两腿分数，再按 RRF 顺序重建列表。

**`search_queries` 表**（`app/db/models.py:763`，索引 `created_at`、`mode`）：

| 列 | 类型 | 内容 |
|---|---|---|
| `id` | UUID PK | `new_uuid()` |
| `request_id` | `String(64)` | 请求 ID（无则 `uuid4().hex`，`api/search.py:217`） |
| `query` | `Text` | **原始**查询 |
| `rewritten_query` | `Text` | 实际用于检索的改写文本，未改写为 `NULL` |
| `mode` / `top_k` / `rerank` | `String(16)` / `Integer` / `Boolean` | 请求参数 |
| `filters` | `JSONB` | `SearchFilters.to_query_filters()` 的结果 |
| `candidates` / `returned` / `took_ms` | `Integer` | 候选数（见 §5 第 8 条）/ 返回论文数 / 整数毫秒 |
| `results` | `JSONB` | 压缩结果列表（`RESULT_FIELDS`，`search_log_service.py:32`） |
| `created_at` | `DateTime(tz)` | `server_default=now()` |

## 4. 调用链（从入口到落地，逐跳）

```
POST /api/search                                    app/api/search.py:50（路由挂载 app/main.py:88）
 ├─ SearchRequest 校验（mode 白名单、strip、top_k 1..50）  app/schemas/search.py:130
 ├─ request.filters.to_query_filters()              app/schemas/search.py:125
 ├─ asyncio.to_thread(_maybe_rewrite, query)        app/api/search.py:65 → :165
 │    └─ query_rewrite_service.rewrite_query()      （细节见 06；未启用时零外部调用）
 ├─ asyncio.to_thread(search_service.search_papers) app/api/search.py:81
 │    └─ search_chunks()                            app/services/search_service.py:356 → hybrid.py:637
 │         ├─ _first_stage_k(top_k, rerank)         hybrid.py:750
 │         ├─ keyword 腿：_keyword_hits             hybrid.py:588
 │         │    ├─ build_keyword_query              hybrid.py:292
 │         │    │    └─ build_filters + _with_filters  hybrid.py:208 / :273
 │         │    └─ _search → client.search          hybrid.py:527 → :535
 │         │         └─ rank_hits                   hybrid.py:542
 │         ├─ semantic 腿：_semantic_hits           hybrid.py:610
 │         │    ├─ embedding_service.embed_text     app/services/embedding_service.py:149（1 条文本）
 │         │    └─ build_semantic_query(k=..., filter=...)  hybrid.py:306
 │         ├─ 融合：rrf_fuse([kw_ids, sem_ids], k, weights)  hybrid.py:708 → ranking.py:31
 │         │    └─ 按融合序回填 by_id 与 hit.score   hybrid.py:713-719
 │         ├─ 精排（rerank=True）：_apply_rerank    hybrid.py:723 → :758
 │         │    ├─ rerank_service.rerank_texts      app/services/rerank_service.py:56
 │         │    └─ _normalize_rerank_scores         hybrid.py:808（min-max → 0..1）
 │         └─ telemetry{rerank_took_ms, reranked, candidates} + hit.rank  hybrid.py:727-733
 │    ├─ aggregate_papers(hits, top_k)              search_service.py:367 → :195
 │    │    └─ _select_evidence                      search_service.py:167
 │    └─ normalize_scores                           search_service.py:367 → :272
 ├─ 组装 SearchResult/SearchEvidence                app/api/search.py:111-140
 ├─ _log_search（独立 SessionLocal，失败吞掉）      app/api/search.py:197 → search_log_service.py:100
 │    └─ serialize_results（rank 从 1 起、截断到 SEARCH_LOG_RESULTS_LIMIT）  search_log_service.py:47
 └─ 返回 SearchResponse{query, rewritten_query, mode, total, candidates, took_ms, rerank, rewrite, results}
```

**快照刷新（不重算向量）**：`uv run python scripts/refresh_index_metadata.py` → `opensearch.update_mapping()`（`opensearch.py:145`，给活索引**加**新字段）→ `opensearch.bulk_update_documents()`（`opensearch.py:113`，每个 chunk 一条 partial update，不动 `embedding`/`text`）。2026-09-22 真机：2883 文档全部更新、0 失败。

**写入链（元数据快照的产生点）**：`POST /api/papers/{paper_id}/reindex`（`app/api/papers.py:345`）→ `job_queue.KIND_REINDEX` → `workers/tasks.py::_run_pipeline` → `_index_rows`（`:1004`）→ `opensearch.bulk_index_chunks`（`opensearch.py:364`，默认写 `ALIAS`）。

## 5. 不变量与踩过的坑

1. **过滤不参与打分**：过滤器一律进 `bool.filter`（`hybrid.py:273-284`）；kNN 模式下 filter 被塞进 `knn` 子句内部（`hybrid.py:313-315`，注释称 "filter first, then kNN"）。
2. **过滤字段是索引时快照**：文档里的 `venue`/`year`/`authors`/`doi`/`arxiv_id`/`tags`/`venue_year`/`paper_type`/卷期页/`publication_date`/`identifiers` 全部来自 `_index_rows` 里的 `paper` 对象（`tasks.py:1114-1148`），即索引那一刻 PG 的值；`build_filters` 只读文档（`hybrid.py:219-244`），**不查 PostgreSQL**。因此 `PATCH /metadata`、外部导入、合并**不会改变检索过滤结果**，必须 `POST /api/papers/{id}/reindex`（`docs/progress/project.md:767-769`，元数据真机验收第 5 项即先 reindex 再按 venue 命中，`docs/progress/project.md:753`）。同理 `tag` 过滤目前必然为空，因为 tag 写入路径本身未接通（`docs/progress/project.md:126`、`docs/progress/project.md:161`）。
3. **论文分取组内最大值**：`aggregate_papers` 先按 `_hit_score` 降序排组（`search_service.py:225`），再取 `ordered_group[0]` 的分（`:226`、`:241`）——是 max，不是加权和/平均；`matched_chunks` 单独记录组内 chunk 数（`:260`）。单测锁定：`tests/test_aggregation.py:93`。
4. **论文元数据也来自最佳 chunk**：`title`/`authors`/`year`/`venue`/`doi`/`arxiv_id` 都取 `best`（`search_service.py:245-252`，锁定于 `tests/test_aggregation.py:177`）。
5. **evidence 选取规则**：先过滤"噪声"（文本 `< MIN_EVIDENCE_CHARS` 或 `section/section_title` 命中 `NOISE_SECTION` 正则，`search_service.py:38`、`:180-185`），优先取非噪声、不足时用剩余项补齐，上限 `MAX_EVIDENCE=3`（`:187-191`）；每条文本截断到 `EVIDENCE_TEXT_LIMIT=500`（`:140`）。`evidence[].section` 优先取 `section_title`（`:232`），`page` 取 `page_start`。
6. **分数归一化会覆盖 relevance**：`aggregate_papers` 里 `relevance` 用**原始**分算（`:231`），随后 `normalize_scores` 把最高分设为 1.0 并重算 `relevance`（`:260-273`）——响应里的 `relevance` 是归一后口径（阈值 0.9/0.6，`:44-45`）。例外：已有 `rerank_score` 的论文**跳过**归一，直接沿用精排的 0..1 分与由此算出的 `relevance`（`:267-270`）。
7. **精排降级不报错**：`rerank_texts` 返回 `None` 时按一阶段顺序返回 `top_k*2`，`rerank_score` 保持 `None`，不抛异常（`hybrid.py:775-777`）；API 的 `rerank.model/took_ms` 也据"是否有论文带 `rerank_score`"决定是否为 `null`（`api/search.py:105-110`）。
8. **`top_k × RERANK_CANDIDATES` 是一阶段候选窗，但日志字段不是**：`_first_stage_k = top_k × RERANK_CANDIDATES`（默认 5，`hybrid.py:754`），hybrid 模式每条腿再乘 `CANDIDATE_MULTIPLIER=5`（`:691`）。而日志里的 `candidates` 恒为 `top_k × CANDIDATE_FACTOR(5)`（`api/search.py:47`、`:131`）且**只用于日志**，未传给检索——`rerank=true` 时它与真实候选池不符。
9. **`_semantic_hits` 的 k 是过取后的值**：`k = fetch_k × SEMANTIC_K_MULTIPLIER(3)`，同时作为 ES `size` 与 `knn.k` 传入（`hybrid.py:687-688`、`:625`），之后截回 `fetch_k`（`:689`）。代码里**没有 `num_candidates` 参数**（Lucene engine 只用 `k` + 可选 `filter`，`hybrid.py:312-315`）。
10. **空查询短路**：`search_chunks` 在 `strip()` 后为空时返回 `[]`，不报错（`hybrid.py:675-677`）；上层 schema 已用 `min_length=1` + strip 校验挡住（`schemas/search.py:157-163`）。
11. **别名是唯一读写入口**：`ALIAS`/`INDEX` 直接取配置（`opensearch.py:25-27`），`_search`/`bulk_index_chunks`/`delete_by_paper_id`/`index_stats` 默认都走 `ALIAS`。`is_write_index` 只在迁移的别名切换里设置（`opensearch.py:392`）；`ensure_index` 首次绑别名**不设**该属性（`:97`），单索引下仍可写入。
12. **`top_k` 有两套边界**：schema 限制 1..50（`schemas/search.py:42-43`），`search_chunks` 只要求 `> 0`（`hybrid.py:673`）；非法 mode 在 schema 与 `search_chunks` 两处各校验一次（`hybrid.py:672-673`）。
13. **`SearchError` 的 503 映射曾完全失效（2026-09-22 已修，有回归测试）**：`app/api/search.py:88` 捕获 `search_service.SearchError`，而类只定义在 `app/search/hybrid.py:87`——原先 `search_service` 只导入 `ChunkHit`，该 `except` 被触发时会先抛 `AttributeError`（**实测**：`uv run python -c "from app.services import search_service; search_service.SearchError"` → `AttributeError`），于是后端故障返回 **500** 而不是 503。修法：`app/services/search_service.py:28` 一并导入 `SearchError` 并加入 `__all__`；回归 `tests/test_search_api.py`（后端抛错 → 503、`ValueError` → 422、`search_service.SearchError is hybrid.SearchError`）。
14. ~~**`total` 语义与 docstring 不一致**~~ → **2026-09-30 已修（T-A2）**：原先 `search_papers` 的 docstring 说返回 "chunk candidate pool"，实现却返回 `len(results)`，API 直接当 `total`，于是 `total` = 被 `top_k` 截断后的论文数。现在是两个数：`total` = **本次查询 + 过滤条件下命中的论文数真值**（`hybrid.count_papers`：`size: 0` + `cardinality(paper_id)`，hybrid 模式用 `bool.should` 把关键词腿与 kNN 腿合起来数，否则向量独有命中会被漏掉；聚合在 `precision_threshold=3000` 以内是精确值，超过是 HLL 估计），`candidates` = 喂给论文聚合的 chunk 数。计数是**额外一次往返**（semantic/hybrid 还多一次 embedding），失败只记 warning 并退回候选池，绝不把能用的搜索变成 503（单测 `tests/test_search_total.py`）。
15. **`by_id` 合并顺序敏感**：先 `keyword_hits` 后 `semantic_hits` 用 `setdefault` 去重（`hybrid.py:698-704`），因此两腿都命中的 chunk 其**基础字段取自 keyword 腿**，语义腿只补 `semantic_score`。

15. **facet 数的是论文、而且与查询无关（2026-09-30，T-A3）**：`facets=true` 时额外一次 `size: 0` 聚合（`hybrid.py:365`），体里**不带 BM25 腿也不带 kNN 腿** —— facet 回答"当前过滤条件下库里有什么"，因此不随 `top_k`/rerank/查询改写变化，也才能与 `GET /api/papers`（读 PG）的等价过滤计数逐桶对上（真机 30 篇语料两侧全等，见 `docs/progress/project.md`）。**每个桶数论文**（`cardinality(paper_id)`）：索引是 chunk 文档，用桶自带的 `doc_count` 会变成"有多少 chunk 提到这个 venue"。桶上限 `FACET_SIZE=50`，`terms` 是 top-N，**超出部分是"没列"不是"没有"**。聚合失败只记 warning，响应 `facets=null`（与 `total` 同一套纪律；服务层用 `{}` 表示"问了但没算出来"，API 层把它转成 `null`，别让它读成"这个库一个 venue 都没有"）。

## 6. 配置项（键 → 默认值 → 作用 → 出处文件:行）

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `OPENSEARCH_URL` | `http://localhost:9200` | 集群地址（无鉴权/无 TLS） | `app/core/config.py:55` |
| `OPENSEARCH_INDEX` | `paper_chunks_v3` | 物理索引名（`INDEX`） | `config.py:56`；`opensearch.py:27` |
| `OPENSEARCH_ALIAS` | `paper_chunks_current` | 读写别名（`ALIAS`） | `config.py:57`；`opensearch.py:25` |
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | 文档/查询向量模型标记 | `config.py:70` |
| `EMBEDDING_DIMENSION` | `1024` | `knn_vector` 维度 | `config.py:71`；`mappings.py:124` |
| `RERANK_CANDIDATES` | `5` | 一阶段候选窗倍数 `top_k × N` | `config.py:86`；`hybrid.py:754` |
| `RRF_KEYWORD_WEIGHT` | `1.0` | keyword 腿在 RRF 中的权重（0 即废掉该腿） | `config.py:91`；`hybrid.py:711` |
| `RRF_SEMANTIC_WEIGHT` | `1.0` | semantic 腿权重 | `config.py:93`；`hybrid.py:711` |
| `SEARCH_LOG_ENABLED` | `true` | 关掉即不写 `search_queries` | `config.py:116`；`search_log_service.py:121` |
| `SEARCH_LOG_RESULTS_LIMIT` | `20` | 每行日志最多记多少条论文 | `config.py:117`；`search_log_service.py:138` |
| `QUERY_REWRITE_ENABLED` | `false` | 检索前改写开关（细节见 06） | `config.py:98`；`api/search.py:171` |

代码内常量（非环境变量）：

| 常量 | 值 | 作用 | 出处 |
|---|---|---|---|
| `TITLE_BOOST` | `2.0` | `title^2` | `hybrid.py:62` |
| `CANDIDATE_MULTIPLIER` | `5` | hybrid 每腿过取倍数 | `hybrid.py:64` |
| `SEMANTIC_K_MULTIPLIER` | `3` | 纯 semantic 模式的 kNN 过取倍数 | `hybrid.py:66` |
| `DEFAULT_RRF_K` | `60` | RRF 常数（`1/(k+rank+1)`） | `ranking.py:19`、`:28` |
| `MAX_EVIDENCE` / `EVIDENCE_TEXT_LIMIT` / `MIN_EVIDENCE_CHARS` | `3` / `500` / `200` | evidence 条数、文本长度、噪声阈值 | `search_service.py:31/32/35` |
| `HIGH_THRESHOLD` / `MEDIUM_THRESHOLD` | `0.9` / `0.6` | `relevance` 分档 | `search_service.py:44-45` |
| `MIN_TOP_K` / `MAX_TOP_K` | `1` / `50` | `top_k` 边界 | `schemas/search.py:42-43` |
| `CANDIDATE_FACTOR` | `5` | 日志 `candidates` 的系数 | `api/search.py:47` |
| `BULK_BATCH_SIZE` | `200` | 写入批量 | `opensearch.py:30` |
| `DEFAULT_LIMIT` / `MAX_LIMIT` | `50` / `200` | `GET /api/search-logs` 的 limit | `search_log_service.py:42-43` |

> 注意：配置默认索引与真机 `.env` 现均为 `paper_chunks_v3`（CJK bigram + `dynamic: "strict"`），别名 `paper_chunks_current` → v3；`v1`/`v2` 已于 2026-09-30 删除（`AGENTS.md` §3.5、`docs/progress/project.md:43`）。**以 `.env` 为准**。

## 7. 测试位置与覆盖（tests/xxx.py → 覆盖什么）

| 测试文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/test_filters.py` | 21 | 过滤器构造：year range 单边/双边/字符串强转/垃圾值丢弃、`terms`（authors/venue/tags）、`term`（doi/arxiv_id）、`tag` 与 `tags` 双写、标量作者、空值与空白条目、未知键忽略、`multi_match` 形状、filter 包进 `bool`、kNN `filter` 在子句内、无过滤时无 `filter` 键、非数值向量报错 |
| `tests/test_search_facets.py` | 18 | facet 聚合：`size:0` 且**不含查询腿**（纯函数断言）、每桶 `cardinality(paper_id)`、`year` 用直方图而非 terms、响应承诺的 facet 键与体里的聚合键一一对应、读数把 `key` 规范成字符串（`year` 的 `key_as_string`）并丢掉 0 论文的桶、`facets=False` 不发聚合、`facets=True` 透传 filters、**聚合抛错只 warning**（`facets={}`）而 `total` 照常、API 三条（默认 `null` / 有桶 / 失败 `null` 且 200） |
| `tests/test_rrf.py` | 11 | RRF 公式 `1/(61)`、双列表叠加、同顺位一致、空输入、单列表去重、降序与正分、非字符串 id 强转、`k` 改变尺度、`DEFAULT_RRF_K == 60`、非法参数 |
| `tests/test_aggregation.py` | 16 | 阈值分档、截断（500+3）、论文分 = 组内最大值、排序、`top_k` 限制、evidence ≤ 3 且取最强、evidence 字段形状、空 `paper_id` 跳过、元数据取自最佳 chunk、同分确定性 |
| `tests/test_index_migration.py` | 18 | CJK 分析器落在 `title`/`text`/`section_title`、keyword/integer/`knn_vector` 未被改动、`knn: true` 等 settings、`_reindex` body 无 script/无 `_source`（向量随文档搬）、别名切换 body 含 `is_write_index`、计数不等的安全闸、假客户端下 migrate 的 5 条路径（0/1/2 退出码、跳过拷贝、保留旧索引）、CLI 默认与覆盖 |
| `tests/test_search_log.py` | 19 | `serialize_results`：rank 从 1 起、按 limit 截断、非正 limit 不记录、跳过非 dict、字段集 == `RESULT_FIELDS`、`evidence_count` 三种来源、`retrieval_score` 回退到 `score`；`log_search` 在 DB 故障/异常下不抛且 rollback、关闭时不写、落库行内容与 `took_ms` 取整、按配置截断结果；`serialize_search_log` 的 datetime/None 透传 |
| `tests/test_search_api.py` | 3 | `POST /api/search` 的错误映射：后端抛 `SearchError` → 503 `search backend unavailable`（不是 500）、`ValueError` → 422、`search_service.SearchError` 必须存在（2026-09-22 回归） |
| `tests/test_index_snapshot.py` | 9 | 映射与文档形状快照：新字段类型（`venue_year` integer / `publication_date` date / 其余 keyword）、tag 四个 kind 分列、`identifiers` 形状、`embedding` 仍 1024 维 knn |
| `tests/test_refresh_index_metadata.py` | 13 | 快照刷新：每 chunk 一条 partial update（不含 `embedding`/`text`）、先 mapping 后文档、跳过软删与无 chunk 论文、`--dry-run`/`--paper-id`/`--no-mapping`、DISTINCT+ORDER BY 选择列覆盖 |

相邻但不属本模块主测：`tests/test_rerank.py`（25 例，含 `api/search.py::serialize_results`，`:369`）、`tests/test_query_rewrite.py`（28 例，含 `_maybe_rewrite` 门控，`:348`）。

## 8. 未做 / 已知缺口

1. **无 `search_chunks` 的模式分派单测**：RRF/过滤/聚合都是纯函数级测试（§7），但"三模式分派 + 过取倍数 + telemetry"没有直接单测，只被评测脚本（`scripts/eval.py`，`docs/progress/project.md:280`）间接跑到。
2. ~~**`total` 与文档口径不一致**~~ → 2026-09-30 修（见上一节第 14 条），且已由 `tests/test_search_total.py` 锁定字段语义。
3. ~~**`search_service.SearchError` 隐患**~~ **已修（2026-09-22）**：服务层现导出 `SearchError`（§5 第 13 条），端点级回归在 `tests/test_search_api.py`。
4. **日志 `candidates` 语义偏差**（§5 第 8 条）：`rerank=true` 时低估真实候选窗。
5. ~~**过滤键只能输入、不能回显有哪些取值**~~ → **2026-09-30 已做（T-A3）**：`facets=true`（§5 第 15 条）。仍未做的是**分页式 facet**：`terms` 取 top-N（`FACET_SIZE=50`），词表更大的库需要 composite 聚合 + `after_key` 续页。
6. **`MAX_RESULTS_LIMIT = 100` 定义后从未使用**（`search_log_service.py:44`、`:220` 仅导出）。
6. **检索日志无按 `id` 取单条的 HTTP 路由**：service 有 `get_search_log`（`:212`），但 `app/api/search_logs.py` 只有列表端点。
7. **`GET /api/search-logs` 无分页偏移**：只有 `limit`（1..200）/`since`/`mode`（`api/search_logs.py:29-31`、`search_log_service.py:194-196`），取不到第 200 条以后的历史。
8. **过滤不暴露 `section` / `paper_id` / `chunk_id`**：这些在映射里是 keyword 字段（`mappings.py:61`），但 `build_filters` 未生成对应子句（`hybrid.py:219-244`），`KEYWORD_FIELDS` 是映射层清单、不是 API 能力清单。
9. ~~**`tag` 过滤实际返回空**~~ **已随元数据层落地（2026-09-21 起）**：`papers_tags` 由元数据层写入，`_index_rows` 把四个 kind 快照进索引（`snapshot.py:39`），过滤现在能命中真实数据；但**改元数据后要刷新快照**（`scripts/refresh_index_metadata.py`）才可见。
10. **查询侧 e5 前缀（`query:`）未做**：`embed_text(query)` 直接送原文（`hybrid.py:619`），`docs/progress/project.md:§11` 把前缀列为未实施的备选方向。
11. **未知项（未确认）**：本会话未连真机，`knn` 参数在 Lucene engine 下的实际候选行为（是否隐式使用 `num_candidates`）、以及真机索引文档数与别名指向，均未实测；正文只断言代码里写了什么。
