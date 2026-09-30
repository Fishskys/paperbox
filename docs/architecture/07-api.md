# HTTP API 层

| 项 | 内容 |
|---|---|
| 状态 | 依据 commit 54048a3 的工作树实测（2026-09-22） |
| 关键文件 | `app/main.py`、`app/api/{health,ingestion,jobs,metadata,papers,search,search_logs}.py`、`app/core/{security,errors,config,logging}.py`、`app/schemas/*.py`、`app/workers/{queue,housekeeping}.py`、`app/db/session.py` |
| 相关文档 | `docs/architecture/MVP-SPEC.md` §0/§2/§3、`README.md` §3.4/§4/§7、`AGENTS.md` §3.8/§3.9（冲突处以代码为准，见 §5、§8） |

## 1. 职责边界（做什么 / 不做什么）

做什么：

- 把 HTTP 请求翻译成服务层调用：路由声明、请求体解析、Bearer 鉴权、错误码映射、响应模型序列化。
- 拥有进程级生命周期：`lifespan` 启动/停止摄取队列（`app/workers/queue.py`）与 housekeeping GC（`app/workers/housekeeping.py`），见 §2.4。
- 拥有请求级横切：`X-Request-ID` 中间件（`app/main.py:70-79`）、请求级 DB session（`app/db/session.py:70-76`）。

不做什么：

- 不实现业务逻辑：摄取流水线在 `app/workers/tasks.py`，检索在 `app/search/hybrid.py` + `app/services/search_service.py`，元数据合并在 `app/services/metadata_*.py`。本层只调用。
- 不做鉴权以外的安全控制：无 CORS（未发现任何 `CORSMiddleware`/`add_middleware` 注册）、无全局限流（仅上传准入，见 `app/services/upload_admission.py`）。
- 不注册任何异常处理器（全仓 grep `exception_handler` 0 命中），错误体形状由 FastAPI 默认处理器决定。
- `app/core/errors.py` 不负责 HTTP 状态映射：它是**作业失败归因**（写入 `ingestion_jobs`），详见 §2.3。

## 2. 关键文件与函数（文件 → 函数/类 → 作用，带行号）

### 2.1 路由总表（28 条，包含不在 schema 的 `GET /`）

`Bearer` 列：`是` = 该 router 构造时带 `dependencies=[Depends(require_api_key)]`。

| 方法 | 路径 | 作用 | Bearer | 关键查询参数 | 出处 |
|---|---|---|---|---|---|
| GET | `/health` | 四个依赖的轻量探针 | 免 | — | `app/api/health.py:79` |
| GET | `/api/consistency` | 三端只读对账（PG / MinIO / OpenSearch） | 是 | `limit`（1..1000，默认 200）、`parser_papers`（bool，默认 false：连每个解析戳下的存活论文 id 一起返回，即 `parser_backends.paper_ids`） | `app/api/consistency.py:38` |
| GET | `/` | landing（`include_in_schema=False`） | 免 | — | `app/main.py:92` |
| POST | `/api/papers/ingest` | URL 摄取 | 是 | — | `app/api/ingestion.py:238` |
| POST | `/api/papers/ingest/files` | multipart 多文件（`files` 可重复） | 是 | — | `app/api/ingestion.py:257` |
| POST | `/api/papers/ingest/dir` | 服务端目录导入（零传输） | 是 | — | `app/api/ingestion.py:356` |
| POST | `/api/papers/ingest/file` | 单文件（`/files` 的薄封装） | 是 | — | `app/api/ingestion.py:513` |
| POST | `/api/papers/ingest/compressed` | 上传 zip 解包导入 | 是 | — | `app/api/ingestion.py:567` |
| GET | `/api/jobs/queue` | 队列深度快照 | 是 | — | `app/api/jobs.py:21` |
| GET | `/api/jobs/{job_id}` | 单作业阶段/进度 | 是 | — | `app/api/jobs.py:33` |
| POST | `/api/jobs/{job_id}/retry` | 重驱动 FAILED 作业（202） | 是 | — | `app/api/jobs.py:44` |
| GET | `/api/jobs` | 最近作业列表 | 是 | `limit`（默认 20，无上下界） | `app/api/jobs.py:75-80` |
| GET | `/api/papers` | 论文列表 | 是 | `limit`、`offset`、`status`、`q`、`venue`、`year_from`、`year_to`、`paper_type`、`tag` | `app/api/papers.py:65-80` |
| GET | `/api/papers/{paper_id}` | 单篇元数据 | 是 | — | `app/api/papers.py:110` |
| GET | `/api/papers/{paper_id}/file` | 原文流式下载（attachment） | 是 | — | `app/api/papers.py:118` |
| GET | `/api/papers/{paper_id}/chunks` | chunk 分页列表 | 是 | `limit`、`offset`（夹取 1..200） | `app/api/papers.py:157`, `papers.py:162` |
| GET | `/api/papers/{paper_id}/degradations` | **T7.3** 降级账本：该论文各阶段「能用但更薄」的原因 | 是 | `include_resolved`（默认 false，只列仍未消解的） | `app/api/papers.py:201` |
| GET | `/api/papers/{paper_id}/metadata` | 当前值 + 每字段溯源 | 是 | — | `app/api/papers.py:242` |
| PATCH | `/api/papers/{paper_id}/metadata` | 手动改元数据 | 是 | — | `app/api/papers.py:256` |
| POST | `/api/papers/{paper_id}/metadata/rollback` | 单字段回滚到历史主张 | 是 | — | `app/api/papers.py:276` |
| DELETE | `/api/papers/{paper_id}` | 删除（204） | 是 | — | `app/api/papers.py:302` |
| POST | `/api/papers/{paper_id}/reindex` | 重建索引（202） | 是 | — | `app/api/papers.py:345` |
| POST | `/api/metadata/import` | 外部元数据导入 | 是 | `dry_run`（默认 true）、`apply`、`limit`、`source_type` | `app/api/metadata.py:92-102` |
| GET | `/api/metadata/review` | 复核清单 + 已登记冲突 | 是 | `status`（可重复）、`limit`（1..200，默认 50） | `app/api/metadata.py:147-151` |
| POST | `/api/metadata/sources/{source_id}/attach` | 人工归属来源 | 是 | — | `app/api/metadata.py:164` |
| POST | `/api/metadata/apply` | 按报告批量应用人工决定 | 是 | — | `app/api/metadata.py:217` |
| POST | `/api/search` | 论文级混合检索 | 是 | — | `app/api/search.py:49` |
| GET | `/api/search-logs` | 检索日志（只读） | 是 | `limit`、`since`、`mode` | `app/api/search_logs.py:27-32` |

`/docs`、`/redoc`、`/openapi.json` 由 FastAPI 默认挂载（`app/main.py:59-67` 未加保护），匿名可读。`POST /api/search` 不注入 DB session（`app/api/search.py:50` 只收 body），日志另开 session：`app/api/search.py:197`。

### 2.2 鉴权实现

| 环节 | 实现 | 出处 |
|---|---|---|
| 取凭证 | `Authorization` 头优先；`partition(" ")` 后 scheme 必须为 `bearer`（忽略大小写）且凭证非空 | `app/core/security.py:25-30` |
| 无 `Authorization` 时回退 | 读 `X-API-Key` | `app/core/security.py:19`, `:32-35` |
| 有 `Authorization` 但 scheme 非 Bearer | 直接返回 `None`，**不回退** `X-API-Key` | `app/core/security.py:30` |
| 比对 | `hmac.compare_digest`，期望值 `settings.paper_api_key` | `app/core/security.py:38-43`, `app/core/config.py:77` |
| 缺凭证 | `401 {"detail":"Missing API key"}` + `WWW-Authenticate: Bearer` | `app/core/security.py:49-54` |
| 凭证不符 | `403 {"detail":"Invalid API key"}` + 同头 | `app/core/security.py:55-60` |
| 依赖挂载 | 每个 router 构造时 `dependencies=[Depends(require_api_key)]` | `ingestion.py:59-63`、`jobs.py:14-18`、`papers.py:31-35`、`metadata.py:40-44`、`search.py:40-44`、`search_logs.py:20-24` |
| 豁免 | `health.py:25` 无 dependencies；`GET /`（`main.py:92`）、`/docs` 等框架端点无保护 | 同上 |

`require_api_key` 只是 `verify_api_key` 的别名函数（`app/core/security.py:64-66`），测试用 `app.dependency_overrides[require_api_key]` 整体绕过（如 `tests/test_ingest_files.py:124`）。空 `PAPER_API_KEY`（`expected` 为空）→ `api_key_matches` 返回 `False`（`security.py:41-42`），即**所有受保护请求 403**，不是开放访问。

### 2.3 错误处理

两个互不相干的机制：

1. **HTTP 错误** = 各 endpoint 内手写 `raise HTTPException(...)`。没有统一异常处理器，也没有业务异常基类到状态的集中映射；FastAPI 默认处理器输出字符串 `detail`，与 `docs/architecture/MVP-SPEC.md:108`「统一 `{"detail": "…"}`」一致。
2. **作业失败归因** = `app/core/errors.py:127 classify_failure()` 把流水线异常映射成 14 个稳定 code（`errors.py:48-63`）：`NO_TEXT_LAYER`、`ENCRYPTED_PDF`、`CORRUPT_PDF`、`DOWNLOAD_FAILED`、`OVERSIZED`、`UNSUPPORTED_TYPE`、`DUPLICATE_FINGERPRINT`、`PARSE_BACKEND_UNAVAILABLE`、`PARSE_FAILED`、`EMBEDDING_FAILED`、`INDEX_FAILED`、`STORAGE_FAILED`、`INTERRUPTED`、`INTERNAL`（后两个解析码只在「明确要求 docling 且不许降级」时出现 —— 正常流水线降级到 pypdf 并记 `degraded_reason`/`paper_degradations`，见 `03-parsing-chunking.md`）。它写进作业行，经 `GET /api/jobs/{job_id}` 的 `error_code` 暴露（`app/schemas/job.py:20-23`）。调用点：`app/api/ingestion.py:79`、`app/api/ingestion.py:449`、`app/workers/tasks.py:1202`。

业务异常 → HTTP 状态映射（全部为端点内显式 raise）：

| 触发 | 状态 | 出处 |
|---|---|---|
| URL 非法（`ingest.UnsupportedSource`） | 422 | `app/api/ingestion.py:246-249` |
| `local_scan.ScanUnavailable`（白名单为空 = 端点关闭） | 404 | `app/api/ingestion.py:379-380` |
| `local_scan.RootNotAllowed`（越界/`..`/链接逃逸） | 403 | `app/api/ingestion.py:381-382` |
| `local_scan.RootMissing` | 404 | `app/api/ingestion.py:383-384` |
| `/files`：0 个文件或超 `INGEST_MAX_FILES_PER_REQUEST` | 422 | `app/api/ingestion.py:280-294` |
| `/files`：声明总字节超 `INGEST_MAX_REQUEST_MB` | 413 | `app/api/ingestion.py:302-309` |
| `/files`：批请求 + 队列积压 ≥ 水位 | 429 + `Retry-After` | `app/api/ingestion.py:316-317`, `:66-72` |
| `upload_admission.AdmissionRejected`（在途上传超限） | 429 + `Retry-After` | `app/api/ingestion.py:332-333`；`:541-542`；`:626-628` |
| `/compressed`：非 zip 魔数 | 415 | `app/api/ingestion.py:595-602` |
| `/compressed`：`ArchiveError`（gzip-bomb/zip-slip 等上限） | 422 | `app/api/ingestion.py:629-634` |
| `/compressed`：解包其它异常 | 500 | `app/api/ingestion.py:635-641` |
| `/file`：单文件被判 rejected | 422 | `app/api/ingestion.py:544-548` |
| `/file`：staging 后作业行消失 | 503 | `app/api/ingestion.py:552-555` |
| `GET /api/jobs/{id}` / `POST .../retry`：作业不存在 | 404 | `app/api/jobs.py:38-40`, `:61-64` |
| `POST /api/jobs/{id}/retry`：非 FAILED | 409 | `app/api/jobs.py:66-70` |
| `papers` 路由：论文不存在（含软删） | 404 | `app/api/papers.py:59-61` |
| 原文对象缺失 / `ObjectNotFound` | 404 | `app/api/papers.py:124-126`, `:122-125` |
| `ObjectStorageError`（读文件） | 503 | `app/api/papers.py:134-138` |
| rollback 的 `provenance_id` 不属该论文/字段 | 404 | `app/api/papers.py:288-291` |
| DELETE：`SearchIndexError` / `ObjectStorageError` | 503（不标记删除，可重试） | `app/api/papers.py:316-320`, `:275-279` |
| `POST /api/metadata/import`：Content-Type 既非 multipart 也非 JSON | 415 | `app/api/metadata.py:87-89` |
| 同上：multipart 缺 `file` part / JSON 解析失败 / 未知 `source_type` / `importer` 抛 `ValueError` | 422 | `app/api/metadata.py:66-70`, `:74-78`, `:82-86`, `:112-116`, `:126-129` |
| `attach` / `apply`：来源或论文不存在 | 404（`import`）/ 计入 `skipped`（`apply`） | `app/api/metadata.py:54-57`, `:178-179`, `:241-249` |
| `POST /api/search`：`SearchError` / `ValueError` | 503 / 422 | `app/api/search.py:88-93`, `:94-97` |
| `GET /api/consistency` | **不抛**：store 不可达写进 `errors` 字段、HTTP 仍 200；只读，不写三端 | `app/api/consistency.py:38-56` |

### 2.4 lifespan 启停序列（`app/main.py:41-56`）

| 时点 | 调用 | 行号 | 实际行为 |
|---|---|---|---|
| 启动 | `configure_logging(settings.log_level)` | `main.py:43` | 根 logger 只配置一次（`app/core/logging.py:87`） |
| 启动 | `job_queue.start()` | `main.py:48` | 建 `INGEST_CONCURRENCY` 个 worker 协程（`app/workers/queue.py:119-137`） |
| 启动 | `job_queue.recover()` | `main.py:49` | `RECEIVED/QUEUED` 且未结束的作业重新入队；中间态作业标 `FAILED` + `error_code='INTERRUPTED'`（`queue.py:240-259` → `app/services/ingestion_service.py:426-461`） |
| 启动 | `housekeeping.start()` | `main.py:52` | 起周期任务，**首轮立即执行**（`housekeeping.py:323-334`：先 `run_gc` 再 `sleep(interval)`） |
| 关闭 | `await housekeeping.stop()` | `main.py:54` | cancel 周期任务 |
| 关闭 | `await job_queue.stop()` | `main.py:55` | cancel worker 协程；**不等待**，线程内已开始的流水线跑到结束（`queue.py:139-154`） |
| 关闭 | `logger.info("paperbox stopping")` | `main.py:56` | 无 DB/索引收尾：未调用 `job_queue.join()`（存在，`queue.py:156-160`），未调用 `dispose_engine()`（存在，`app/db/session.py:79`） |

启动**不**做 OpenSearch 索引存在性检查：`main.py:19-36` 的 import 列表不含 `app.search.opensearch`，lifespan 内亦无相关调用。

## 3. 数据结构

### 3.1 schema 组织（`app/schemas/`）

| 文件 | 模型 | 用途 |
|---|---|---|
| `ingestion.py` | `IngestRequest`(`:15`)、`IngestAccepted`(`:40`)、`IngestFileResult`(`:54`)、`IngestFilesAccepted`(`:75`)、`IngestDirRequest`(`:93`)、`IngestDirJob`(`:113`)、`IngestDirAccepted`(`:135`)、`IngestCompressedAccepted`(`:158`)、状态常量 `accepted/duplicate/rejected`(`:10-12`) | 五个摄取端点的请求/响应 |
| `job.py` | `JobOut`(`:10`)、`JobListOut`(`:29`)、`QueueOut`(`:38`) | 作业状态与队列快照 |
| `paper.py` | `PaperOut`(`:22`)、`PaperFileOut`(`:10`)、`PaperListOut`(`:52`)、`PaperChunkOut`(`:63`)、`PaperChunkList`(`:79`) | 论文读接口 |
| `metadata.py` | `PaperMetadataOut`(`:55`)、`SourceOut`(`:11`)、`IdentifierOut`(`:29`)、`ProvenanceEntry`(`:41`)、`MetadataPatch`(`:70`)、`MetadataPatchOut`(`:97`)、`MetadataRollbackIn/Out`(`:108`/`:115`)、`ImportReportOut`(`:127`)、`ConflictOut`(`:144`)、`ReviewOut`(`:157`)、`AttachIn/Out`(`:167`/`:173`)、`ApplyEntryIn/ApplyIn/ApplyOut`(`:186`/`:194`/`:202`) | 元数据读写与导入报告 |
| `app/schemas/search.py`（**写全路径：裸 `search.py` 会与 `app/api/search.py` 串表**） | `SearchFilters`(`:61`)、`MIN_TOP_K/MAX_TOP_K`(`:42-43`)、`SearchRequest`(`:130`，含 **`facets`** `:146`)、`SearchEvidence`(`:176`)、`SearchResult`(`:187`)、`SearchRerankInfo`(`:215`)、`SearchRewriteInfo`(`:228`)、**`FacetBucket`(`:243`)、`SearchFacets`(`:254`)**、`SearchResponse`(`:268`，含 `facets` `:288`) | 检索请求/响应（**2026-09-30 加 facets**） |
| `app/schemas/consistency.py`（**写全路径：裸 `consistency.py` 会串到 `app/api/consistency.py`**） | `PaperConsistencyOut`(`:8`，含 `parser_backend` `:26`)、`ConsistencyTotalsOut`(`:30`)、**`ParserBackendsOut`(`:47`)**（`:58` `papers`/`documents` 计数、`:62` `paper_ids` + `:65` `paper_ids_truncated`，后者只在 `?parser_papers=true` 时有内容）、`ConsistencyOut`(`:65`，含 `parser_backends` `:75`) |
| `search_log.py` | `SearchLogOut`(`:11`)、`SearchLogListOut`(`:32`) | 检索日志读接口 |
| `__init__.py` | — | 仍是占位（1 行 docstring），无重导出；导入一律走子模块 |

### 3.2 响应风格与 `docs/architecture/MVP-SPEC.md` §0 约定的对照

| 约定 | 代码是否遵守 | 证据 |
|---|---|---|
| `/api/*` 除 `/health` 外都要 Bearer | 遵守 | §2.2；`docs/architecture/MVP-SPEC.md:12` |
| 配置统一走 `app/core/config.py`，禁止硬编码 | 基本遵守；唯一硬编码是 429 的 `Retry-After: 2`（非配置键） | `app/services/upload_admission.py:36` |
| `paper_id` 为 UUID 字符串 | 遵守（schema 层是 `str`，未做 UUID 格式校验） | `app/schemas/paper.py:29` |
| 错误体统一 `{"detail": "…"}` | 手写 `HTTPException` 处遵守；框架请求体校验失败时 `detail` 是**列表** | `docs/architecture/MVP-SPEC.md:108`；代码未注册处理器（§2.3） |
| 列表响应带总数 | 遵守：`{total, items/papers/jobs/chunks/logs}` | `job.py:34`、`paper.py:55`、`paper.py:81`、`search_log.py:37`、`metadata.py:162` |

未使用统一的泛型分页模型：每个列表各自定义字段名（`papers` / `jobs` / `chunks` / `logs` / `items`）。

### 3.3 进程内内存结构（非表）

| 结构 | 字段 | 出处 |
|---|---|---|
| `IngestQueue` | `asyncio.PriorityQueue[tuple[int,int,_Item]]`（`(priority, seq, item)`）、`_pending: dict[job_id,(kind,priority)]`、`_running: dict[job_id,kind]`、`_seq` | `app/workers/queue.py:99-108`, `:201` |
| `UploadAdmission` | `_in_flight: int` + `threading.Lock`、`limit`、`high_watermark` | `app/services/upload_admission.py:79-84` |
| `Housekeeping` | `_task: asyncio.Task`、`interval`、`passes`、`last` | `app/workers/housekeeping.py:288-294` |
| 请求 id | `ContextVar("paperbox_request_id")` | `app/core/logging.py:24` |

## 4. 调用链（逐跳）

**启动**：uvicorn → `app.main:lifespan`(`main.py:42`) → `configure_logging` → `job_queue.start` → `job_queue.recover` → `housekeeping.start` → 请求可服务。

**`POST /api/papers/ingest/files`**（`ingestion.py:262`）：
`request_id_middleware`(`main.py:70`) → router 级 `require_api_key`(`security.py:64`) → `Depends(get_db)` 开请求 session(`session.py:70`) → 文件数/总字节检查(`ingestion.py:286-309`) → `upload_admission.get_admission()` + `should_throttle_batch`(`:315-317`) → `admission.slot()`(`:321`) → 逐文件 `stage_and_queue`(`:133`)：`ingest.is_pdf`/`ensure_size` → `run_in_threadpool(_stage_upload)`(`:95` → `object_storage.upload_stream_hashed`) → `ingest.find_existing_paper` → `ingest.create_job` + `session.commit`(`:193-201`) → `job_queue.submit`(`:207` → `ingest.mark_queued` → `enqueue` → `_hand_off`) → worker `_worker`(`queue.py:296`) → `asyncio.to_thread(tasks.run_ingestion_job)`(`queue.py:318`) → 返回 `summarize()` 的 202 响应(`:345`)。

**`POST /api/search`**（`app/api/search.py:50`）：中间件 → `require_api_key` → `SearchRequest` 校验(`schemas/search.py:130`) → `_maybe_rewrite`(`search.py:165`，线程化 `:65`) → `asyncio.to_thread(search_service.search_papers)`(`:149`) → 逐结果构造 `SearchResult`(`:179`) → `_log_search` 另开 `SessionLocal()` 写日志(`:206`, `:208`) → `SearchResponse`(`:221`)。

**`GET /api/papers/{id}/file`**（`papers.py:103`）：`_load_paper`(`:49`) → `papers.original_file`（主版本）→ `object_storage.open_stream`(`:121`) → `StreamingResponse` 64 KiB 分块 + `Content-Disposition: attachment`(`:134-146`)。

## 5. 不变量与踩过的坑

1. **路由注册顺序有两处硬约束**：`GET /api/jobs/queue` 必须声明在 `GET /api/jobs/{job_id}` 之前，否则被路径参数吞掉（`app/api/jobs.py:26-28` 注释）；`/api/papers` 前缀被 ingestion 与 papers 两个 router 共用（`main.py:84` vs `:86`），靠方法与字面量路径区分，新增 `/{something}` 形式的 GET/POST 前必须确认不遮挡 `/ingest*`。
2. **新路由必须手工 `include_router`**（`main.py:82-89`），没有自动发现；漏加 = 404 且 `/openapi.json` 里也看不到。
3. **单文件与多文件的错误契约相反**：`/ingest/file` 单文件失败 → 请求级 422（`ingestion.py:544-548`）；`/ingest/files` 同一种失败 → 逐文件 `rejected` 行 + 整体 202（`ingestion.py:75-92`, `:270-277`）。
4. **413 只在 multipart 声明了 size 时触发**：`declared_total` 只累加 `upload.size` 为正整数的部分（`ingestion.py:297-301`）；未声明长度时该门形同不存在，超限由 per-file 的 `ingest.ensure_size` 兜底（归因 `OVERSIZED`，`errors.py:149-150`）。
5. **429 的判定在服务端**（客户端无并发参数）：`INGEST_UPLOAD_CONCURRENCY` 管在途请求，`INGEST_QUEUE_HIGH_WATERMARK` 只管多文件请求（`upload_admission.py:128-136`），单文件永远放行。`Retry-After` 值硬编码 2 秒。
6. **`X-Request-ID` 总是回显**：带了沿用，没带生成 `uuid4().hex`（`main.py:75`），响应头无条件写入（`main.py:78`）；日志侧靠 ContextVar（`logging.py:34`）。
7. **关闭不排空**：`job_queue.stop()` 只 cancel 协程，线程内流水线继续跑；作业行停在中间态，靠下次启动的 `recover()` 标 `INTERRUPTED`（`queue.py:139-154`, `:240-259`）。想让在途作业跑完必须显式 `job_queue.join()`，lifespan 目前不调用。
8. **`GET /health` 恒 200**（`health.py:88-97`）：任一依赖挂掉只是该字段变 `"error"`，`status` 永远是 `"ok"`；用 200 判依赖健康会误判，必须看 `services.*`。
9. **分页参数不一致**：`chunks` 夹取 1..200（`papers.py:162`）、`review` 有 `ge=1, le=200`（`metadata.py:150`）、`search-logs` 依赖服务常量（`search_logs.py:29`），但 `GET /api/jobs`、`GET /api/papers` 的 `limit/offset` 无任何上下界（`jobs.py:77`, `papers.py:50-51`）。
10. **DELETE 的补偿语义**：先清 OpenSearch、再清 MinIO、最后才标记软删（`papers.py:242-242`），任一步 503 时论文仍可见且可原样重试——不要把顺序调换。
11. **`POST /api/metadata/import` 的 multipart 只读 `file` 一个 part**（`metadata.py:65`）；`source_type` 只来自 query（`metadata.py:98-101`）。README §3.4 的 `-F source_type=import_file` 示例不生效（默认值恰好相同，故不易察觉）。
12. **无 CORS 中间件**：全仓 grep `CORS` 0 命中，浏览器跨源调用会失败；Hermes/脚本这类非浏览器客户端不受影响。

## 6. 配置项（键 → 默认值 → 作用 → 出处）

| 键 | 默认 | 作用 | 出处 |
|---|---|---|---|
| `PAPER_API_KEY` | `change-me` | Bearer 密钥（比对对象）；空值 → 全部 403 | `app/core/config.py:77` |
| `PAPER_API_HOST` / `PAPER_API_PORT` | `0.0.0.0` / `8077` | 监听地址/端口（仅 uvicorn 启动参数使用） | `config.py:77-77` |
| `APP_ENV` / `LOG_LEVEL` | `local` / `INFO` | 启动日志内容与级别（lifespan 首行） | `config.py:40-41` |
| `INGEST_MAX_FILE_MB` | `100` | 单文件上限（超限 → `rejected`/422） | `config.py:121` |
| `INGEST_MAX_FILES_PER_REQUEST` | `20` | `/files` 文件数上限（超 → 422） | `config.py:141` |
| `INGEST_MAX_REQUEST_MB` | `200` | `/files` 单请求总字节上限（超 → 413） | `config.py:145` |
| `INGEST_UPLOAD_CONCURRENCY` | `2` | 在途上传请求上限（超 → 429） | `config.py:133` |
| `INGEST_QUEUE_HIGH_WATERMARK` | `50` | 积压水位，仅拒多文件（0 = 关闭） | `config.py:137` |
| `INGEST_LOCAL_ROOTS` | `""` | `/ingest/dir` 白名单（空 = 端点 404） | `config.py:151`, `:232-235` |
| `INGEST_ARCHIVE_MAX_MB` | `500` | 上传 zip 体积上限 | `config.py:155` |
| `INGEST_ARCHIVE_MAX_FILES` / `_MAX_UNCOMPRESSED_MB` / `_MAX_RATIO` | `2000` / `5000` / `100` | zip-bomb 三重上限 | `config.py:157-163` |
| `INGEST_ARCHIVE_TMP_DIR` / `INGEST_ARCHIVE_TTL_HOURS` | `""`（系统 temp）/ `24` | 解包位置与保留时长（GC 用） | `config.py:165-167` |
| `INGEST_GC_INTERVAL_S` | `300` | housekeeping 间隔（首轮启动即跑） | `config.py:171` |
| `INGEST_CONCURRENCY` | `2` | 并行流水线数（队列 worker 数，`/api/jobs/queue` 的 `concurrency`） | `config.py:127` |
| `OPENSEARCH_URL` / `MINIO_BUCKET` / `EMBEDDING_URL` | `http://localhost:9200` / `paperbox` / `http://localhost:8090` | `/health` 探针目标（embedding 探 `GET /health`） | `config.py:55`, `:59`, `:64`；`health.py:60-76` |
| `SEARCH_LOG_ENABLED` / `SEARCH_LOG_RESULTS_LIMIT` | `True` / `20` | `POST /api/search` 写日志开关与结果条数上限 | `config.py:116-117` |
| `RERANK_ENABLED` / `RERANK_TIMEOUT` | `True` / `10.0` | 响应 `rerank` 块与两阶段检索 | `config.py:77`, `:83`；`search.py:106-110` |
| `QUERY_REWRITE_ENABLED` + `_URL`/`_MODEL`/`_API_KEY` | `False` / `""` | 改写开关；开启时三者必填否则启动即报错 | `config.py:98-100`, `:207-225` |

非配置常量：`RETRY_AFTER_SECONDS = 2`（`app/services/upload_admission.py:36`）、`PROBE_TIMEOUT = 3.0`（`app/api/health.py:27`）（原先还有 `CANDIDATE_FACTOR = 5` = 「请求时估算的候选池」，2026-09-30 随 T-A2 删除：候选数改由检索实际结果给出）、chunk 分页上限 200（`app/api/papers.py:178`）。

## 7. 测试位置与覆盖（tests/xxx.py → 覆盖什么）

| 文件 | HTTP 层覆盖 | 证据 |
|---|---|---|
| `tests/test_ingest_files.py` | `/ingest/files`：逐 part 失败隔离、422、413、429+`Retry-After`、水位放行单文件 | `TestClient` `:126`；依赖覆盖 `:124`；413 `:325-333`；429 `:359-391` |
| `tests/test_ingest_file.py` | `/ingest/file` 旧契约回归、429 | `:136-142` |
| `tests/test_ingest_dir.py` | `/ingest/dir`：白名单 403、`..`/链接逃逸、dry_run | `:298-326` |
| `tests/test_ingest_compressed.py` | `/ingest/compressed`：非 zip 415、429、临时目录清理 | `:314-333`, `:346` |
| `tests/test_metadata_api.py` | `/api/metadata/import`（dry_run/apply/覆写）、`review`、`attach`、`apply`、415 | `:110-215`, `:189`, `:251-277`, `:305-475` |
| `tests/test_manual_metadata.py` | `GET/PATCH /api/papers/{id}/metadata`（含 404）、rollback | `:326-400` |
| `tests/test_deletion.py` | `papers_api.delete_paper` 直调：204、OpenSearch/MinIO 失败 503、顺序与幂等 | `:71-125`（非 TestClient） |
| `tests/test_upload_gc.py` | housekeeping 生命周期（周期任务启停）、幂等、不改作业行 | `README.md:445` |
| `tests/test_ingest_queue.py` / `test_queue_priority.py` | `recover_jobs`/`mark_queued`、优先级与 `queued_high/low` | `README.md:435-436` |
| `tests/test_job_retry.py` | 重试的原子认领与路由（服务层） | `README.md:432` |
| `tests/test_search_log.py` | 检索日志行序列化与写入降级（服务层） | `README.md:433` |

未覆盖（本模块视角）：`security.py` 的 401/403 分支（tests/ 内 grep `api_key_matches|extract_api_key|verify_api_key` 0 命中，所有 API 测试用 `dependency_overrides` 绕过）、`GET /health`、`GET /api/search-logs` 路由、`GET /api/jobs/queue` 路由、`GET /api/jobs` 路由、`main.py` 的 lifespan 与 `request_id_middleware`（`TestClient` 只出现在 5 个文件：`test_ingest_files/file`、`test_ingest_dir`、`test_ingest_compressed`、`test_metadata_api`、`test_manual_metadata`）。

## 8. 未做 / 已知缺口

1. **无统一异常处理器**：Pydantic 校验失败的 422 响应体 `detail` 是列表，与 `docs/architecture/MVP-SPEC.md:108` 的「统一 `{"detail": "…"}`」不一致；代码里没有任何 handler 可以覆盖它。
2. **`/docs`、`/redoc`、`/openapi.json` 无鉴权**，匿名可获取完整接口清单。
3. **无 CORS、无通用限流**：跨源浏览器调用不可用；限流仅存在于上传准入（`upload_admission.py`）。是否存在反向代理层的限流未在本次阅读范围内，**未确认**。
4. **关闭不等待在途作业**：`job_queue.join()` 存在但 lifespan 未调用（`main.py:53-56`），重启会把中间态作业标 `INTERRUPTED`；UI 侧需提示用户重试。
5. **启动不校验 OpenSearch 索引/别名**，也不跑 DB 迁移（`main.py:41-56` 无相关调用）；索引由脚本与 worker 自行处理（`ensure_index` 的调用点未在本模块范围内确认）。
6. **`GET /api/jobs`、`GET /api/papers` 的 `limit/offset` 无上下界**，可通过极大值放大查询。
7. **`GET /health` 恒 200**，无法作为就绪探针区分「进程活」与「依赖可用」。
8. **`Retry-After` 固定 2 秒**，不是配置项，积压很深时可能过于乐观。
9. **文档漂移（以代码为准）**：`docs/architecture/MVP-SPEC.md` §2（`:29-53`）未列 `POST /api/jobs/{job_id}/retry` 与 `GET /api/search-logs`；`README.md` §3.4 的导入示例含无效的 `-F source_type=…`（`:238-239`）且响应示例字段名写作 `total_records`（`:240`），代码实际返回 `total`（`app/schemas/metadata.py:132`、`app/services/metadata_import.py:600-612`）；`docs/architecture/MVP-SPEC.md:9` 提示该文件部分表述已过期。
10. **鉴权零单测**：401/403 与 `X-API-Key` 回退路径均无测试；测试统一绕过依赖，因此「precondition 挂了但鉴权漏配」这类回归不会被发现。
