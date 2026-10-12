# 两阶段精排与查询改写

| 项 | 内容 |
|---|---|
| 状态 | 依据 commit 54048a3 的工作树实测（2026-09-22） |
| 关键文件 | `app/services/rerank_service.py`、`app/services/query_rewrite_service.py`、`app/search/hybrid.py`（精排落地）、`app/services/search_service.py`、`app/api/search.py`、`app/schemas/search.py`、`app/core/config.py`、`infra/embedding/server.py` |
| 相关文档 | `docs/old/SPEC-P1-20260912.md`（D2/I1）、`AGENTS.md`（第 3.4 节/第 3.6 节）、`evals/report-jina-rerank-comparison.md`、`evals/report-jina-rerank.md`、`evals/report-zh-llm-rewrite.md`、`.env.example` |

## 1. 职责边界（做什么 / 不做什么）

| 组件 | 做 | 不做 |
|---|---|---|
| `rerank_service` | 把候选文本 + 查询 POST 给容器的 `/rerank`，取回 `(index, score)` 并按分排序；暴露 `is_available()` / `rerank_took_ms()` | 不排序论文、不截断结果集、不写数据库；不缓存；**任何失败都返回 `None`，绝不抛异常**（`rerank_service.py`） |
| `query_rewrite_service` | 把含 CJK 的查询发给 OpenAI 兼容 `/chat/completions`，清洗成单行英文检索式 | 不做多候选/多轮、不做查询扩展、不做结果缓存；失败一律降级为原查询（`query_rewrite_service.py`） |
| `hybrid._apply_rerank` | 用精排分重排候选、保留一阶段分到 `retrieval_score`、min-max 归一到 `score`、截断 `top_k*2` | 不直接决定最终论文列表（论文级聚合另有其人） |
| `api.search` | 改写门控、`rerank`/`rewrite` 响应块组装、耗时统计、搜索日志落库 | 不实现检索逻辑 |
| 容器 `infra/embedding/server.py` | 承载交叉编码器，按 `RERANK_MAX_BATCH` 分批推理，异常转 503 | 不做降级（降级在 app 侧） |

## 2. 关键文件与函数（文件 → 函数/类 → 作用）

| 文件 | 函数/类 | 作用 |
|---|---|---|
| `app/services/rerank_service.py` | `truncate_document(text, limit=MAX_DOCUMENT_CHARS)` | 单文档截断到 2000 字符（`MAX_DOCUMENT_CHARS`），索引不变 |
| 同上 | `rerank_texts(query, texts, top_n=None)` | 调用 `/rerank`；`RERANK_ENABLED=false` 时直接 `None`，空候选返回 `[]` |
| 同上 | `_parse_scores(body, expected)` | 严格校验：必须是 dict、有 `results`、条数等于输入数、`index` 为 int 且在界内、`score` 为数值；最后按分降序 |
| 同上 | `is_available()` | 探 `/health`，接受 `status=ok` 或含 `rerank_model`；**全仓无调用方**（仅 /` 出现） |
| 同上 | `rerank_took_ms(started)` | `perf_counter` 毫秒差 |
| `app/services/query_rewrite_service.py` | `needs_rewrite(query)` | 触发判定：非空、长度 ≤ `QUERY_REWRITE_MAX_CHARS`、命中 CJK 正则（覆盖 Han/假名/谚文） |
| 同上 | `rewrite_query(query)` | 调 LLM 并清洗；返回 `RewriteOutcome`（永不出错） |
| 同上 | `clean_rewrite(raw)` | 去成对引号、空白折叠为单空格、按 `MAX_CHARS` 截断 |
| 同上 | `_extract_content(payload)` | 取 `choices[0].message.content`，兼容网关的 `choices[0].text` |
| `app/search/hybrid.py` | `search_chunks(...)` `search_chunks()` | 三模式一阶段检索；`rerank=True` 时整体走两阶段（`search_chunks()`） |
| 同上 | `_first_stage_k(top_k, rerank)` | 候选窗口 = `top_k × RERANK_CANDIDATES`（缺省/非法值兜底为 1） |
| 同上 | `_apply_rerank(query, ordered, top_k)` | 调 `rerank_texts`、重排、补 `retrieval_score`、归一化、返回 `top_k*2` |
| 同上 | `_normalize_rerank_scores(hits)` | 交叉编码器 logits 在窗口内 min-max 到 0..1 写入 `score`（窗口平坦则全 1.0） |
| `app/services/search_service.py` | `search_papers(...)` | 组装 `search_chunks` + 论文聚合 + 归一化；`telemetry` 原地回填 |
| 同上 | `normalize_scores(results)` | `rerank_score` 非空时**跳过** RRF 重归一化，relevance 直接由现有 `score` 判定 |
| `app/api/search.py` | `search(request)` `search()` | 入口：改写 → 检索 → 响应块 |
| 同上 | `maybe_rewrite(query)` `app/services/search_pipeline.py` | 门控（开关 + `needs_rewrite`），关闭时零 HTTP 调用 |
| `app/schemas/search.py` | `SearchRerankInfo` / `SearchRewriteInfo` / `SearchResponse` | 响应契约 |
| `infra/embedding/server.py` | `rerank(req)` | 容器实现：按 `RERANK_MAX_BATCH` 分批并还原原始下标，经队列 `QUEUE.submit`、队满转 503、推理异常转 503，支持 `top_n` |

## 3. 数据结构

| 结构 | 位置 | 字段/语义 |
|---|---|---|
| `RerankScore`（frozen dataclass） | `rerank_service.py` | `index`（回指输入 `texts` 的下标）、`score`（float，原始 logits） |
| `RewriteOutcome`（frozen dataclass） | `query_rewrite_service.py` | `original`、`rewritten`、`applied`、`model`、`took_ms`、`reason`（`reason` 只进日志，不进响应） |
| `ChunkHit.retrieval_score` / `.rerank_score` | `hybrid.py` / | 前者为一阶段分（精排时才回填）、后者为归一化前的交叉编码器分；未精排时为 `None` |
| 排序稳定性内存结构 | `hybrid.py` | `order = {id(hit): position}`，用于精排同分时回退一阶段次序 |
| `search_queries` 表 | `app/db/models.py` | `query`、`rewritten_query`、`rerank`、`candidates`、`results`（JSONB，含双分数）；索引在 `created_at`/`mode` |

响应字段语义（`app/services/search_pipeline.py` 的 `to_response()`、`app/schemas/search.py`）：

| 字段 | 语义 |
|---|---|
| `query` | 调用方原样查询，永不被改写覆盖（`app/schemas/search.py`） |
| `rewritten_query` | 本次实际用于检索的英文检索式；未改写时为 `null`（`app/services/search_pipeline.py`） |
| `rewrite{enabled}` | 服务端 `QUERY_REWRITE_ENABLED`，与本次是否改写无关（`app/services/search_pipeline.py`、`app/schemas/search.py`） |
| `rewrite{applied,model,took_ms}` | 仅当本次真的改写才填；失败/命中即跳过时 `applied=false`、其余 `null`（`app/services/search_pipeline.py`） |
| `rerank{enabled}` | 服务端 `RERANK_ENABLED`（`app/services/search_pipeline.py`） |
| `rerank{model,took_ms}` | 仅当**结果集中至少有一条带 `rerank_score`** 才非空（`app/services/search_pipeline.py`）；降级或未开启时为 `null` |
| `result.retrieval_score` | 一阶段 BM25/kNN/RRF 分；未精排时为 `null`（`app/schemas/search.py`） |
| `result.rerank_score` | 该论文组内最佳归一化精排分；该组无任何被打分 chunk 时为 `null`（`search_service.py`） |
| `result.score` | 精排生效时=归一化精排分（0..1）；否则=RFF 分按本次最佳值归一（`search_service.py`） |
| `total` | = 返回的论文条数，不是 chunk 候选池大小（`search_service.py`） |

## 4. 调用链（从入口到落地，逐跳）

1. `POST /api/search` → `api.search.search`；`rerank` 请求字段默认 `false`（`app/schemas/search.py`）。
2. `_maybe_rewrite(query)`（跑在 `asyncio.to_thread` 里）→ 开关关或 `needs_rewrite()` 为假即返回透传 `RewriteOutcome`→ 否则 `query_rewrite_service.rewrite_query`。
3. `retrieval_query = rewritten if applied else request.query`→ `RewriteInfo` 组装。
4. `search_service.search_papers(retrieval_query, ..., rerank=request.rerank, telemetry=...)`（`clean_rewrite()`）。
5. → `hybrid.search_chunks(...)`（`search_service.py`）→ `_first_stage_k(top_k, rerank)`（`hybrid.py`）：非精排 = `top_k`，精排 = `top_k × RERANK_CANDIDATES`。
6. 一阶段：`keyword` → `_keyword_hits`；`semantic` → `_semantic_hits`（k 再 ×3）；`hybrid` → 两腿各取候选 ×5 后 `rrf_fuse`（`hybrid.py`）。
7. `rerank=True` → `_apply_rerank(query, ordered, top_k)`（`app/search/hybrid.py`）→ `rerank_service.rerank_texts`（`app/search/hybrid.py`，**不传 `top_n`**）→ `httpx.post(RERANK_URL + "/rerank")`（`rerank_service.py`，超时 `RERANK_TIMEOUT`）→ 容器 `rerank()` 分批推理（`infra/embedding/server.py`）→ `_parse_scores` 校验并降序（`rerank_service.py`）。
8. 回程：按 `rerank_score` 降序、同分回退一阶段位次（`hybrid.py`）；未被容器打分的 hit 追加到窗口尾部；`_normalize_rerank_scores`→ 截断 `top_k*2`；`telemetry` 回填；`hit.rank` 赋值与 `chunk search finished` 日志。
9. `aggregate_papers(hits, top_k=top_k)`（`search_service.py`）→ `normalize_scores`→ `api.search` 组装 `SearchResult`/`SearchResponse`（`app/services/search_pipeline.py`）→ `_log_search` 落库（含 `rewritten_query`）。

**顺序结论（以代码为准）：查询改写 → 一阶段检索 → 精排 → 论文级聚合 → 分归一化**。改写发生在 `search_papers` 调用之前（`app/services/search_pipeline.py` 先于 `app/services/search_pipeline.py`），所以精排看到的是一阶段用**改写后**查询召回的结果，精排的 `query` 参数也是改写后的文本（`hybrid.py` 的 `query` 即 `retrieval_query`）。

## 5. 不变量与踩过的坑

| 项 | 事实 | 证据 |
|---|---|---|
| 候选数 | `top_k × RERANK_CANDIDATES`（默认 5）；`top_k=10` → 50 条；三种模式共用同一窗口 | `hybrid.py`；`tests/test_rerank.py` 的 `test_rerank_on_over_fetches_top_k_times_candidates()` |
| 截断 | 精排后保留 `top_k × 2`，论文级聚合再收到 `top_k` | `hybrid.py`；`tests/test_rerank.py`（top_k=3 返回 6） |
| 截断（native） | 折叠流按**篇**计数：`top_k` 篇（不是 `top_k` 个 chunk），精排后仍取 `top_k` 篇 | `hybrid.py`；`tests/test_native_hybrid.py` |
| 精排池（native） | 每篇最多 `RERANK_CANDIDATES` 个 chunk 进交叉编码器（**与 `MAX_EVIDENCE` 解耦**），论文分取该篇最佳 chunk | `hybrid.py`；`tests/test_native_hybrid.py` |
| 不丢候选 | 容器没打分的 hit 不消失，按原序追加在精排名单之后 | `hybrid.py` |
| 双分数 | 一阶段分保留在 `retrieval_score`（含未被精排的 hit），`score` 被归一化精排分覆盖 | `hybrid.py`、|
| 归一化边界 | 窗口内 max→1.0、min→0.0；只有一条或全平坦时全部为 1.0 → `relevance` 全 `high` | `hybrid.py`、`search_service.py` |
| 降级语义 | `rerank_texts` 返回 `None` → 原序原分返回、`rerank_score` 保持 `None`、`rerank.model`/`rerank.took_ms` 为 `null`、无异常无 5xx | `hybrid.py`；`app/services/search_pipeline.py`；`tests/test_rerank.py` |
| 降级面 | 覆盖：开关关闭、连接/HTTP 错误、非 JSON、`results` 缺失、条数不匹配、条目非对象、`index` 非整数/越界、`score` 非数值 | `rerank_service.py`、；`tests/test_rerank.py` |
| 默认超时偏小 | `RERANK_TIMEOUT` 默认 10s，但多语言档 ≈0.4–0.47 s/候选，`top_k=10`（50 候选）≈20s → **静默降级**（`rerank.model=null`、`rerank_score=null`），不报错 | `.env.example`；[report-jina-rerank-comparison.md](../../evals/report-jina-rerank-comparison.md) |
| 日志文案 | 降级 warning：`rerank request failed, falling back to first-stage order`、`rerank response was not JSON, falling back`、`rerank response was not an object`/`missing 'results'`/`returned %d scores for %d documents`/`index %s is out of range`；正常 info：`chunk search finished` 带 `rerank`/`rerank_took_ms`（`hybrid.py`） | 同上 |
| `rerank_took_ms` 口径 | 客户端整段耗时（含网络与容器排队），容器自己的 `took_ms` 未被读取（只取 `results`） | `hybrid.py`；`rerank_service.py` |
| `RERANK_MODEL` 不参与调用 | 它只出现在响应 `rerank.model` 里；实际模型由容器环境变量决定，两侧不一致不会被发现 | `config.py`；`app/services/search_pipeline.py`；`infra/embedding/server.py` |
| 候选数放不大 | 容器单批上限 `RERANK_MAX_BATCH`；交叉编码器激活内存随 `token × 候选数` 增长，故 `RERANK_CANDIDATES` 调大只会线性拉长批次数与总时长，不能靠堆候选换精度。**批大小也有反效果**（50 候选）：jina 档 4 → 2.4GB / 8 → 3.3GB / 16 → 5.1GB（未复测，3GB 封顶即 OOM-kill）；现役 int8 档按 4/8/16 = 3.2/4.0/4.8 秒每调用、匿名峰值 2351/2555/3199 MiB —— 更大的批更慢**也更占内存**（批内按最长补齐），所以仍用 4 | `infra/embedding/server.py`；`AGENTS.md` 第 3.3 节；`infra/.env.example`；`logs/eval/rerank-model-*.json` |
| 容器侧无候选上限 | `RerankRequest.documents` 不设 `max_length`（对比 `/embed` 的 `MAX_BATCH` 限批），长候选清单由容器内部切批，不会 422 | `infra/embedding/server.py`、|
| 改写触发条件 | 需同时满足：`QUERY_REWRITE_ENABLED=true`、查询非空、长度 ≤ `QUERY_REWRITE_MAX_CHARS`、命中 CJK 正则；**纯 ASCII 查询即使开启也不改** | `app/services/search_pipeline.py`；`query_rewrite_service.py`；`tests/test_query_rewrite.py` |
| 改写失败降级 | 非 200、JSON 解析失败、无 `choices`/`content`、清洗后为空、改写结果与原查询相同 → `applied=false`、原查询继续检索 | `query_rewrite_service.py`；`tests/test_query_rewrite.py` |
| 启动即校验 | `QUERY_REWRITE_ENABLED=true` 时 `URL`/`MODEL`/`API_KEY` 任一为空 → `Settings()` 抛 `ValueError`，进程起不来（不静默降级） |；`tests/test_query_rewrite.py` |
| 改写契约 | 请求体固定 `model/messages/temperature=0/max_tokens`，`Authorization: Bearer <key>`，URL 为 `<base>/chat/completions`（尾斜杠被 `rstrip`） | `query_rewrite_service.py`；`tests/test_query_rewrite.py` |
| 推理模型坑 | 64-token 上限时推理模型把预算花在隐藏 `reasoning_content` 上，可见 `content` 为空且 `finish_reason=length` → 静默降级；默认已抬到 512 | `config.py`；`.env.example` |
| 改写收益（外部实测） | 60 条查询：`hybrid|rerank=on` HR@1 0.700→0.900、MRR 0.850→0.950；中文改写后的另一组实测 ZH 语义命中从 0.30 提到 1.00（模块 docstring 引用的数字） ；出自 [report-zh-llm-rewrite.md](../../evals/report-zh-llm-rewrite.md)；`query_rewrite_service.py`|
| 精排收益（外部实测） | jina-v2 + `RERANK_MAX_BATCH=4`：`hybrid|on` HR@1 0.760→0.880、nDCG@10 0.853→0.934，增益全在跨语言 top-1（ZH HR@1 0.100→0.700），英文持平 ；出自 [report-jina-rerank-comparison.md](../../evals/report-jina-rerank-comparison.md)、[report-jina-rerank-comparison.md](../../evals/report-jina-rerank-comparison.md)|
| 组合未验证 | 改写与精排机制正交，但“`QUERY_REWRITE_ENABLED=true` + `rerank=true`”无实测数字 | [report-jina-rerank-comparison.md](../../evals/report-jina-rerank-comparison.md) |

## 6. 配置项（键 → 默认值 → 作用 → 出处文件:行）

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `RERANK_ENABLED` | `true` | 服务端是否具备精排能力；`false` 时 `rerank_texts` 直接返回 `None` | `app/core/config.py` |
| `RERANK_MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` | 仅用于响应 `rerank.model`，不参与调用 —— 所以它必须与 `infra/.env` 里容器真正加载的名字一致（现役 `temsa/mmarco-mMiniLMv2--H384-v1-onnx-cpu-qint8`），否则响应会报出一个没在跑的模型名。**改这个键必须重启应用**（配置在启动时读入） | `app/core/config.py`、`infra/.env:13`、`.env:29` |
| `RERANK_URL` | `http://127.0.0.1:8090` | 精排容器基址，拼 `/rerank`、`/health` | `app/core/config.py` |
| `RERANK_TIMEOUT` | `10.0`（本机 `.env`=60） | httpx 超时（秒）；超时即静默降级。现役 int8 档 0.064 s/候选 ⇒ `top_k=10` ≈3.2s，默认值够用；保留 60 是因为推理队列单线程，排队时间由队列而非模型速度决定（换回 jina 0.276 s/候选时更是必需） | `app/core/config.py`、`.env:50` |
| `RERANK_CANDIDATES` | `5` | 一阶段过取倍数，候选数 = `top_k × 此值` | `app/core/config.py` |
| `RERANK_MAX_BATCH` | 无 app 侧默认；`infra/.env` 与 `.env.example` 为 `4`，`docker-compose.yml` 兜底 `16` | 容器单次推理的文档上限，超出则切批。**批越大越慢越占内存**（int8 档 50 候选：批 4/8/16 = 3.2/4.0/4.8 秒每调用、匿名峰值 2351/2555/3199 MiB） | `infra/embedding/server.py`、`infra/.env:17`、`infra/docker-compose.yml` |
| `RERANK_MODEL_FILE` | 无 app 侧；`infra/.env` `model.onnx`（compose 兜底 `onnx/model.onnx`） | 容器侧：**不在 fastembed 内置清单里的**交叉编码器的仓库内 ONNX 文件路径；应用侧不读 | `infra/embedding/server.py`、`infra/.env:14`、`infra/docker-compose.yml` |
| `QUERY_REWRITE_ENABLED` | `false` | 改写总开关；关闭时零 HTTP 调用 | `app/core/config.py` |
| `QUERY_REWRITE_URL` | `""` | OpenAI 兼容基址，拼 `/chat/completions` | `app/core/config.py` |
| `QUERY_REWRITE_API_KEY` | `""` | Bearer 凭证 | `app/core/config.py` |
| `QUERY_REWRITE_MODEL` | `""` | 请求体 `model` | `app/core/config.py` |
| `QUERY_REWRITE_TIMEOUT` | `10.0` | httpx 超时（秒） | `app/core/config.py` |
| `QUERY_REWRITE_MAX_CHARS` | `300` | 触发改写的输入长度上限 + 输出截断上限；校验必须为正 | `app/core/config.py`|
| `QUERY_REWRITE_TARGET_LANGUAGE` | `en` | 目标语言，**当前无代码读取** | `app/core/config.py` |
| `QUERY_REWRITE_MAX_TOKENS` | `512` | 单次改写 `max_tokens`（留给推理模型的隐藏推理） | `app/core/config.py` |

## 7. 测试位置与覆盖（tests/xxx.py → 覆盖什么）

| 文件 | 覆盖 |
|---|---|
| `tests/test_rerank.py` | `rerank_service` 全失败面：抛异常/HTTP 500/条数不匹配/非 JSON/结构畸形 → `None`；禁用即 `None`；解析并按分降序；发送 `top_n` 与截断后文档；空候选不发请求；`is_available` 三态；`search_chunks`：关闭取 `top_k`、开启过取 `top_k × rerank_candidates`、返回 `top_k*2`、重排且保留 `retrieval_score`、降级回一阶段序、归一化到 0..1、hybrid 模式同样精排；响应契约 `rerank_score`/`retrieval_score` 与 `rerank` 块；请求字段 `rerank` 生效 |
| `tests/test_query_rewrite.py` | CJK 触发与 ASCII/空/超长不触发；消息体形状；`clean_rewrite` 规范化与截断；成功路径与尾斜杠 URL；传输错误/非 200/非 JSON/无 content/清洗为空/与原查询相同 → 降级；禁用与空查询；网关 `text` 字段兼容；配置默认关闭、开启需 URL+MODEL+KEY、`MAX_CHARS` 必须为正；API 门控三态；响应契约 `rewritten_query`/`rewrite`；请求体无改写开关 |

## 8. 未做 / 已知缺口

| 缺口 | 说明 |
|---|---|
| 改写与精排组合无实测 | [report-jina-rerank-comparison.md](../../evals/report-jina-rerank-comparison.md) 提及两者叠加，仓库内无结果 |
| `QUERY_REWRITE_TARGET_LANGUAGE` 是死配置 | `config.py` 定义、`.env.example` 暴露，但全仓无读取方；系统提示词硬编码英文（`query_rewrite_service.py`） |
| `DEFAULT_MAX_TOKENS = 512` 未被使用 | 仅定义与导出（`query_rewrite_service.py`），实际取值走 `settings.query_rewrite_max_tokens`，两者可漂移 |
| `rerank_service.is_available()` 无调用方 | 全仓仅 `rerank_service.py`/` 出现，未接任何健康检查或启动自检 |
| 改写失败原因不入响应 | `RewriteOutcome.reason` 只进日志（`query_rewrite_service.py`），API 只上报 `applied/model/took_ms`，调用方无法区分“未触发”与“调用失败” |
| `telemetry["reranked"]` / `["candidates"]` 无消费方 | 写入在 `hybrid.py`，API 只用 `rerank_took_ms`（`app/services/search_pipeline.py`），`candidates` 只用于日志字段 `candidates`（其值来自 `top_k × CANDIDATE_FACTOR`，与检索候选不同源） |
| `rerank.model` 可能失真 | 取自 app 配置而非容器返回体（`app/services/search_pipeline.py` vs `rerank_service.py` 丢弃 `model`），两侧不一致时无人发现 |
| 无改写缓存 / 无精排批间并行 | 每次请求各发一次 LLM 与一次 `/rerank`；无结果缓存、无跨请求复用、无并发分批 |
| 无 paper 级精排 | 精排只作用于 chunk；论文分取组内最大值（`search_service.py` 的 `aggregate_papers()`） |
| 分阶段延迟不可观测 | 仅总 `took_ms` + `rerank_took_ms`，改写耗时只出现在 `rewrite.took_ms`，BM25/kNN/embedding 各段无计时（[审查合并报告-20260914.md](../examine/审查合并报告-20260914.md)） |
| jina 档内存数字未复测 | jina 档的 4/8/16 → 2.4/3.3/5.1GB 未用新仪器复测；**int8 档**用宿主侧 20ms 采样测得：批 4/8/16 → 匿名峰值 2351/2555/3199 MiB，脚本 `scripts/acceptance_rerank_model.py`，报告 `logs/eval/rerank-model-mmarco-int8-b{4,8,16}.json` |
