# Embedding 服务与调用

| 项 | 内容 |
|---|---|
| 状态 | 主体依据 commit 54048a3（2026-09-22）；**T7.3 增补**（服务端推理队列、`paper_degradations`）已按 2026-09-30 工作树核对行号；**2026-10-07 增补**：`/info`/`/health` 改报**实测维度**、应用启动期三方维度对账（§5b）、换模型 runbook（§9） |
| 关键文件 | `infra/embedding/server.py`、`infra/embedding/Dockerfile`、`infra/docker-compose.yml`(embedding 段)、`app/services/embedding_service.py`、`app/workers/tasks.py`(EMBEDDING 阶段)、`app/core/config.py`、`app/search/mappings.py`、`app/search/hybrid.py`(查询侧调用点) |
| 相关文档 | `AGENTS.md` §3.3/§3.7；`docs/progress/project.md` §12/§13；`docs/examine/RAG六步对照自查-20260914.md:24`；`evals/report-jina-rerank-comparison.md` |

## 1. 职责边界（做什么 / 不做什么）

做：
- 容器侧把文本批量转成稠密向量（`POST /embed`；维度由所加载的模型决定，部署档 1024），并提供 OpenAI 兼容入口（`POST /v1/embeddings`）与交叉编码器精排（`POST /rerank`）；`GET /info`、`GET /health` 回报能力、限批与**实测维度**。
- 应用侧按批切分 texts、重试、超时、维度校验，把向量写进 OpenSearch，把 embedding 溯源信息写进 PostgreSQL。
- **启动期三方维度对账**（2026-10-07，§5b）：容器实测维度 ↔ `EMBEDDING_DIMENSION` ↔ 活索引 mapping，实测到的不一致拒绝启动。

不做：
- 不做 `query:`/`passage:` 前缀（e5 前缀未加，见 §8）。
- 不做文本截断/分块：容器不对入参做 token 限长（`server.py` 中无 `max_length`/截断逻辑），分块由 `app/parsing/chunking.py` 负责（`DEFAULT_TARGET_TOKENS=400`、`MAX_TOKENS=450`，`app/parsing/chunking.py:41-42`）。
- 向量不落 PostgreSQL：PG 只存 `embedding_model`/`embedding_dimension`/`embedded_at`（`app/workers/tasks.py:1141-1234`）。
- 本模块不定义精排/改写策略（候选窗口、降级语义、双分数属 06 号文档）。

## 2. 关键文件与函数（文件 → 函数/类 → 作用，带行号）

| 文件 | 函数/类 | 作用 |
|---|---|---|
| `infra/embedding/server.py` | `QueueFull` :70 / `InferenceQueue` :66（`submit()` :120、`stats()` :134）/ `QUEUE` :146 / `_busy()` :149 | **T7.3 服务端队列**：所有推理排进一条 FIFO，队满回 503+`Retry-After` |
| | `get_model()` :161 / `register_custom_reranker()` :177 / `get_reranker()` :198 | 惰性单例；首次调用才加载/下载模型（加载本身也在队列里执行）。`register_custom_reranker` 把**不在 fastembed 清单里**的交叉编码器（例如 int8 量化导出）用 `add_custom_model` 注册一次 —— 幂等，内置模型直接跳过 |
| | `EmbedRequest` :216 / `OpenAIEmbedRequest` :222 / `RerankRequest` :229 | 入参模型（`extra="ignore"`） |
| | `health()` :237 / `info()` :251 | `/health`、`/info`（两者都回报 `queue`/`inference` 计数） |
| | `embed()` :264 | `/embed` 批量向量（经 `QUEUE.submit`） |
| | `rerank()` :283 | `/rerank` 交叉编码器打分（同一条队列） |
| | `openai_compat()` :330 | `/v1/embeddings` OpenAI 形态（同一条队列） |
| `app/services/embedding_service.py` | `_endpoint()` :34 / `_post_batch()` :39 | 拼 URL；单批 POST + 响应解析 |
| | `validate_dimension()` :73 | 逐向量核对维度 |
| | `embed_texts()` :83 | 分批 + 重试 + 退避（主入口） |
| | `embed_text()` :153 | 单条包装（查询侧用） |
| | `embedding_metadata()` :158 | 返回 model/dimension 字典（当前无调用方） |
| | `container_dimension()` :183 / `index_dimension()` :221 / `check_dimension_consistency()` :246 | **2026-10-07 启动期三方对账**：探测容器实测维度与活索引 `knn_vector` 维度（**永不抛**——不可达/未知一律 WARN + `None`），`check_dimension_consistency` 只在**实测到**的不一致上抛 `RuntimeError`；由 `app/main.py` lifespan 在队列启动前调用 |
| `app/workers/tasks.py` | `_advance_stage()` :194 | 阶段标记并 COMMIT（可见性） |
| | `_run_pipeline()` :464 起；CHUNKING 段 :545-592（`on_degrade=degradations` :579、`degradations.resolve(...)` :592）；EMBEDDING 段 :594 | 调用 embed、数量核对、写溯源；降级留痕（T7.3） |
| | `_write_embeddings()` :1217 | PG 侧 model/dimension/embedded_at |
| | `_index_rows()` :1253 | 构造待索引文档（含 `embedding`） |
| | `_record_failure()` :1319 | FAILED 簿记（`classify_failure` → `error_code`） |
| `app/search/hybrid.py` | `_semantic_hits()` :640 | 查询侧 `embed_text(query)`，把 `EmbeddingError` 转 `SearchError` |
| `app/core/errors.py` | `classify_failure()` :144；`EmbeddingError` 分支 :202 | `EMBEDDING_FAILED` 归类 |
| `app/services/degradation_service.py` | `record()` :81 / `resolve_stage()` :133 / `Recorder` :226 | **T7.3 降级账本**：`(stage, code, detail)` 落 `paper_degradations` |

## 3. 数据结构（表/字段/索引，或内存结构）

容器入参/出参：

| 接口 | 入参 | 出参 | 校验位置 |
|---|---|---|---|
| `POST /embed` | `{"texts": [...], "model"?: str}`，`min_length=1, max_length=MAX_BATCH` | `{"embeddings":[[...]],"model","dimension","latency_ms","count"}`；队满 `503`+`Retry-After: 5` | `infra/embedding/server.py:216-218`（pydantic，超批 422）；返回的 `dimension` 取第一个向量的实际长度（`infra/embedding/server.py:272`）；入队点 `infra/embedding/server.py:267`（`QUEUE.submit`） |
| `POST /v1/embeddings` | `{"model"?: str, "input": str\|list[str], "encoding_format"?: str}` | OpenAI 形态 `{"object":"list","model","data":[{"embedding":[...]}],"usage":{...},"latency_ms"}` | 无长度上限（`infra/embedding/server.py:222-225`）；`usage` token 恒为 0（`infra/embedding/server.py:345`） |
| `POST /rerank` | `{"query": str, "documents": [...], "top_n"?: int}` | `{"results":[{"index","score"}...],"model","took_ms"}`，按 score 降序 | 无入参长度上限；服务内按 `RERANK_MAX_BATCH` 分片（`infra/embedding/server.py:295-296`） |
| `GET /info` | — | `{"model","dimension": <int\|null>,"max_batch","rerank_model","rerank_max_batch","inference":{workers,queue_depth,waiting,running,completed,rejected}}` | `dimension` 是**模型加载时探针实测**的输出宽度（`get_model()` 里 embed 一条探针文本，`infra/embedding/server.py:161-174`），模型未加载时为 `null`（`infra/embedding/server.py:251-261`）；不再是写死的 1024 |
| `GET /health` | — | `{"status":"ok","model","dimension": <int\|null>,"loaded","rerank_model","rerank_loaded","queue":{...}}` | 模型未加载也返回 200（惰性加载，`infra/embedding/server.py:237-249`），此时 `dimension` 为 `null` |

PostgreSQL（`app/db/models.py`）：

| 表 | 字段 | 说明 |
|---|---|---|
| `papers` | `embedding_model` :132 / `embedding_dimension` :133 | STORED 阶段先写（`tasks.py:297-298`），EMBEDDING 后重申（:1226-1227） |
| `paper_chunks` | `embedding_model` :420 / `embedding_dimension` :417 / `embedded_at` :418 / `doc_metadata`(JSONB) :416 | 不含向量；`doc_metadata["embedding_dimension"]` 记的是返回向量实际长度（`tasks.py:1130-1132`） |
| `ingestion_jobs` | `stage` :450 / `progress` :453 / `error_code` :458 / `error_message` :459 | 失败时保留失败阶段与进度 |
| `paper_degradations` | `stage` :773 / `code` :755 / `detail`(JSONB) :763 / `occurrences` :766 / `first_seen_at` :757 / `last_seen_at` :760 / `resolved_at` :771 / `job_id` :753 | **T7.3 降级账本**：`UNIQUE(paper_id, stage, code)`；`resolved_at IS NULL` = 当前仍然成立 |

OpenSearch（`app/search/mappings.py`）：`embedding` = `knn_vector`，`dimension = settings.embedding_dimension`，`hnsw/l2/lucene`，`ef_construction=128`、`m=16`（:125-134）；`embedding_model`(keyword)/`embedding_dimension`(integer)（:135-136）；索引级 `knn=true`（:156）。文档体由 `_index_rows()` 组装（`tasks.py:1278-1279` 的 payload 起始）并经 `build_chunk_document()`（`app/search/opensearch.py:268-310`）落库，`embedding` 仅在非空时写入（`opensearch.py:268-270`）。

## 4. 调用链（从入口到落地，逐跳，带函数名）

写入（ingest/reindex）：
1. `run_ingestion_job()`（`tasks.py:141`）/ `run_reindex_job()`（:121）→ `_process_job()`（:213）→ `_run_pipeline()`（:464）。
2. `chunk_document()` 出 chunks（语义模式下它会**先**调用同一个 `/embed` 给句子打分，见 §8 缺口 11）→ `_replace_chunks()` 落 PG（:545-592）。
3. `_advance_stage(session, job, STAGE_EMBEDDING, PROGRESS_EMBEDDING=80.0)`（:643，常量 :56/:66）→ COMMIT，此刻 `GET /api/jobs/{id}` 已能看到 `EMBEDDING/80`。
4. `embedding_service.embed_texts([chunk.text for chunk in chunks])`（:581）。
5. `embed_texts` 内：`settings.embedding_batch_size`（16）切片 → `_post_batch()`（`embedding_service.py:39`）→ `httpx.Client(timeout=EMBEDDING_TIMEOUT)` POST `{EMBEDDING_URL}/embed`。
6. 容器：`embed()`（`infra/embedding/server.py:264`）→ `QUEUE.submit(...)`（`infra/embedding/server.py:267`）→ **排队** → 工作线程里执行 `fn(...)`（`infra/embedding/server.py:111`，即 `get_model().embed(...)`，fastembed/ONNX）。队满则第 5 跳直接拿到 `503`+`Retry-After`（`infra/embedding/server.py:128-132` 抛 `QueueFull`、`infra/embedding/server.py:149-151` 转 503），应用按第 90 行表格的退避重试。
7. 回程校验：向量条数 == 批大小（`embedding_service.py:120-124`）→ `validate_dimension()`（:125）→ 拼接。
8. `len(vectors) != len(rows)` → `IngestionError`（`tasks.py:582-585`）。
9. `_write_embeddings()`（:587）写 PG 溯源 → `_advance_stage(INDEXING, 95.0)`（:589）。
10. `opensearch.ensure_index()` → `delete_by_paper_id()` → `_index_rows()` → `bulk_index_chunks()`（`app/workers/tasks.py:607-610`，`opensearch.py:389`，`BULK_BATCH_SIZE` 默认 200 + `refresh=True`）→ `_mark_indexed()`（`app/workers/tasks.py:623`）。

查询侧（策略细节见 06）：
`POST /api/search` → `search_service.search_papers` → `hybrid.search_chunks()`（`hybrid.py:668`）→ `_semantic_hits()`（:640）→ `embed_text(query)`（:633）→ `EmbeddingError` 被转成 `SearchError`（:651-652）→ `app/api/search.py:45-48` 返回 503（查询侧 embedding 失败**不会**退化成纯关键词）。精排另走 `rerank_service.rerank_texts()` → `POST /rerank`（客户端契约见 `app/services/rerank_service.py:1-14`）。

### 4b. 服务端排队（T7.3）

**问题**：`/embed` 是同步 `def`，Starlette 会把它丢进 anyio 线程池（默认 ~40 线程），每个请求各自跑一次 ONNX 推理；`INGEST_CONCURRENCY`(2) 的流水线、语义分块的 CHUNKING 段、查询侧精排又都打同一个容器。并发不会遭拒，只会互相抢 CPU/内存（历史上 uvicorn 被 oom-killer 杀掉就是这条路），延迟同时被拉长。

**做法**：容器内部加一条 FIFO 队列 `InferenceQueue`（`infra/embedding/server.py:70`）。`/embed`、`/v1/embeddings`、`/rerank` 三处都改成 `QUEUE.submit(fn)`：调用线程把 `(fn, args, future)` 入队，`INFERENCE_WORKERS`(默认 1) 个工作线程依次取出执行，调用线程在 `future.result()` 上等结果（异常原样透传，所以 500/503 语义不变）。

- 积压长度由 `INFERENCE_QUEUE_DEPTH` 兜底：满了**立即**抛 `QueueFull` → `503` + `Retry-After: 5`（`infra/embedding/server.py:128-132`、`infra/embedding/server.py:149-151`）。选择“拒绝而不是无限排队”：无界排队会把等待推到客户端超时之后，那时两边都拿不到可用的错误。
  - 定这个值的规矩：`depth × 单次推理耗时 ≤ EMBEDDING_TIMEOUT / 2`（应用侧默认 300s）。
  - **2026-10-01 从 32 提到 512**：原值是按"单次交互搜索"定的，跑 SRW 参数扫描（一次几百个并发神经检索、每次都要过远程模型）时被瞬间打满 —— `/info` 的 `rejected` 累计 10141，SRW 变体 90% 报
    `inference queue full (depth=32, workers=1)`，而远端连接池怎么放大都没用（瓶颈在这条队列）。实测扫描期峰值 `waiting=38`，正好卡在旧上限之上。512 × 0.1~0.3s ≈ 51~154s < 150s(=300/2)，`rejected` 归零。
    **放大队列不等于无限排队**：深度仍是有限的，只是把"并发几十"这一档从拒绝改成排队。
- 客户端**不做闸门**（这是刻意的：应用侧没有信号量，只保留 `EMBEDDING_TIMEOUT` 与重试），削峰全部在服务端。
- 代价是延迟变成“排队时间 + 推理时间”，所以 `EMBEDDING_TIMEOUT` 从 120s 上调到 300s（`config.py:83-87`）——否则排队会以“批次失败”的形式表现出来。查询侧 `RERANK_TIMEOUT` 另算（见 06）。

**实测**（`scripts/probe_embedding_queue.py`，本机 CPU，e5-large，batch=16）：

| 场景 | 结果 |
|---|---|
| 单批延迟（热） | 17.86s |
| 4 批同时打 | wall 70.12s，各请求 17.79 / 35.21 / 52.66 / 70.12s（严格等差 = 串行） |
| 峰值并发推理 | 1（`/info` 采样，workers=1） |
| 队满（另起容器 `INFERENCE_QUEUE_DEPTH=1`） | 6 请求 → `200, 200, 503, 503, 503, 503`，四个 503 都带 `Retry-After: 5`；`rejected=4` |

细节与原始输出见 `docs/progress/project.md` §21.7。

## 5. 不变量与踩过的坑

| # | 不变量 / 坑 | 证据 |
|---|---|---|
| 1 | 应用 batch 必须等于容器 `MAX_BATCH`，但两侧分属两个 env 文件、代码无一致性校验；不等即 422、导入整体失败 | 应用 `config.py:78`=.env 16；容器 `infra/docker-compose.yml:128`=16；告警写在 `infra/.env.example:33` |
| 2 | embedding 与精排的限批必须解耦：`MAX_BATCH` 同时管 `/embed` 上限，压小会让导入全 422 | `infra/embedding/server.py:46-51` 注释；`infra/docker-compose.yml:120-122` |
| 3 | 每批新建 `httpx.Client`，无连接复用；批次串行、批内重试，单条流水线内部无并发（并发来自多条流水线） | `embedding_service.py:43`（with 块）、:114-127（顺序循环） |
| 4 | 重试 = `EMBEDDING_MAX_RETRIES`(2) 次额外尝试，退避 `0.5 * 2**attempt`（0.5s、1.0s）；超时 = 单批 `EMBEDDING_TIMEOUT`(**300s**，T7.3 起：服务端排队后它必须大于最坏排队时间，否则排队会变成失败) | `embedding_service.py:117`、:135；`config.py:83-87` |
| 5 | 维度曾在三处各说各话（应用校验、索引 mapping、容器实际输出），且无启动期校验 —— **2026-10-07 已补**：启动期三方对账（§5b），写侧逐批校验不变 | `embedding_service.py:73-80`、`mappings.py:124`、`infra/embedding/server.py:272` |
| 6 | 容器失败码：推理本身出错 `/embed` = **500**、`/rerank` = **503**；T7.3 起**队满一侧统一 503**（带 `Retry-After: 5`）——`AGENTS.md` §3.3 的“未就绪返回 503”只对精排与“队满”成立 | `infra/embedding/server.py:270-271`（500） vs `infra/embedding/server.py:128-132` + `infra/embedding/server.py:149-151`（队满 503）、`infra/embedding/server.py:312-313`（精排 503） |
| 7 | `/v1/embeddings` 不受 `MAX_BATCH` 限制，且应用侧不使用它（应用只走 `/embed`）；它同样排在队列后面 | `infra/embedding/server.py:222-225`、`infra/embedding/server.py:329-347`；`embedding_service.py:27`（`EMBED_PATH="/embed"`） |
| 8 | 向量条数不匹配（第 8 跳）落 `IngestionError`，不在 `EmbeddingError` 分支 → `error_code=INTERNAL`，不是 `EMBEDDING_FAILED` | `tasks.py:545-566`；`errors.py:175-220`（`EmbeddingError` 分支 :182，尾部兜底 :200） |
| 9 | 失败保留现场：`_advance_stage` 已 COMMIT，`EMBEDDING/80` 是可读的失败点；`_record_failure` 只改 job 与 paper.status。降级账本骑在同一个事务上：作业回滚则降级行一并回滚（不是漏记——那次运行没留下产物） | `tasks.py:194-210`、:1319-1338；`degradation_service.py:226-283` |
| 9b | **T7.3 服务端队列**：`/embed`、`/v1/embeddings`、`/rerank` 共用一条 FIFO，由 `INFERENCE_WORKERS`(1) 个工作线程串行执行；积压 > `INFERENCE_QUEUE_DEPTH`（2026-10-01 起 512）直接 503，而不是无限排队（无界排队只会把等待推到客户端超时之后） | `infra/embedding/server.py:60-63`、`infra/embedding/server.py:70`、`infra/embedding/server.py:146` |
| 10 | 模型惰性加载：首个请求才下载/加载（`/health` 在加载前也 200）；healthcheck 15s×30 次容错下载窗口；**首次加载发生在队列工作线程里**，所以冷启动期间其余请求都在排队等待 | `infra/embedding/server.py:161-213`；`infra/docker-compose.yml:145-149` |
| 11 | `/info`、`/health` 的 `dimension` 曾是硬编码 1024 —— **2026-10-07 已改为加载时探针实测**（未加载为 `null`）；换模型后自报维度即真实值 | `infra/embedding/server.py:157-158`（全局量）、`infra/embedding/server.py:161-174`（探针）、`infra/embedding/server.py:241`（/health）、`infra/embedding/server.py:254`（/info） |
| 12 | 容器单进程（`--workers 1`）：embedding 与 rerank 共用一个进程/ORT 线程池 | `Dockerfile:9` |
| 13 | embedding 容器**没有任何内存上限**（compose 段无 `mem_limit`/`deploy.resources`），稳定性依赖 `MAX_BATCH`/`RERANK_MAX_BATCH`/`ORT_THREADS` 三个阀 + T7.3 的 `INFERENCE_WORKERS`（同时推理数） | `infra/docker-compose.yml:100-149` |
| 14 | 内存实测：`RERANK_MAX_BATCH=4` 是硬约束（16 候选峰值 5.10GB、4 候选 2.36GB；3GB 封顶那次被 OOM-kill exit 137）；`ORT_THREADS=4` + embed batch 16 是安全点（8 线程+大 batch 曾把 uvicorn 打成 5.7GB 被 oom-killer 杀）—— **出自 docs/progress/project.md §13** 与 `infra/docker-compose.yml:124-128` 注释 |
| 15 | WSL 内存线：`.wslconfig` `memory=9GB`、`free -h` 实测 8.7GiB —— **出自 docs/progress/project.md §13** |
| 16 | 应用并发上限 `INGEST_CONCURRENCY`(默认 2) ⇒ 最多 2 条流水线同时打同一个 `/embed`；叠加语义分块的 CHUNKING 段与查询侧，队列是唯一的削峰点 | `config.py:152`；`app/workers/queue.py:6` 注释 |
| 17 | 空入参 `embed_texts([])` 直接返回 `[]`；空字符串照发（保证 1:1 对齐） | `embedding_service.py:101-102`；docstring :91-92 |
| 18 | 异常信息只带批次 offset 与上游文本，不打全文（无原文泄漏） | `embedding_service.py:131-134` |

### 5b. 维度三方对账（2026-10-07）

**问题**：维度 statements 曾有三处、互不对账——容器模型的**实际输出宽度**、`EMBEDDING_DIMENSION`
（应用侧逐批校验的期望值）、活索引 mapping 的 `knn_vector.dimension`。配错要到第一次 embed 批
（`EMBEDDING_FAILED`）或第一次 kNN 查询（400）才暴露；同维度换模型则更糟——不炸，但检索质量静默劣化。

**做法**（`app/services/embedding_service.py::check_dimension_consistency`，`app/main.py` lifespan 在
**队列启动前**调用）：

1. `container_dimension()`：`GET {EMBEDDING_URL}/info` 读**实测维度**（探针 embed 一次，见不变量 11）。
2. `index_dimension()`：读活物理索引 mapping 的 `embedding.dimension`（读 mapping 的纯函数
   `app/search/opensearch.py::mapping_embedding_dimension` 同时兼容 `get_mapping()` 与 `build_mapping()`
   两种形状——`scripts/create_index.py` 的迁移护栏共用它）。
3. 任何一处**实测到**与 `EMBEDDING_DIMENSION` 不一致 → `RuntimeError`，进程拒绝启动
   （错误信息写明修复路径：改配置或整库重嵌，见 §9）。
4. 不可达 / 模型未加载（`dimension=null`）/ 索引不存在 → **WARN 放行**：应用可以先于容器启动，
   写侧 `validate_dimension` 与索引 `dynamic: strict` 仍是兜底。**"无法验证"不是"不一致"**。

**测试**：`tests/test_dimension_startup_check.py`（探针永不抛、两种 mapping 形状、只有实测不一致才抛）。
真机基线（2026-10-07）：`{expected: 1024, container: 1024, index: 1024}` 通过。

## 6. 配置项（键 → 默认值 → 作用 → 出处文件:行）

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `EMBEDDING_URL` | `http://localhost:8090`（`.env` 实为 `http://127.0.0.1:8090`） | 应用侧 `/embed` base | `app/core/config.py:79`、`.env:19` |
| `EMBEDDING_MODEL` | 代码 `BAAI/bge-m3`；`.env`/compose `intfloat/multilingual-e5-large` | 容器加载的模型名；应用仅作为请求字段与溯源值 | `config.py:80`、`infra/embedding/server.py:44`、`infra/docker-compose.yml:111`、`.env:20` |
| `EMBEDDING_DIMENSION` | 1024 | 应用侧维度校验 + mapping `dimension` | `config.py:81`、`.env:21` |
| `EMBEDDING_BATCH_SIZE` | 代码 32；`.env`/`.env.example` 16 | 单次 `/embed` 的 texts 数 | `config.py:82`、:292-297（正数校验）、`.env:22`、`.env.example:43` |
| `EMBEDDING_TIMEOUT` | **300.0**（T7.3 由 120 上调） | 单批 HTTP 超时；必须大于服务端最坏排队时间 | `config.py:83-87`、`.env.example:49` |
| `EMBEDDING_MAX_RETRIES` | 2 | 每批额外重试次数 | `config.py:88`、`.env.example:50` |
| `MAX_BATCH` | 代码 64；compose `16`（`infra/.env` 未设） | `/embed` 入参上限（超批 422） | `infra/embedding/server.py:46`、`infra/docker-compose.yml:128` |
| `RERANK_MAX_BATCH` | 代码 = `MAX_BATCH`；compose 兜底 `16`；`infra/.env` **4** | 单次精排推理的候选上限（服务内分片）。**批越大越慢越占内存**：50 候选实测批 4/8/16 = 3.2/4.0/4.8 秒每调用、匿名峰值 2351/2555/3199 MiB（int8 档） | `infra/embedding/server.py:51`、`infra/docker-compose.yml:123`、`infra/.env:22` |
| `ORT_THREADS` | 代码 2；compose `4` | ONNX Runtime 线程数（embedding 与 rerank 共用） | `infra/embedding/server.py:57`、`infra/docker-compose.yml:127` |
| `INFERENCE_WORKERS` | 代码 1；compose `1`；`infra/.env` 1 | **T7.3** 同时执行推理的工作线程数（=1 即完全串行） | `infra/embedding/server.py:60`、`infra/docker-compose.yml:138`、`infra/.env:44` |
| `INFERENCE_QUEUE_DEPTH` | 代码 32；compose `512`；`infra/.env` 512 | **T7.3** 允许排队等待的请求数上限，超出直接 503；**2026-10-01 提档**，见 §4b 的定值规矩（`depth × 单次推理耗时 ≤ EMBEDDING_TIMEOUT/2`） | `infra/embedding/server.py:63`、`infra/docker-compose.yml:139`、`infra/.env:45` |
| `RERANK_MODEL` | 代码 `Xenova/ms-marco-MiniLM-L-6-v2`；`infra/.env` `temsa/mmarco-mMiniLMv2-L12-H384-v1-onnx-cpu-qint8`（2026-10-01 全量换档；jina 作为高配机器选项留在注释里，见 `docs/progress/project.md` §22.u） | 交叉编码器；名字在应用侧与容器侧**必须一致**（应用只用它回显 `rerank.model`，容器才真正加载）。**改完要重启应用**（`.env` 启动时读入，否则响应会报旧模型名） | `infra/embedding/server.py:45`、`config.py:92`、`infra/.env:13`、`.env:29` |
| `RERANK_MODEL_FILE` | 代码 `onnx/model.onnx`；`infra/.env` `model.onnx`（现役 int8 档需要它） | **只对不在 fastembed 清单里的模型生效**：仓库内 ONNX 文件路径。多数 HF 导出在 `onnx/` 子目录，动态 int8 量化导出常放仓库根（就是这个档）。换回内置档（jina/ms-marco）时注释掉 | `infra/embedding/server.py:56`、`infra/docker-compose.yml:116`、`infra/.env:14` |
| `RERANK_ENABLED` / `RERANK_URL` / `RERANK_TIMEOUT` / `RERANK_CANDIDATES` | `true` / `http://127.0.0.1:8090` / `10.0`（`.env:49`=60）/ `5` | 精排开关、地址、超时、候选倍数（细节见 06） | `config.py:91-103` |
| `FASTEMBED_CACHE_PATH` | `/models` | 模型缓存目录（挂载宿主目录） | `infra/docker-compose.yml:119`、`infra/docker-compose.yml:144` |
| `HF_ENDPOINT` / `HF_HUB_DISABLE_XET` | `https://hf-mirror.com` / `1` | 首次下载的镜像与传输开关 | `infra/docker-compose.yml:117-118` |
| `INGEST_CONCURRENCY` | 2 | 并发流水线数（决定 `/embed` 的并发压力） | `config.py:152`、:311-316 |

## 7. 测试位置与覆盖（tests/xxx.py → 覆盖什么）

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/test_failure_classification.py:151-157` | `EmbeddingError` → `EMBEDDING_FAILED`；:39 断言 `FAILURE_CODES` 含该码 |
| `tests/test_job_progress.py:232-238` | 打桩 `embed_texts`，返回 `settings.embedding_dimension` 长度的向量（下游依赖该长度） |
| `tests/test_job_progress.py:308-328` | 阶段序列含 `EMBEDDING/80.0`，且另一会话可读 |
| `tests/test_job_progress.py:428-453` | `embed_texts` 抛错 → `stage=FAILED` 且 `progress=80.0`（失败点保留） |
| `tests/test_job_retry.py:31`、:68-75 | 以 `code=EMBEDDING_FAILED`、`progress=80` 造失败，重试回到 `RECEIVED/0.0` |
| `tests/test_stored_cleanup.py:223-243` | STORED 之后的 embedding 失败不影响已存 paper 行，staging 已清 |
| `tests/test_index_migration.py:63-78`、:98 | `knn_vector.dimension == settings.embedding_dimension`、方法体不变、`knn=true` |
| `tests/test_rerank.py:1-40` | 应用侧精排客户端用假 `httpx` 打桩（本模块只关心接口契约） |
| `tests/test_embedding_rerank_model.py`（5 例，**T-C2**） | 同上方式加载 `server.py`：内置模型不重复注册、自定义模型按 `(repo, model_file)` 注册一次、`RERANK_MODEL_FILE` 缺省为 `onnx/model.onnx`、`get_reranker()` 幂等、`/health`+`/info` 都回报 `rerank_model_file`（模型未加载时 `dimension` 为 `null`） |
| `tests/test_embedding_server_queue.py`（7 例，**T7.3 + 2026-10-07**） | 直接加载 `infra/embedding/server.py`（`fastembed` 打桩）：串行性（workers=1 峰值并发=1）、FIFO 顺序、队满抛 `QueueFull` 且计数、推理异常透传、`/embed`+`/rerank`+`/v1/embeddings` 共用队列、`/info`+`/health` 回报队列计数、**`dimension` 是加载时实测值（未加载为 `null`，加载后 = 假模型真实宽度 2）** |
| `tests/test_dimension_startup_check.py`（15 例，**2026-10-07**） | 三方对账：`mapping_embedding_dimension` 兼容两种 mapping 形状、两个探针**永不抛**（不可达/非 200/`null` → `None`）、`check_dimension_consistency` 只在实测不一致上抛（容器 768 vs 1024、索引 768 vs 1024）、未知放行 |
| `tests/test_create_index_guard.py`（2 例，**2026-10-07**） | `create_index.py --migrate-from` 在新旧索引维度不同时**在建任何索引之前**退出码 2；同维度放行到 `ensure_index` |
| `tests/test_consistency.py`（embedding 普查段，**2026-10-07**） | 单一模型不打 problem、NULL 模型 = `unknown` 只进普查、单篇跨两模型（PG 内 / PG vs 文档）报 `embedding_model_mismatch`、**跨论文**的模型分裂只是普查事实不判漂移、`as_dict` 形状 |

未覆盖：`app/services/embedding_service.py` 的**批次切分/重试/退避**仍无独立单测 —— 检索/导入路径都把它打桩掉（见上表）；2026-10-07 起其维度对账部分有独立单测（`test_dimension_startup_check.py`）。`infra/embedding/server.py` 的**模型侧行为**（真实 ONNX 推理、截断）仍无自动化测试，进了单测的是 T7.3 的队列逻辑与 2026-10-07 的维度上报；容器行为靠 `scripts/probe_embedding_queue.py` 真机验证（见 docs/progress/project.md §21.7）。

## 8. 未做 / 已知缺口

| # | 缺口 | 说明 / 证据 |
|---|---|---|
| 1 | e5 的 `query:`/`passage:` 前缀**未做** | 全仓无 `passage:` 前缀构造（`grep` 仅命中 `evals/report-jina-rerank-comparison.md:39` 的说明）；`embedding_service.py` 直接发送原文（:34），`hybrid.py:650` 发送原查询。决策与理由：**出自 docs/progress/project.md §13**（一阶段召回已非瓶颈，重算+迁移收益传导不到最终指标） |
| 2 | 池化方式（mean/cls）代码不可见 —— 未确认（由 fastembed 内部决定，本模块未配置） | `infra/embedding/server.py:161-174` 只传 `model_name`/`cache_dir`/`threads` |
| 3 | 512 token 上限未在代码中体现 —— 未确认（依据 `AGENTS.md` §3.7；容器无截断/长度校验，`fastembed` 内部如何截断未读） | `infra/embedding/server.py:161-174`、`embedding_service.py:39-44` 均无长度参数 |
| 4 | 启动对账存在**盲窗**：容器模型是惰性加载的，`/info` 在首个请求前报 `dimension=null`，此时三方对账只能 WARN 放行（配错的维度要到第一次 embed 或第一次 kNN 才炸）；写侧 `validate_dimension` 与索引 `strict` mapping 仍是兜底 | `infra/embedding/server.py:157-158`；`embedding_service.py:183-218` 对 `None` 一律放行 |
| 5 | `RERANK_MODEL_FILE` 写错要到**第一次精排**才暴露（模型惰性加载，容器 `/health` 照样 `ok`） | `infra/embedding/server.py:198-213`；`/health` 只回报配置值，不探测该文件是否存在 |
| 5 | 应用 batch 与容器 `MAX_BATCH` 无一致性校验（靠两个 env 手工对齐） | `config.py:82` vs `infra/docker-compose.yml:128` |
| 6 | `embedding_metadata()` 定义后无任何调用方（疑似遗留 API） | `embedding_service.py:158-163`；全仓 `grep` 只在定义处与 `__all__` 出现 |
| 7 | `/v1/embeddings` 无 `MAX_BATCH` 校验、`usage` token 恒 0 —— 未确认是否有外部消费者（应用不使用该端点） | `infra/embedding/server.py:222-225`、`infra/embedding/server.py:329-347` |
| 8 | 容器无内存上限与资源声明；OOM 风险靠 `MAX_BATCH`/`RERANK_MAX_BATCH`/`ORT_THREADS`/`INFERENCE_WORKERS` 四个环境变量兜住 —— 是否还有宿主 cgroup 限制未确认 | `infra/docker-compose.yml:100-149` |
| 9 | 服务端异常返回码仍不完全统一（推理失败时 `/embed` 500、`/rerank` 503），与 `AGENTS.md` §3.3 的表述不完全一致；**队满一侧已是统一 503** | `infra/embedding/server.py:270-271`、`infra/embedding/server.py:149-151`、`infra/embedding/server.py:312-313` |
| 10 | 向量只存在于 OpenSearch：PG 与索引无一致性校验（PG 有文档、索引缺向量的状态可能长期存在） | `_write_embeddings` 不写向量（`tasks.py:1217-1252`）；索引失败另走 `INDEX_FAILED`（`errors.py:205`） |
| 11 | **同一段文字被嵌两遍（T7.3 留档，未优化）**：`CHUNK_MODE=semantic` 时 `chunk_document` 先用同一个 `/embed` 给**句子**打分，紧接着 EMBEDDING 阶段又给**chunk**（= 同一批句子的拼接）嵌一次；两者没有缓存或复用，等于把这篇论文的文字嵌了近两遍 | `tasks.py:579`（chunking 侧 `embed_fn`）与 :581（EMBEDDING 侧 `embed_texts`）打到同一个 `EMBEDDING_URL`；`chunking.py:310-377` 的句级批调用；探针的磁盘向量缓存只存在于 `scripts/probe_chunk_semantic.py`，生产路径没有 |
| 12 | 服务端队列的**等待时间不可观测**：`/info` 只有计数（`waiting`/`running`/`completed`/`rejected`），没有排队时长直方图；`Retry-After: 5` 是写死的，应用侧的重试退避（0.5s/1.0s）也不读它 —— 队满时三次尝试可能全部撞墙。2026-10-01 提深度只是把阈值抬高，**没解决"客户端不读 Retry-After"**；ml-commons connector 侧已加 `max_retry_times=3` | `infra/embedding/server.py:134-143`、`infra/embedding/server.py:151`；`embedding_service.py:117`、`embedding_service.py:147` |

## 9. 换 embedding 模型 runbook（2026-10-07）

**先记住一条：换模型 = 整库重嵌，同维度也一样。** 两个模型的向量空间不相容，旧向量与新查询向量
（或反过来）算出来的距离是垃圾；维度恰好相同只说明"能写进索引"，不说明"能比"。

1. **容器侧**（`infra/.env`）：改 `EMBEDDING_MODEL`（不在 fastembed 清单里的模型另有规矩，参照
   `RERANK_MODEL_FILE` 的做法），然后 **`docker compose build embedding && docker compose up -d`**
   —— 代码是烤进镜像的，只 `up -d` 不会生效（AGENTS §3.4 的老坑）。新模型首次请求才真正下载/加载。
2. **应用侧**（根 `.env`）：`EMBEDDING_MODEL` 同步改（溯源与回显用）；**维度变了同时改
   `EMBEDDING_DIMENSION`**；`EMBEDDING_BATCH_SIZE` 与容器 `MAX_BATCH` 保持一致。改完**重启 uvicorn**
   （`.env` 启动时读入）。启动时 §5b 的对账会拦住"容器与配置不一致"的组合。
3. **索引**：维度没变 → 不用动索引；维度变了 → `knn_vector` 不可原地改，先
   `uv run python scripts/create_index.py --index paper_chunks_vN+1`（新物理索引；**不要**用
   `--migrate-from`，它现在会在维度不同时拒绝——`_reindex` 原样拷贝向量，跨维度必然是错的），
   并把 `OPENSEARCH_INDEX` 指过去。
4. **整库重嵌**（三条路等价，选顺手的）：
   * **API/界面**：`POST /api/papers/reindex`（WebUI 是论文库里的「重建全库索引」按钮）。
     先 `dry_run=true`（默认）看"选几篇、为什么"，确认后带 `dry_run=false` 才排队 ——
     整库是小时级操作，不能挂在一次请求上。换模型时它自己会报
     `embedding_model_changed`（整库范围），不用你记着"这次该全跑"。
   * **脚本**：`uv run python scripts/reindex.py --auto --dry-run` 先看清单（同一套检测），
     去掉 `--dry-run` 真跑（按 `--parser-backend` 戳或 `--degraded` 收窄的用法见 AGENTS §3.10）。
   * 界面/API 与脚本**共用同一份选择与检测实现**（`app/services/reindex_service.py`），
     只有执行方式不同：脚本在本进程同步跑（可 Ctrl-C），端点按论文入队。
   每篇论文重走 chunk→embed→index，解析产物有缓存，开销主要在 embedding。
5. **验收**：`GET /api/consistency` 看 `embedding_models.{chunks,documents}`——重嵌完成后应只剩一个
   模型名；没重嵌完的论文若两侧模型不一致，会以 `embedding_model_mismatch` 出现在 problems 里。
6. **SRW 旁路**：`paper_repr` 的向量与 connector 的 `model` 参数都是旧模型的，重跑
   `uv run python scripts/srw_setup.py build` 幂等刷新（详见 `docs/architecture/10-eval-ops.md` §9）。
7. **评测**：换模型后旧基线作废，重跑 `scripts/eval.py` 出新基线再谈指标。
