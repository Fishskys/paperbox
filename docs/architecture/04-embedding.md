# Embedding 服务与调用

| 项 | 内容 |
|---|---|
| 状态 | 主体依据 commit 54048a3（2026-09-22）；**T7.3 增补**（服务端推理队列、`paper_degradations`）已按 2026-09-30 工作树核对行号 |
| 关键文件 | `infra/embedding/server.py`、`infra/embedding/Dockerfile`、`infra/docker-compose.yml`(embedding 段)、`app/services/embedding_service.py`、`app/workers/tasks.py`(EMBEDDING 阶段)、`app/core/config.py`、`app/search/mappings.py`、`app/search/hybrid.py`(查询侧调用点) |
| 相关文档 | `AGENTS.md` §3.3/§3.7；`docs/progress/project.md` §12/§13；`docs/examine/RAG六步对照自查-20260914.md:24`；`evals/report-jina-rerank-comparison.md` |

## 1. 职责边界（做什么 / 不做什么）

做：
- 容器侧把文本批量转成 1024 维稠密向量（`POST /embed`），并提供 OpenAI 兼容入口（`POST /v1/embeddings`）与交叉编码器精排（`POST /rerank`）；`GET /info`、`GET /health` 回报能力与限批。
- 应用侧按批切分 texts、重试、超时、维度校验，把向量写进 OpenSearch，把 embedding 溯源信息写进 PostgreSQL。

不做：
- 不做 `query:`/`passage:` 前缀（e5 前缀未加，见 §8）。
- 不做文本截断/分块：容器不对入参做 token 限长（`server.py` 中无 `max_length`/截断逻辑），分块由 `app/parsing/chunking.py` 负责（`DEFAULT_TARGET_TOKENS=400`、`MAX_TOKENS=450`，`app/parsing/chunking.py:41-42`）。
- 向量不落 PostgreSQL：PG 只存 `embedding_model`/`embedding_dimension`/`embedded_at`（`app/workers/tasks.py:1093-1116`）。
- 本模块不定义精排/改写策略（候选窗口、降级语义、双分数属 06 号文档）。

## 2. 关键文件与函数（文件 → 函数/类 → 作用，带行号）

| 文件 | 函数/类 | 作用 |
|---|---|---|
| `infra/embedding/server.py` | `QueueFull` :62 / `InferenceQueue` :66（`submit()` :116、`stats()` :130）/ `QUEUE` :142 / `_busy()` :145 | **T7.3 服务端队列**：所有推理排进一条 FIFO，队满回 503+`Retry-After` |
| | `get_model()` :155 / `register_custom_reranker()` :166 / `get_reranker()` :187 | 惰性单例；首次调用才加载/下载模型（加载本身也在队列里执行）。`register_custom_reranker` 把**不在 fastembed 清单里**的交叉编码器（例如 int8 量化导出）用 `add_custom_model` 注册一次 —— 幂等，内置模型直接跳过 |
| | `EmbedRequest` :205 / `OpenAIEmbedRequest` :211 / `RerankRequest` :218 | 入参模型（`extra="ignore"`） |
| | `health()` :226 / `info()` :240 | `/health`、`/info`（两者都回报 `queue`/`inference` 计数） |
| | `embed()` :253 | `/embed` 批量向量（经 `QUEUE.submit`） |
| | `rerank()` :272 | `/rerank` 交叉编码器打分（同一条队列） |
| | `openai_compat()` :319 | `/v1/embeddings` OpenAI 形态（同一条队列） |
| `app/services/embedding_service.py` | `_endpoint()` :28 / `_post_batch()` :33 | 拼 URL；单批 POST + 响应解析 |
| | `validate_dimension()` :67 | 逐向量核对维度 |
| | `embed_texts()` :77 | 分批 + 重试 + 退避（主入口） |
| | `embed_text()` :147 | 单条包装（查询侧用） |
| | `embedding_metadata()` :152 | 返回 model/dimension 字典（当前无调用方） |
| `app/workers/tasks.py` | `_advance_stage()` :194 | 阶段标记并 COMMIT（可见性） |
| | `_run_pipeline()` :446 起；CHUNKING 段 :518-559（`on_degrade=degradations` :552、`degradations.resolve(...)` :559）；EMBEDDING 段 :561-569 | 调用 embed、数量核对、写溯源；降级留痕（T7.3） |
| | `_write_embeddings()` :1099 | PG 侧 model/dimension/embedded_at |
| | `_index_rows()` :1135 | 构造待索引文档（含 `embedding`） |
| | `_record_failure()` :1201 | FAILED 簿记（`classify_failure` → `error_code`） |
| `app/search/hybrid.py` | `_semantic_hits()` :624 | 查询侧 `embed_text(query)`，把 `EmbeddingError` 转 `SearchError` |
| `app/core/errors.py` | `classify_failure()` :127；`EmbeddingError` 分支 :179 | `EMBEDDING_FAILED` 归类 |
| `app/services/degradation_service.py` | `record()` :81 / `resolve_stage()` :133 / `Recorder` :226 | **T7.3 降级账本**：`(stage, code, detail)` 落 `paper_degradations` |

## 3. 数据结构（表/字段/索引，或内存结构）

容器入参/出参：

| 接口 | 入参 | 出参 | 校验位置 |
|---|---|---|---|
| `POST /embed` | `{"texts": [...], "model"?: str}`，`min_length=1, max_length=MAX_BATCH` | `{"embeddings":[[...]],"model","dimension","latency_ms","count"}`；队满 `503`+`Retry-After: 5` | `server.py:205-207`（pydantic，超批 422）；返回的 `dimension` 取第一个向量的实际长度（`server.py:256`）；入队点 `server.py:256`（`QUEUE.submit`） |
| `POST /v1/embeddings` | `{"model"?: str, "input": str\|list[str], "encoding_format"?: str}` | OpenAI 形态 `{"object":"list","model","data":[{"embedding":[...]}],"usage":{...},"latency_ms"}` | 无长度上限（`server.py:211-214`）；`usage` token 恒为 0（`server.py:325`） |
| `POST /rerank` | `{"query": str, "documents": [...], "top_n"?: int}` | `{"results":[{"index","score"}...],"model","took_ms"}`，按 score 降序 | 无入参长度上限；服务内按 `RERANK_MAX_BATCH` 分片（`server.py:275-280`） |
| `GET /info` | — | `{"model","dimension":1024,"max_batch","rerank_model","rerank_max_batch","inference":{workers,queue_depth,waiting,running,completed,rejected}}` | `dimension` 硬编码 1024（`server.py:243`），不随模型变化 |
| `GET /health` | — | `{"status":"ok","model","dimension":1024,"loaded","rerank_model","rerank_loaded","queue":{...}}` | 模型未加载也返回 200（惰性加载，`server.py:226-237`） |

PostgreSQL（`app/db/models.py`）：

| 表 | 字段 | 说明 |
|---|---|---|
| `papers` | `embedding_model` :132 / `embedding_dimension` :133 | STORED 阶段先写（`tasks.py:279-280`），EMBEDDING 后重申（:1108-1109） |
| `paper_chunks` | `embedding_model` :402 / `embedding_dimension` :399 / `embedded_at` :400 / `doc_metadata`(JSONB) :398 | 不含向量；`doc_metadata["embedding_dimension"]` 记的是返回向量实际长度（`tasks.py:1082-1084`） |
| `ingestion_jobs` | `stage` :432 / `progress` :435 / `error_code` :440 / `error_message` :441 | 失败时保留失败阶段与进度 |
| `paper_degradations` | `stage` :725 / `code` :707 / `detail`(JSONB) :709 / `occurrences` :712 / `first_seen_at` :715 / `last_seen_at` :718 / `resolved_at` :723 / `job_id` :705 | **T7.3 降级账本**：`UNIQUE(paper_id, stage, code)`；`resolved_at IS NULL` = 当前仍然成立 |

OpenSearch（`app/search/mappings.py`）：`embedding` = `knn_vector`，`dimension = settings.embedding_dimension`，`hnsw/l2/lucene`，`ef_construction=128`、`m=16`（:125-134）；`embedding_model`(keyword)/`embedding_dimension`(integer)（:135-136）；索引级 `knn=true`（:156）。文档体由 `_index_rows()` 组装（`tasks.py:1160-1161` 的 payload 起始）并经 `build_chunk_document()`（`app/search/opensearch.py:243-285`）落库，`embedding` 仅在非空时写入（`opensearch.py:243-245`）。

## 4. 调用链（从入口到落地，逐跳，带函数名）

写入（ingest/reindex）：
1. `run_ingestion_job()`（`tasks.py:141`）/ `run_reindex_job()`（:121）→ `_process_job()`（:213）→ `_run_pipeline()`（:446）。
2. `chunk_document()` 出 chunks（语义模式下它会**先**调用同一个 `/embed` 给句子打分，见 §8 缺口 11）→ `_replace_chunks()` 落 PG（:518-559）。
3. `_advance_stage(session, job, STAGE_EMBEDDING, PROGRESS_EMBEDDING=80.0)`（:601，常量 :56/:66）→ COMMIT，此刻 `GET /api/jobs/{id}` 已能看到 `EMBEDDING/80`。
4. `embedding_service.embed_texts([chunk.text for chunk in chunks])`（:563）。
5. `embed_texts` 内：`settings.embedding_batch_size`（16）切片 → `_post_batch()`（`embedding_service.py:33`）→ `httpx.Client(timeout=EMBEDDING_TIMEOUT)` POST `{EMBEDDING_URL}/embed`。
6. 容器：`embed()`（`server.py:253`）→ `QUEUE.submit(...)`（`server.py:254`）→ **排队** → 工作线程里执行 `fn(...)`（`server.py:107`，即 `get_model().embed(...)`，fastembed/ONNX）。队满则第 5 跳直接拿到 `503`+`Retry-After`（`server.py:124-128` 抛 `QueueFull`、:145-151 转 503），应用按第 90 行表格的退避重试。
7. 回程校验：向量条数 == 批大小（`embedding_service.py:114-118`）→ `validate_dimension()`（:119）→ 拼接。
8. `len(vectors) != len(rows)` → `IngestionError`（`tasks.py:564-567`）。
9. `_write_embeddings()`（:569）写 PG 溯源 → `_advance_stage(INDEXING, 95.0)`（:571）。
10. `opensearch.ensure_index()` → `delete_by_paper_id()` → `_index_rows()` → `bulk_index_chunks()`（:549-552，`opensearch.py:364`，`BULK_BATCH_SIZE` 默认 200 + `refresh=True`）→ `_mark_indexed()`（:557）。

查询侧（策略细节见 06）：
`POST /api/search` → `search_service.search_papers` → `hybrid.search_chunks()`（`hybrid.py:651`）→ `_semantic_hits()`（:624）→ `embed_text(query)`（:633）→ `EmbeddingError` 被转成 `SearchError`（:634-635）→ `app/api/search.py:89-93` 返回 503（查询侧 embedding 失败**不会**退化成纯关键词）。精排另走 `rerank_service.rerank_texts()` → `POST /rerank`（客户端契约见 `app/services/rerank_service.py:1-14`）。

### 4b. 服务端排队（T7.3）

**问题**：`/embed` 是同步 `def`，Starlette 会把它丢进 anyio 线程池（默认 ~40 线程），每个请求各自跑一次 ONNX 推理；`INGEST_CONCURRENCY`(2) 的流水线、语义分块的 CHUNKING 段、查询侧精排又都打同一个容器。并发不会遭拒，只会互相抢 CPU/内存（历史上 uvicorn 被 oom-killer 杀掉就是这条路），延迟同时被拉长。

**做法**：容器内部加一条 FIFO 队列 `InferenceQueue`（`server.py:66`）。`/embed`、`/v1/embeddings`、`/rerank` 三处都改成 `QUEUE.submit(fn)`：调用线程把 `(fn, args, future)` 入队，`INFERENCE_WORKERS`(默认 1) 个工作线程依次取出执行，调用线程在 `future.result()` 上等结果（异常原样透传，所以 500/503 语义不变）。

- 积压长度由 `INFERENCE_QUEUE_DEPTH` 兜底：满了**立即**抛 `QueueFull` → `503` + `Retry-After: 5`（`server.py:124-128`、:145-151）。选择“拒绝而不是无限排队”：无界排队会把等待推到客户端超时之后，那时两边都拿不到可用的错误。
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
| 1 | 应用 batch 必须等于容器 `MAX_BATCH`，但两侧分属两个 env 文件、代码无一致性校验；不等即 422、导入整体失败 | 应用 `config.py:78`=.env 16；容器 `infra/docker-compose.yml:108`=16；告警写在 `infra/.env.example:33` |
| 2 | embedding 与精排的限批必须解耦：`MAX_BATCH` 同时管 `/embed` 上限，压小会让导入全 422 | `server.py:42-47` 注释；`docker-compose.yml:96-98` |
| 3 | 每批新建 `httpx.Client`，无连接复用；批次串行、批内重试，单条流水线内部无并发（并发来自多条流水线） | `embedding_service.py:37`（with 块）、:108-121（顺序循环） |
| 4 | 重试 = `EMBEDDING_MAX_RETRIES`(2) 次额外尝试，退避 `0.5 * 2**attempt`（0.5s、1.0s）；超时 = 单批 `EMBEDDING_TIMEOUT`(**300s**，T7.3 起：服务端排队后它必须大于最坏排队时间，否则排队会变成失败) | `embedding_service.py:111`、:129；`config.py:83-87` |
| 5 | 维度在三处各说各话：应用 `validate_dimension`（settings 1024）、mapping `dimension`（settings 1024）、容器返回实际长度；无启动期一致性校验 | `embedding_service.py:67-74`、`mappings.py:124`、`server.py:256` |
| 6 | 容器失败码：推理本身出错 `/embed` = **500**、`/rerank` = **503**；T7.3 起**队满一侧统一 503**（带 `Retry-After: 5`）——`AGENTS.md` §3.3 的“未就绪返回 503”只对精排与“队满”成立 | `server.py:259-260`（500） vs :124-128 + :145-151（队满 503）、:313-314（精排 503） |
| 7 | `/v1/embeddings` 不受 `MAX_BATCH` 限制，且应用侧不使用它（应用只走 `/embed`）；它同样排在队列后面 | `server.py:211-214`、:318-322；`embedding_service.py:21`（`EMBED_PATH="/embed"`） |
| 8 | 向量条数不匹配（第 8 跳）落 `IngestionError`，不在 `EmbeddingError` 分支 → `error_code=INTERNAL`，不是 `EMBEDDING_FAILED` | `tasks.py:518-539`；`errors.py:152-197`（`EmbeddingError` 分支 :182，尾部兜底 :200） |
| 9 | 失败保留现场：`_advance_stage` 已 COMMIT，`EMBEDDING/80` 是可读的失败点；`_record_failure` 只改 job 与 paper.status。降级账本骑在同一个事务上：作业回滚则降级行一并回滚（不是漏记——那次运行没留下产物） | `tasks.py:194-210`、:1201-1220；`degradation_service.py:226-283` |
| 9b | **T7.3 服务端队列**：`/embed`、`/v1/embeddings`、`/rerank` 共用一条 FIFO，由 `INFERENCE_WORKERS`(1) 个工作线程串行执行；积压 > `INFERENCE_QUEUE_DEPTH`（2026-10-01 起 512）直接 503，而不是无限排队（无界排队只会把等待推到客户端超时之后） | `server.py:56-59`、:66、:142 |
| 10 | 模型惰性加载：首个请求才下载/加载（`/health` 在加载前也 200）；healthcheck 15s×30 次容错下载窗口；**首次加载发生在队列工作线程里**，所以冷启动期间其余请求都在排队等待 | `server.py:155-202`；`docker-compose.yml:110-114` |
| 11 | `/info`、`/health` 的 `dimension` 是硬编码 1024，换模型不会自动修正 | `server.py:243`、:229 |
| 12 | 容器单进程（`--workers 1`）：embedding 与 rerank 共用一个进程/ORT 线程池 | `Dockerfile:9` |
| 13 | embedding 容器**没有任何内存上限**（compose 段无 `mem_limit`/`deploy.resources`），稳定性依赖 `MAX_BATCH`/`RERANK_MAX_BATCH`/`ORT_THREADS` 三个阀 + T7.3 的 `INFERENCE_WORKERS`（同时推理数） | `docker-compose.yml:80-118` |
| 14 | 内存实测：`RERANK_MAX_BATCH=4` 是硬约束（16 候选峰值 5.10GB、4 候选 2.36GB；3GB 封顶那次被 OOM-kill exit 137）；`ORT_THREADS=4` + embed batch 16 是安全点（8 线程+大 batch 曾把 uvicorn 打成 5.7GB 被 oom-killer 杀）—— **出自 docs/progress/project.md §13** 与 `docker-compose.yml:95-98` 注释 |
| 15 | WSL 内存线：`.wslconfig` `memory=9GB`、`free -h` 实测 8.7GiB —— **出自 docs/progress/project.md §13** |
| 16 | 应用并发上限 `INGEST_CONCURRENCY`(默认 2) ⇒ 最多 2 条流水线同时打同一个 `/embed`；叠加语义分块的 CHUNKING 段与查询侧，队列是唯一的削峰点 | `config.py:152`；`app/workers/queue.py:6` 注释 |
| 17 | 空入参 `embed_texts([])` 直接返回 `[]`；空字符串照发（保证 1:1 对齐） | `embedding_service.py:95-96`；docstring :85-86 |
| 18 | 异常信息只带批次 offset 与上游文本，不打全文（无原文泄漏） | `embedding_service.py:125-128` |

## 6. 配置项（键 → 默认值 → 作用 → 出处文件:行）

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `EMBEDDING_URL` | `http://localhost:8090`（`.env` 实为 `http://127.0.0.1:8090`） | 应用侧 `/embed` base | `app/core/config.py:79`、`.env:19` |
| `EMBEDDING_MODEL` | 代码 `BAAI/bge-m3`；`.env`/compose `intfloat/multilingual-e5-large` | 容器加载的模型名；应用仅作为请求字段与溯源值 | `config.py:80`、`server.py:40`、`docker-compose.yml:86`、`.env:20` |
| `EMBEDDING_DIMENSION` | 1024 | 应用侧维度校验 + mapping `dimension` | `config.py:81`、`.env:21` |
| `EMBEDDING_BATCH_SIZE` | 代码 32；`.env`/`.env.example` 16 | 单次 `/embed` 的 texts 数 | `config.py:82`、:292-297（正数校验）、`.env:22`、`.env.example:32` |
| `EMBEDDING_TIMEOUT` | **300.0**（T7.3 由 120 上调） | 单批 HTTP 超时；必须大于服务端最坏排队时间 | `config.py:83-87`、`.env.example:35` |
| `EMBEDDING_MAX_RETRIES` | 2 | 每批额外重试次数 | `config.py:88`、`.env.example:36` |
| `MAX_BATCH` | 代码 64；compose `16`（`infra/.env` 未设） | `/embed` 入参上限（超批 422） | `server.py:42`、`docker-compose.yml:99` |
| `RERANK_MAX_BATCH` | 代码 = `MAX_BATCH`；compose 兜底 `16`；`infra/.env` **4** | 单次精排推理的候选上限（服务内分片）。**批越大越慢越占内存**：50 候选实测批 4/8/16 = 3.2/4.0/4.8 秒每调用、匿名峰值 2351/2555/3199 MiB（int8 档） | `server.py:47`、`docker-compose.yml:103`、`infra/.env:22` |
| `ORT_THREADS` | 代码 2；compose `4` | ONNX Runtime 线程数（embedding 与 rerank 共用） | `server.py:53`、`docker-compose.yml:98` |
| `INFERENCE_WORKERS` | 代码 1；compose `1`；`infra/.env` 1 | **T7.3** 同时执行推理的工作线程数（=1 即完全串行） | `server.py:56`、`infra/docker-compose.yml:118`、`infra/.env:44` |
| `INFERENCE_QUEUE_DEPTH` | 代码 32；compose `512`；`infra/.env` 512 | **T7.3** 允许排队等待的请求数上限，超出直接 503；**2026-10-01 提档**，见 §4b 的定值规矩（`depth × 单次推理耗时 ≤ EMBEDDING_TIMEOUT/2`） | `server.py:59`、`infra/docker-compose.yml:119`、`infra/.env:45` |
| `RERANK_MODEL` | 代码 `Xenova/ms-marco-MiniLM-L-6-v2`；`infra/.env` `temsa/mmarco-mMiniLMv2-L12-H384-v1-onnx-cpu-qint8`（2026-10-01 全量换档；jina 作为高配机器选项留在注释里，见 `docs/progress/project.md` §22.u） | 交叉编码器；名字在应用侧与容器侧**必须一致**（应用只用它回显 `rerank.model`，容器才真正加载）。**改完要重启应用**（`.env` 启动时读入，否则响应会报旧模型名） | `server.py:41`、`config.py:92`、`infra/.env:13`、`.env:29` |
| `RERANK_MODEL_FILE` | 代码 `onnx/model.onnx`；`infra/.env` `model.onnx`（现役 int8 档需要它） | **只对不在 fastembed 清单里的模型生效**：仓库内 ONNX 文件路径。多数 HF 导出在 `onnx/` 子目录，动态 int8 量化导出常放仓库根（就是这个档）。换回内置档（jina/ms-marco）时注释掉 | `server.py:52`、`docker-compose.yml:96`、`infra/.env:14` |
| `RERANK_ENABLED` / `RERANK_URL` / `RERANK_TIMEOUT` / `RERANK_CANDIDATES` | `true` / `http://127.0.0.1:8090` / `10.0`（`.env:49`=60）/ `5` | 精排开关、地址、超时、候选倍数（细节见 06） | `config.py:91-103` |
| `FASTEMBED_CACHE_PATH` | `/models` | 模型缓存目录（挂载宿主目录） | `docker-compose.yml:90`、:109 |
| `HF_ENDPOINT` / `HF_HUB_DISABLE_XET` | `https://hf-mirror.com` / `1` | 首次下载的镜像与传输开关 | `docker-compose.yml:88-89` |
| `INGEST_CONCURRENCY` | 2 | 并发流水线数（决定 `/embed` 的并发压力） | `config.py:152`、:307-312 |

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
| `tests/test_embedding_rerank_model.py`（5 例，**T-C2**） | 同上方式加载 `server.py`：内置模型不重复注册、自定义模型按 `(repo, model_file)` 注册一次、`RERANK_MODEL_FILE` 缺省为 `onnx/model.onnx`、`get_reranker()` 幂等、`/health`+`/info` 都回报 `rerank_model_file` |
| `tests/test_embedding_server_queue.py`（6 例，**T7.3**） | 直接加载 `infra/embedding/server.py`（`fastembed` 打桩）：串行性（workers=1 峰值并发=1）、FIFO 顺序、队满抛 `QueueFull` 且计数、推理异常透传、`/embed`+`/rerank`+`/v1/embeddings` 共用队列、`/info`+`/health` 回报队列计数 |

未覆盖：`app/services/embedding_service.py` 没有独立单测 —— 所有用例都把它打桩掉（见上表）。即批次切分/重试/退避/维度校验逻辑只在真实链路中被间接使用。`infra/embedding/server.py` 的**模型侧行为**（真实 ONNX 推理、维度、截断）仍无自动化测试，只有 T7.3 的队列逻辑进了单测；容器行为靠 `scripts/probe_embedding_queue.py` 真机验证（见 docs/progress/project.md §21.7）。

## 8. 未做 / 已知缺口

| # | 缺口 | 说明 / 证据 |
|---|---|---|
| 1 | e5 的 `query:`/`passage:` 前缀**未做** | 全仓无 `passage:` 前缀构造（`grep` 仅命中 `evals/report-jina-rerank-comparison.md:39` 的说明）；`embedding_service.py` 直接发送原文（:34），`hybrid.py:633` 发送原查询。决策与理由：**出自 docs/progress/project.md §13**（一阶段召回已非瓶颈，重算+迁移收益传导不到最终指标） |
| 2 | 池化方式（mean/cls）代码不可见 —— 未确认（由 fastembed 内部决定，本模块未配置） | `server.py:155-163` 只传 `model_name`/`cache_dir`/`threads` |
| 3 | 512 token 上限未在代码中体现 —— 未确认（依据 `AGENTS.md` §3.7；容器无截断/长度校验，`fastembed` 内部如何截断未读） | `server.py:155-163`、`embedding_service.py:33-38` 均无长度参数 |
| 4 | 换模型不会自动更新自报维度：`/info`、`/health` 硬编码 1024 | `server.py:243`、:229 |
| 5 | `RERANK_MODEL_FILE` 写错要到**第一次精排**才暴露（模型惰性加载，容器 `/health` 照样 `ok`） | `server.py:187-196`；`/health` 只回报配置值，不探测该文件是否存在 |
| 5 | 应用 batch 与容器 `MAX_BATCH` 无一致性校验（靠两个 env 手工对齐） | `config.py:82` vs `docker-compose.yml:99` |
| 6 | `embedding_metadata()` 定义后无任何调用方（疑似遗留 API） | `embedding_service.py:152-157`；全仓 `grep` 只在定义处与 `__all__` 出现 |
| 7 | `/v1/embeddings` 无 `MAX_BATCH` 校验、`usage` token 恒 0 —— 未确认是否有外部消费者（应用不使用该端点） | `server.py:211-214`、:323-326 |
| 8 | 容器无内存上限与资源声明；OOM 风险靠 `MAX_BATCH`/`RERANK_MAX_BATCH`/`ORT_THREADS`/`INFERENCE_WORKERS` 四个环境变量兜住 —— 是否还有宿主 cgroup 限制未确认 | `docker-compose.yml:80-118` |
| 9 | 服务端异常返回码仍不完全统一（推理失败时 `/embed` 500、`/rerank` 503），与 `AGENTS.md` §3.3 的表述不完全一致；**队满一侧已是统一 503** | `server.py:259-260`、:145-151、:313-314 |
| 10 | 向量只存在于 OpenSearch：PG 与索引无一致性校验（PG 有文档、索引缺向量的状态可能长期存在） | `_write_embeddings` 不写向量（`tasks.py:1099-1134`）；索引失败另走 `INDEX_FAILED`（`errors.py:182`） |
| 11 | **同一段文字被嵌两遍（T7.3 留档，未优化）**：`CHUNK_MODE=semantic` 时 `chunk_document` 先用同一个 `/embed` 给**句子**打分，紧接着 EMBEDDING 阶段又给**chunk**（= 同一批句子的拼接）嵌一次；两者没有缓存或复用，等于把这篇论文的文字嵌了近两遍 | `tasks.py:552`（chunking 侧 `embed_fn`）与 :563（EMBEDDING 侧 `embed_texts`）打到同一个 `EMBEDDING_URL`；`chunking.py:279-346` 的句级批调用；探针的磁盘向量缓存只存在于 `scripts/probe_chunk_semantic.py`，生产路径没有 |
| 12 | 服务端队列的**等待时间不可观测**：`/info` 只有计数（`waiting`/`running`/`completed`/`rejected`），没有排队时长直方图；`Retry-After: 5` 是写死的，应用侧的重试退避（0.5s/1.0s）也不读它 —— 队满时三次尝试可能全部撞墙。2026-10-01 提深度只是把阈值抬高，**没解决"客户端不读 Retry-After"**；ml-commons connector 侧已加 `max_retry_times=3` | `server.py:130-140`、:148；`embedding_service.py:111`、:138` |
