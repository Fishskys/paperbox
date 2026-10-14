# HTTP API 层

| 项 | 内容 |
|---|---|
| 状态 | 依据 commit 54048a3 的工作树实测（2026-09-22） |
| 关键文件 | `app/main.py`、`app/api/{health,ingestion,jobs,metadata,papers,search,search_logs}.py`、`app/core/{security,errors,config,logging}.py`、`app/schemas/*.py`、`app/workers/{queue,housekeeping}.py`、`app/db/session.py` |
| 相关文档 | `docs/architecture/MVP-SPEC.md` [#0](MVP-SPEC.md#0-全局约定)/[#2](MVP-SPEC.md#2-api-一览实测-openapijson)/[#3](MVP-SPEC.md#3-响应错误约定)、`README.md` 第 3.4 节/[#4](../../README.md#4-目录结构)/第 7 节（各篇冲突处以代码为准，见 [#5](#5-不变量与踩过的坑)、[#8](#8-未做--已知缺口)） |

## 1. 职责边界（做什么 / 不做什么）

做什么：

- 把 HTTP 请求翻译成服务层调用：路由声明、请求体解析、Bearer 鉴权、错误码映射、响应模型序列化。
- 拥有进程级生命周期：`lifespan` 启动/停止摄取队列（`app/workers/queue.py`）与 housekeeping GC（`app/workers/housekeeping.py`），见 [#2.4](#24-lifespan-启停序列appmainpy)。
- 拥有请求级横切：`X-Request-ID` 中间件（`app/main.py` 的 `lifespan()`）、请求级 DB session（`app/db/session.py` 的 `get_db()`）。

不做什么：

- 不实现业务逻辑：摄取流水线在 `app/workers/tasks.py`，检索在 `app/search/hybrid.py` + `app/services/search_service.py`，元数据合并在 `app/services/metadata_*.py`。本层只调用。
- 不做鉴权以外的安全控制：无 CORS（未发现任何 `CORSMiddleware`/`add_middleware` 注册）、无全局限流（仅上传准入，见 `app/services/upload_admission.py`）。
- 不注册任何异常处理器（全仓 grep `exception_handler` 0 命中），错误体形状由 FastAPI 默认处理器决定。
- `app/core/errors.py` 不负责 HTTP 状态映射：它是**作业失败归因**（写入 `ingestion_jobs`），详见 [#2.3](#23-错误处理)。

## 2. 关键文件与函数（文件 → 函数/类 → 作用）

### 2.1 路由总表（29 条，包含不在 schema 的 `GET /`；另有 `/mcp` 挂载，见 第 2.5 节）

`Bearer` 列写的是该端点的**最低档位**（四档模型：`public` < `read` < `write` < `admin`，见 [#2.2](#22-鉴权实现)）。`public` = 不挂任何依赖、凭证不参与判定，缺凭证与无效凭证都答 200；`read` / `write` / `admin` = 挂了对应档位依赖（读端点由 router 级 `require_read` 统一挂）。有门时：缺凭证 401 + `WWW-Authenticate`、凭证不匹配 403、档位不足 403（`detail` 里说明差哪一档）。唯一的例外在读门：`AUTH_READ_ACCESS=public`（默认）时 **`read` 行不校验凭证**（无凭证调用者记为匿名 read 身份），`write` 与 `admin` 行的表现与读门取值无关。

| 方法 | 路径 | 作用 | Bearer | 关键查询参数 | 出处 |
|---|---|---|---|---|---|
| GET | `/health` | 每个依赖一条**声明式探针**（PG / OpenSearch / MinIO / embedding / rerank / docling；未配置的报 `disabled`）。顶层 `status` 只表示 API 进程存活，`degraded` 汇总**必需**依赖是否就绪，`dependencies` 带每项状态/延迟/检查时刻/原因。**探测只按需**（启动一次 + 每次调用），没有定时器；每项结论按 `HEALTH_PROBE_CACHE_TTL`（默认 300s，`HEALTH_CACHE_TTL_OVERRIDES` 可按依赖覆盖）复用，命中时带 `cached: true` 与原始 `checked_at` | public | — | `app/api/health.py`、`app/services/health_service.py` |
| GET | `/api/consistency` | 三端只读对账（PG / MinIO / OpenSearch），另含**解析后端与 embedding 模型两份普查** | read | `limit`（1..1000，默认 200）、`parser_papers`（bool，默认 false：连每个解析戳下的存活论文 id 一起返回，即 `parser_backends.paper_ids`） | `app/api/consistency.py` |
| GET | `/` | landing（`include_in_schema=False`） | public | — | `app/main.py` 的 `lifespan()` |
| POST | `/api/papers/ingest` | URL 摄取 | write | — | `app/api/ingestion.py` |
| POST | `/api/papers/ingest/files` | multipart 多文件（`files` 可重复） | write | — | `app/api/ingestion.py` |
| POST | `/api/papers/ingest/dir` | 服务端目录导入（零传输） | write | — | `app/api/ingestion.py` 的 `ingest_files()` |
| POST | `/api/papers/ingest/file` | 单文件（`/files` 的薄封装） | write | — | `app/api/ingestion.py` 的 `ingest_dir()` |
| POST | `/api/papers/ingest/compressed` | 上传 zip 解包导入 | write | — | `app/api/ingestion.py` 的 `ingest_file()` |
| GET | `/api/jobs/queue` | 队列深度快照 | read | — | `app/api/jobs.py` |
| GET | `/api/jobs/{job_id}` | 单作业阶段/进度 | read | — | `app/api/jobs.py` |
| POST | `/api/jobs/{job_id}/retry` | 重驱动 FAILED 作业（202） | write | — | `app/api/jobs.py` |
| GET | `/api/jobs` | 最近作业列表 | read | `limit`（默认 20，无上下界） | `app/api/jobs.py` |
| GET | `/api/papers` | 论文列表 | read | `limit`、`offset`、`status`、`q`、`venue`、`year_from`、`year_to`、`paper_type`、`tag` | `app/api/papers.py` |
| GET | `/api/papers/{paper_id}` | 单篇元数据 | read | — | `app/api/papers.py` 的 `list_papers()` |
| GET | `/api/papers/{paper_id}/file` | 原文流式下载（attachment） | read | — | `app/api/papers.py` 的 `get_paper()` |
| GET | `/api/papers/{paper_id}/chunks` | chunk 分页列表 | read | `limit`、`offset`（夹取 1..200） | `app/api/papers.py`, `papers.py` |
| GET | `/api/papers/{paper_id}/degradations` | 降级账本：该论文各阶段「能用但更薄」的原因 | read | `include_resolved`（默认 false，只列仍未消解的） | `app/api/papers.py` |
| GET | `/api/papers/{paper_id}/metadata` | 当前值 + 每字段溯源 | read | — | `app/api/papers.py` 的 `dismiss_paper_conflict()` |
| PATCH | `/api/papers/{paper_id}/metadata` | 手动改元数据 | write | — | `app/api/papers.py` 的 `rollback_paper_metadata()` |
| POST | `/api/papers/{paper_id}/metadata/rollback` | 单字段回滚到历史主张（裁决「采纳被拒值」） | write | — | `app/api/papers.py` 的 `delete_paper()` |
| POST | `/api/papers/{paper_id}/metadata/conflicts/dismiss` | 裁决「保留现值」：值不动，只把该分歧标为已裁决 | write | — | `app/api/papers.py` 的 `delete_paper()` |
| DELETE | `/api/papers/{paper_id}` | 删除（204） | write | — | `app/api/papers.py` 的 `dismiss_paper_conflict()` |
| POST | `/api/papers/reindex` | **批量/整库重建索引**（先检测"为什么要重建"，默认 `dry_run=true` 只报告） | admin | — | `app/api/papers.py` 的 `rollback_paper_metadata()` |
| POST | `/api/papers/{paper_id}/reindex` | 重建单篇索引（202） | write | — | `app/api/papers.py` 的 `rollback_paper_metadata()` |
| POST | `/api/metadata/import` | 外部元数据导入 | write | `dry_run`（默认 true）、`apply`、`limit`、`source_type` | `app/api/metadata.py` |
| GET | `/api/metadata/review` | 复核清单 + 已登记冲突 | read | `status`（可重复）、`limit`（1..200，默认 50） | `app/api/metadata.py` |
| POST | `/api/metadata/sources/{source_id}/attach` | 人工归属来源 | write | — | `app/api/metadata.py` |
| POST | `/api/metadata/apply` | 按报告批量应用人工决定 | write | — | `app/api/metadata.py` |
| POST | `/api/search` | 论文级混合检索 | read | — | `app/api/search.py` 的 `search()` |
| GET | `/api/search-logs` | 检索日志（只读） | read | `limit`、`since`、`mode` | `app/api/search_logs.py` |

`/docs`、`/redoc`、`/openapi.json` 由 FastAPI 默认挂载（`app/main.py` 的 `lifespan()` 未加保护），匿名可读。`POST /api/search` 不注入 DB session（`app/api/search.py` 的 `search()` 只收 body），日志另开 session：`app/services/search_pipeline.py` 的 `log_search()`。

### 2.2 鉴权实现

**四档模型（2026-10-12 读门策略化）**：权限一共 4 级 —— `public`（无凭据）与三级密钥
`read` < `write` < `admin`。

| 级别 | 覆盖端点 | 何时生效 |
|---|---|---|
| `public` | `GET /health`、`GET /`、`/docs`、`/redoc`、`/openapi.json`；**读门为 `public` 时的 13 个业务读** | 框架/运维端点恒为 public；读端点由 `AUTH_READ_ACCESS` 决定 |
| `read` | 13 个业务读端点（[#2.1](#21-端点总表) 里 `Bearer=read` 的行） | 仅当 `AUTH_READ_ACCESS=key` |
| `write` | 全部增删改：5 个 `ingest*`、`jobs/{job_id}/retry`、单篇 `papers/{paper_id}/reindex`、`PATCH papers/{paper_id}/metadata`、`metadata/rollback`、`metadata/conflicts/dismiss`、`metadata/import`、`metadata/sources/{source_id}/attach`、`metadata/apply`、**`DELETE papers/{paper_id}`** | `AUTH_ENABLED=true` |
| `admin` | **只有** `POST /api/papers/reindex`（批量/整库重建：小时级、打满 embedding） | `AUTH_ENABLED=true` |

**两个开关，互不越界**：

| `AUTH_ENABLED` | `AUTH_READ_ACCESS` | 读端点 | 写端点（含删除） | 批量重建（`admin`） |
|---|---|---|---|---|
| `false` | 任意 | 开放（匿名 admin） | 开放（匿名 admin） | 开放（匿名 admin） |
| `true` | `public`（默认） | 开放（匿名 read 身份） | 401 / 403 | 401 / 403 |
| `true` | `key` | 401 / 403 | 401 / 403 | 401 / 403 |

`AUTH_ENABLED` 管整个凭据面（关 = 读写全放行），`AUTH_READ_ACCESS` **只切读门**，且仅在总开关打开时
有意义（关时该键没有作用对象，属于合法组合，启动不告警）。**不变式**：`AUTH_ENABLED=true` 时，
写端点与 `admin` 端点的表现与 `AUTH_READ_ACCESS` 取值无关 —— 门在比档位之前先判 `is_anonymous`，
所以匿名调用者碰写门恒为 401，不会因为读门开着而变成 403。

**拒绝语义**：

| 调用者 | 读门 `public` | 读门 `key` | 写门（含删除） | 批量重建（`admin`） |
|---|---|---|---|---|
| 无凭证 | 200（匿名 read） | 401 + `WWW-Authenticate` | 401 + `WWW-Authenticate` | 401 |
| 无效凭证 | 200（读门不读凭证） | 403 `Invalid API key` | 403 `Invalid API key` | 403 |
| `read` 钥匙 | 200 | 200 | 403 `insufficient role` | 403 |
| `write` 钥匙 | 200 | 200 | 200 | 403 |
| `admin` 钥匙 | 200 | 200 | 200 | 200 |

| 环节 | 实现 | 出处 |
|---|---|---|
| 取凭证 | `Authorization` 头优先；`partition(" ")` 后 scheme 必须为 `bearer`（忽略大小写）且凭证非空 | `app/core/security.py::extract_api_key` |
| 无 `Authorization` 时回退 | 读 `X-API-Key` | 同上 |
| 有 `Authorization` 但 scheme 非 Bearer | 直接返回 `None`，**不回退** `X-API-Key` | 同上 |
| 解析（每请求一次） | `AuthContextMiddleware`（main.py）解析 Bearer → `api_key_service.authenticate(session, token)`，把身份放 `scope["state"]` 并把前缀绑进日志上下文（**纯 ASGI**；不在依赖里做，因为线程池依赖里绑 contextvar 传播不回日志上下文）。读门为 `public` 且没带凭证时，这里直接给出**匿名 read 身份**（`anonymous_reader()`，前缀 `anonymous`），所以日志不再出现空归因 | `app/main.py`、`app/services/api_key_service.py` |
| 凭证存储 | `api_keys` 表只存 `sha256(完整密钥)`；**`prefix` 是钥匙唯一的标识**（日志归因 + 审计标签，`[a-z0-9_-]{2,32}`，唯一）与 `key_hash` 各唯一；三档 `read < write < admin`；**吊销即删除该行**（硬删、无 tombstone，重发 = 同 prefix 再 `create`）；env 引导行（`PAPER_API_KEY`/`PAPER_API_KEYS`）启动时 upsert 成 admin、优先于库行（恢复路径） | `app/db/models.py::ApiKey`、`app/services/api_key_service.py` |
| 匿名身份 | 复用 `AuthIdentity`，加 `is_anonymous` 标志与 `anonymous(role)` / `anonymous_reader()` 工厂；唯一用途是让门区分「没带凭证」（401）与「档位不足」（403） | 同上 |
| 档位强制 | `require_role(min)` 工厂；读端点用 `require_read`（=read）、写端点用 `require_write`（=write，含删除）、批量重建用 `require_admin`（=admin）。缺凭证 401 + `WWW-Authenticate`、不匹配 403 `Invalid API key`、档位不足 403 `insufficient role` | `app/core/security.py` |
| 依赖挂载 | consistency / jobs / metadata / papers / search / search_logs 六个 router 在**构造时**挂 `require_read`（一处声明覆盖全部读端点）；写与 admin 端点各自在端点级挂更高档位（同一路由两条依赖时以最高档为准） | 各 `app/api/*.py` |
| 读门为 `public` 时的读端点 | 不校验凭证：缺凭证 200（匿名 read 身份），**无效凭证也 200**（读门不读凭证，2026-10-12 B2 裁定） | `app/core/security.py` |

测试用 `app.dependency_overrides[require_write]` 绕过写档（如 `tests/test_ingest_files.py` 的 `client()`）；路由表契约由 `tests/test_public_reads.py`（30 个路由逐条钉住各自档位，并断言「只有批量重建是 admin」）覆盖，401/403 语义与两态矩阵由 `tests/test_auth_rest.py`（probe 路由 × 5 种凭证 × 两种读门）与 `tests/test_auth_read_access.py`（配置枚举、匿名身份、「总开关关 + `key` 不告警」）覆盖。
端点→档位全表见 `UserManual.md` [#2.1.1](../../UserManual.md#211-端点--最低密钥档位auth_enabledtrue-时)；启动护栏（拒 `change-me`、拒零密钥、auth-off + 非回环 WARNING）见 `app/main.py::lifespan` 与 `app/core/config.py` 的 `_check_auth`。

### 2.3 错误处理

两个互不相干的机制：

1. **HTTP 错误** = 各 endpoint 内手写 `raise HTTPException(...)`。没有统一异常处理器，也没有业务异常基类到状态的集中映射；FastAPI 默认处理器输出字符串 `detail`，与 [MVP-SPEC.md](MVP-SPEC.md)「统一 `{"detail": "…"}`」一致。
2. **作业失败归因** = `app/core/errors.py` classify_failure()` 把流水线异常映射成 14 个稳定 code（`errors.py`）：`NO_TEXT_LAYER`、`ENCRYPTED_PDF`、`CORRUPT_PDF`、`DOWNLOAD_FAILED`、`OVERSIZED`、`UNSUPPORTED_TYPE`、`DUPLICATE_FINGERPRINT`、`PARSE_BACKEND_UNAVAILABLE`、`PARSE_FAILED`、`EMBEDDING_FAILED`、`INDEX_FAILED`、`STORAGE_FAILED`、`INTERRUPTED`、`INTERNAL`（后两个解析码只在「明确要求 docling 且不许降级」时出现 —— 正常流水线降级到 pypdf 并记 `degraded_reason`/`paper_degradations`，见 `03-parsing-chunking.md`）。它写进作业行，经 `GET /api/jobs/{job_id}` 的 `error_code` 暴露（`app/schemas/job.py`）。调用点：`app/api/ingestion.py`、`app/workers/tasks.py`。

业务异常 → HTTP 状态映射（全部为端点内显式 raise）：

| 触发 | 状态 | 出处 |
|---|---|---|
| URL 非法（`ingest.UnsupportedSource`） | 422 | `app/api/ingestion.py` |
| `local_scan.ScanUnavailable`（白名单为空 = 端点关闭） | 404 | `app/api/ingestion.py` |
| `local_scan.RootNotAllowed`（越界/`..`/链接逃逸） | 403 | `app/api/ingestion.py` |
| `local_scan.RootMissing` | 404 | `app/api/ingestion.py` |
| `/files`：0 个文件或超 `INGEST_MAX_FILES_PER_REQUEST` | 422 | `app/api/ingestion.py` |
| `/files`：声明总字节超 `INGEST_MAX_REQUEST_MB` | 413 | `app/api/ingestion.py` |
| `/files`：批请求 + 队列积压 ≥ 水位 | 429 + `Retry-After` | `app/api/ingestion.py` 的 `ingest_files()`|
| `upload_admission.AdmissionRejected`（在途上传超限） | 429 + `Retry-After` | `app/api/ingestion.py`；； |
| `/compressed`：非 zip 魔数 | 415 | `app/api/ingestion.py` 的 `ingest_compressed()` |
| `/compressed`：`ArchiveError`（gzip-bomb/zip-slip 等上限） | 422 | `app/api/ingestion.py` |
| `/compressed`：解包其它异常 | 500 | `app/api/ingestion.py` 的 `ingest_compressed()` |
| `/file`：单文件被判 rejected | 422 | `app/api/ingestion.py` 的 `ingest_file()` |
| `/file`：staging 后作业行消失 | 503 | `app/api/ingestion.py` 的 `ingest_file()` |
| `GET /api/jobs/{id}` / `POST .../retry`：作业不存在 | 404 | `app/api/jobs.py` 的 `get_job()`, `retry_job()` |
| `POST /api/jobs/{id}/retry`：非 FAILED | 409 | `app/api/jobs.py` 的 `retry_job()` |
| `papers` 路由：论文不存在（含软删） | 404 | `app/api/papers.py` |
| 原文对象缺失 / `ObjectNotFound` | 404 | `app/api/papers.py`|
| `ObjectStorageError`（读文件） | 503 | `app/api/papers.py` |
| rollback 的 `provenance_id` 不属该论文/字段 | 404 | `app/api/papers.py` |
| DELETE：`SearchIndexError` / `ObjectStorageError` | 503（不标记删除，可重试） | `app/api/papers.py`|
| `POST /api/metadata/import`：Content-Type 既非 multipart 也非 JSON | 415 | `app/api/metadata.py` |
| 同上：multipart 缺 `file` part / JSON 解析失败 / 未知 `source_type` / `importer` 抛 `ValueError` | 422 | `app/api/metadata.py`, , , |
| `attach` / `apply`：来源或论文不存在 | 404（`import`）/ 计入 `skipped`（`apply`） | `app/api/metadata.py`, |
| `POST /api/search`：`SearchError` / `ValueError` | 503 / 422 | `app/api/search.py`|
| `GET /api/consistency` | **不抛**：store 不可达写进 `errors` 字段、HTTP 仍 200；只读，不写三端 | `app/api/consistency.py` |

### 2.4 lifespan 启停序列（`app/main.py`）

| 时点 | 调用 | 实际行为 |
|---|---|---|
| 启动 | `configure_logging(settings.log_level)` | 根 logger 只配置一次（`app/core/logging.py`） |
| 启动 | `await health_service.log_startup_report()`（**仅当 `HEALTH_PROBE_ON_STARTUP=true`**） | 每个依赖探一次并把结论写进日志（失败只 `warning`，不拦启动；结论同时落缓存）。**探测只按需**：这一次 + 之后每次调 `/health`，没有定时器 —— 周期探第三方 API 是逐次计费的 |
| 启动 | `job_queue.start()` | 建 `INGEST_CONCURRENCY` 个 worker 协程 |
| 启动 | `job_queue.recover()` | `RECEIVED/QUEUED` 且未结束的作业重新入队；中间态作业标 `FAILED` + `error_code='INTERRUPTED'`（→ `app/services/ingestion_service.py`） |
| 启动 | `housekeeping.start()` | 起周期任务，**首轮立即执行**（`housekeeping.py`：先 `run_gc` 再 `sleep(interval)`） |
| 关闭 | `await housekeeping.stop()` | cancel 周期任务 |
| 关闭 | `await job_queue.stop()` | cancel worker 协程；**不等待**，线程内已开始的流水线跑到结束（`queue.py` 的 `IngestQueue.stop()`） |
| 关闭 | `logger.info("paperbox stopping")` | 无 DB/索引收尾：未调用 `job_queue.join()`（存在），未调用 `dispose_engine()`（存在） |
| 启动 | `await mcp_server.session_manager.run()`（**仅当 `MCP_ENABLED=true`**） | 挂载的子应用**不会**执行自己的 lifespan，所以 MCP 会话管理器必须由宿主 lifespan 进入；漏掉 = `/mcp` 路由在、**首个请求 500**（有回归测试） |
| 启动 | `mcp.auth.warn_about_shared_keys()` | 多 agent 共用同一把 key 时启动告警（审计里无法区分 agent） |

启动**不**做 OpenSearch 索引存在性检查：`main.py` 的 import 列表不含 `app.search.opensearch`，lifespan 内亦无相关调用。

### 2.5 MCP 端点（挂载，不在 OpenAPI 里）

`/mcp` 是一个 **Mount**（`main.py` 附近 `app.mount("/mcp", build_streamable_http_app(...))`）
所以它**不出现在 `/openapi.json`** 与 `/docs` 里；契约与工具清单见
`docs/architecture/11-mcp-agent-interface.md`。要点：

| 项 | 实现 |
|---|---|
| 传输 | Streamable HTTP，`streamable_http_path="/"` + `stateless_http=True`（无会话状态，两个调用之间不依赖会话） |
| `Host` 白名单 | 显式 `TransportSecuritySettings(allowed_hosts=…)`（**不依赖 SDK 默认**，SDK 默认只认 `127.0.0.1`/`localhost`）；白名单外一律 **421**；`MCP_ALLOWED_HOSTS` 为空且 `MCP_ENABLED=true` → **启动即报错** |
| 路径规范化 | `McpMountPathMiddleware` 在服务端内部把 `/mcp` 改写成 `/mcp/`（Starlette 的 `Mount` 正则要求尾斜杠）；**不带尾斜杠也必须一次命中**，不能回 307 |
| 鉴权 | 自写静态 Bearer（**不用** SDK 的 `AuthSettings`——那是 OAuth 形态）：`McpAuthMiddleware` 置于**最外层**，只解析身份、**不拒绝任何请求**（传输层看不到工具名，401/403 没有位置可放），**不支持 `?key=`**；命中后把 agent 名放进 ContextVar 供审计。**四档口径**：`AUTH_ENABLED=false` → 匿名 admin；`AUTH_READ_ACCESS=public`（默认）→ 无凭证调用者拿匿名 **read** 身份，读工具照答；`=key` → 无凭证调用者没有身份，`tools_read.require_read` 以 `FORBIDDEN` 拒绝读工具（码集冻结，失败文本里的 `reads are gated` 指明是策略门）。写工具的 `require_role` 与读门无关；`paper_delete` 的档位是 `write`（2026-10-12 起，删除并入写档） |
`POST /api/papers/reindex` 的选择逻辑与 **`scripts/reindex.py` 共用**
（`app/services/reindex_service.py`：同一份 SQL、同一套检测），差别只在执行 —— 脚本在本进程同步跑
（可 Ctrl-C、可 `--dry-run`），端点按论文**入队**后立即返回（单篇 30–370 秒，整库是小时级操作，不能挂在一次 HTTP 请求上）。请求体：`dry_run`（默认 **true**）、`include_all`（全库）、`paper_ids`（指名）、`reasons`（只按某个检测理由）。响应里 `reasons` 说明为什么、`skipped_reasons` 说明"检测到但本次没采纳"，`skipped` 逐条列出跳过原因（例如没有原始文件）。
检测理由（`DETECTORS` 注册表，可扩展）：`embedding_model_changed` / `parser_backend_changed` /
`open_degradations` / `missing_chunks`。

| 工具 ↔ REST 的关系 | 工具直接调 **service 层**（`search_pipeline.run_search`、`chunk_service`、`paper_service.delete_preview/purge`、`ingestion_service.create_reindex_job`、`metadata_manual.current_value`），不绕 HTTP：同一查询在 REST 与 MCP 上的 `total` 与排序逐位相同（不变式 2） |
| 失败通道 | 工具抛 `ToolFailure`（继承 SDK `ToolError`）→ 客户端拿 `isError:true` + 契约错误 JSON 文本；抛其它异常 = 崩贴、客户端看不到细节（`app/mcp/errors.py`） |
| 写工具开关 | 关掉的写工具**不注册**：不出现在 `tools/list`，按名字也调不到 |
| 审计 | 每次调用一行结构化日志（`app.mcp.audit`）：工具、agent、参数摘要、结果、错误码、耗时、影响面；**不落库、不落新表** |

## 3. 数据结构

### 3.1 schema 组织（`app/schemas/`）

| 文件 | 模型 | 用途 |
|---|---|---|
| `ingestion.py` | `IngestRequest`()、`IngestAccepted`()、`IngestFileResult`()、`IngestFilesAccepted`()、`IngestDirRequest`()、`IngestDirJob`()、`IngestDirAccepted`()、`IngestCompressedAccepted`()、状态常量 `accepted/duplicate/rejected`() | 五个摄取端点的请求/响应 |
| `job.py` | `JobOut`()、`JobListOut`()、`QueueOut`() | 作业状态与队列快照 |
| `paper.py` | `PaperOut`()、`PaperFileOut`()、`PaperListOut`()、`PaperChunkOut`()、`PaperChunkList`() | 论文读接口 |
| `metadata.py` | `PaperMetadataOut`()、`SourceOut`()、`IdentifierOut`()、`ProvenanceEntry`()、`MetadataPatch`()、`MetadataPatchOut`()、`MetadataRollbackIn/Out`(/)、`ImportReportOut`()、`ConflictOut`()、`ReviewOut`()、`AttachIn/Out`(/)、`ApplyEntryIn/ApplyIn/ApplyOut`(//) | 元数据读写与导入报告 |
| `app/schemas/search.py`（**写全路径：裸 `search.py` 会与 `app/api/search.py` 串表**） | `SearchFilters`()、`MIN_TOP_K/MAX_TOP_K`()、`SearchRequest`(含 **`facets`**)、`SearchEvidence`()、`SearchResult`()、`SearchRerankInfo`()、`SearchRewriteInfo`()、**`FacetBucket`()、`SearchFacets`()**、`SearchResponse`(含 `facets`) | 检索请求/响应 |
| `app/schemas/consistency.py`（**写全路径：裸 `consistency.py` 会串到 `app/api/consistency.py`**） | `PaperConsistencyOut`(含 `parser_backend`、**`embedding_models` **)、`ConsistencyTotalsOut`()、**`ParserBackendsOut`()**（`papers`/ `documents` 计数、`paper_ids` + `paper_ids_truncated`，后者只在 `?parser_papers=true` 时有内容）、**`EmbeddingModelsOut`()**（`chunks`/ `documents` 计数）、`ConsistencyOut`(含 `parser_backends`、**`embedding_models` **) | 一致性对账出参（三端普查 + 解析后端/模型两份统计） |
| `search_log.py` | `SearchLogOut`()、`SearchLogListOut`() | 检索日志读接口 |
| `__init__.py` | — | 仍是占位（1 行 docstring），无重导出；导入一律走子模块 |

### 3.2 响应风格与 `docs/architecture/MVP-SPEC.md` 第 0 节 约定的对照

| 约定 | 代码是否遵守 | 证据 |
|---|---|---|
| `/api/*` 除 `/health` 外都要 Bearer | **已改**：四档模型 —— 读端点挂 `read`（读门 `public` 时不校验凭证）、增删改要 `write`、批量重建要 `admin`（2026-10-12） | [#2.2](#22-鉴权实现)；[MVP-SPEC.md](MVP-SPEC.md) |
| 配置统一走 `app/core/config.py`，禁止硬编码 | 基本遵守；唯一硬编码是 429 的 `Retry-After: 2`（非配置键） | `app/services/upload_admission.py` 的 `RETRY_AFTER_SECONDS` |
| `paper_id` 为 UUID 字符串 | 遵守（schema 层是 `str`，未做 UUID 格式校验） | `app/schemas/paper.py` |
| 错误体统一 `{"detail": "…"}` | 手写 `HTTPException` 处遵守；框架请求体校验失败时 `detail` 是**列表** | [MVP-SPEC.md](MVP-SPEC.md)；代码未注册处理器（[#2.3](#23-错误处理)） |
| 列表响应带总数 | 遵守：`{total, items/papers/jobs/chunks/logs}` | `job.py` 的 `JobListOut`、`paper.py` 的 `PaperListOut`、`paper.py` 的 `PaperChunkList`、`search_log.py`、`metadata.py` |
| 健康探针不把"有应答"当健康 | 遵守：成功是**白名单状态码 + body 语义**（401 / 404 / 429 / 3xx 一律 `error`），失败原因经脱敏后才回显 | `app/services/health_service.py`；`tests/test_health_service.py`、`tests/test_health.py` |

未使用统一的泛型分页模型：每个列表各自定义字段名（`papers` / `jobs` / `chunks` / `logs` / `items`）。

### 3.3 进程内内存结构（非表）

| 结构 | 字段 | 出处 |
|---|---|---|
| `IngestQueue` | `asyncio.PriorityQueue[tuple[int,int,_Item]]`（`(priority, seq, item)`）、`_pending: dict[job_id,(kind,priority)]`、`_running: dict[job_id,kind]`、`_seq` | |
| `UploadAdmission` | `_in_flight: int` + `threading.Lock`、`limit`、`high_watermark` | |
| `Housekeeping` | `_task: asyncio.Task`、`interval`、`passes`、`last` | |
| 请求 id | `ContextVar("paperbox_request_id")` | `app/core/logging.py` |

## 4. 调用链（逐跳）

**启动**：uvicorn → `app.main:lifespan`(`main.py`) → `configure_logging` → `job_queue.start` → `job_queue.recover` → `housekeeping.start` → 请求可服务。

**`POST /api/papers/ingest/files`**（`ingestion.py` 的 `ingest_url()`）：
`request_id_middleware`(`main.py`) → router 级 `require_api_key`(`security.py`) → `Depends(get_db)` 开请求 session(`session.py`) → 文件数/总字节检查(`ingestion.py`) → `upload_admission.get_admission()` + `should_throttle_batch`(`ingestion.py`) → `admission.slot()`(`ingestion.py`) → 逐文件 `stage_and_queue`()：`ingest.is_pdf`/`ensure_size` → `run_in_threadpool(_stage_upload)`(→ `object_storage.upload_stream_hashed`) → `ingest.find_existing_paper` → `ingest.create_job` + `session.commit`() → `job_queue.submit`(→ `ingest.mark_queued` → `enqueue` → `_hand_off`) → worker `_worker`(`queue.py`) → `asyncio.to_thread(tasks.run_ingestion_job)`(`queue.py`) → 返回 `summarize()` 的 202 响应(`ingestion.py`)。

**`POST /api/search`**（`app/api/search.py`）：中间件 → `require_api_key` → `SearchRequest` 校验() → `maybe_rewrite`(线程化 `app/services/search_pipeline.py`) → `asyncio.to_thread(search_service.search_papers)`(`app/services/search_pipeline.py`) → 逐结果构造 `SearchResult`(`app/services/search_pipeline.py`) → `_log_search` 另开 `SessionLocal()` 写日志(`app/services/search_pipeline.py`) → `SearchResponse`(`app/services/search_pipeline.py`)。

**`GET /api/papers/{id}/file`**（`papers.py`）：`_load_paper`() → `papers.original_file`（主版本）→ `object_storage.open_stream`() → `StreamingResponse` 64 KiB 分块 + `Content-Disposition: attachment`()。

## 5. 不变量与踩过的坑

1. **路由注册顺序有两处硬约束**：`GET /api/jobs/queue` 必须声明在 `GET /api/jobs/{job_id}` 之前，否则被路径参数吞掉（`app/api/jobs.py` 的 `get_queue()` 注释）；`/api/papers` 前缀被 ingestion 与 papers 两个 router 共用（`main.py` 的 `lifespan()` vs `list_jobs()`），靠方法与字面量路径区分，新增 `/{something}` 形式的 GET/POST 前必须确认不遮挡 `/ingest*`。
2. **新路由必须手工 `include_router`**（`main.py`），没有自动发现；漏加 = 404 且 `/openapi.json` 里也看不到。
3. **单文件与多文件的错误契约相反**：`/ingest/file` 单文件失败 → 请求级 422（`ingestion.py`）；`/ingest/files` 同一种失败 → 逐文件 `rejected` 行 + 整体 202（`ingestion.py`）。
4. **413 只在 multipart 声明了 size 时触发**：`declared_total` 只累加 `upload.size` 为正整数的部分（`ingestion.py`）；未声明长度时该门形同不存在，超限由 per-file 的 `ingest.ensure_size` 兜底（归因 `OVERSIZED`，`errors.py`）。
5. **429 的判定在服务端**（客户端无并发参数）：`INGEST_UPLOAD_CONCURRENCY` 管在途请求，`INGEST_QUEUE_HIGH_WATERMARK` 只管多文件请求（`upload_admission.py`），单文件永远放行。`Retry-After` 值硬编码 2 秒。
6. **`X-Request-ID` 总是回显**：带了沿用，没带生成 `uuid4().hex`（`main.py` 的 `lifespan()`），响应头无条件写入（`main.py` 的 `lifespan()`）；日志侧靠 ContextVar（`logging.py` 的 `get_request_id()`）。
7. **关闭不排空**：`job_queue.stop()` 只 cancel 协程，线程内流水线继续跑；作业行停在中间态，靠下次启动的 `recover()` 标 `INTERRUPTED`。想让在途作业跑完必须显式 `job_queue.join()`，lifespan 目前不调用。
8. **`GET /health` 恒 200**（`health.py`）：任一依赖挂掉只是该字段变 `"error"`，`status` 永远是 `"ok"`；用 200 判依赖健康会误判，必须看 `services.*`。`services` 是**六项**：`postgres` / `opensearch` / `minio` / `embedding` / `rerank` / `docling`；未配置的依赖（`DOCLING_URL` 为空、`RERANK_ENABLED=false`）报 `"disabled"`，不是故障。
9. **分页参数不一致**：`chunks` 夹取 1..200（`papers.py`）、`review` 有 `ge=1, le=200`（`metadata.py`）、`search-logs` 依赖服务常量（`search_logs.py`），但 `GET /api/jobs`、`GET /api/papers` 的 `limit/offset` 无任何上下界（`jobs.py`, `papers.py`）。
10. **DELETE 的补偿语义**：先清 OpenSearch、再清 MinIO、最后才标记软删（`papers.py` 的 `dismiss_paper_conflict()`），任一步 503 时论文仍可见且可原样重试——不要把顺序调换。
11. **`POST /api/metadata/import` 的 multipart 只读 `file` 一个 part**（`metadata.py`）；`source_type` 只来自 query（`metadata.py`）。README 第 3.4 节 的 `-F source_type=import_file` 示例不生效（默认值恰好相同，故不易察觉）。
12. **无 CORS 中间件**：全仓 grep `CORS` 0 命中，浏览器跨源调用会失败；Hermes/脚本这类非浏览器客户端不受影响。
13. **`/mcp` 不能有重定向**：客户端（如 Hermes）在 307 后**不会重放 `Authorization`**，所以 `/mcp` 必须在服务端内部补斜杠一次命中；断言这件事必须 `follow_redirects=False`，否则 TestClient 会自动跟随、测试等于没测。
14. **MCP 写工具"关 = 不注册"**：`MCP_WRITE_ENABLED` 与三个 `MCP_ALLOW_*` 决定工具是否注册，不是运行时才拒绝；想确认当下能力请看 `tools/list`，不要看文档。

## 6. 配置项（键 → 默认值 → 作用 → 出处）

| 键 | 默认 | 作用 | 出处 |
|---|---|---|---|
| `AUTH_ENABLED` | `false` | 鉴权总开关（同时管 `/api/*` 与 `/mcp`）：关 = 每个调用者都是匿名 admin（读写全放行）；开 = 档位强制生效（读门另由 `AUTH_READ_ACCESS` 决定），并拒绝 `change-me` 默认值与零密钥启动 | `app/core/config.py` |
| `AUTH_READ_ACCESS` | `public` | 读门策略（`public` \| `key`）：`public` = 读端点不校验凭证（缺凭证/无效凭证都 200，调用者记匿名 read 身份），`key` = 读端点与写端点同一套规矩。仅在 `AUTH_ENABLED=true` 时有作用对象；非法取值启动即报错，`AUTH_ENABLED=false` + `key` **不告警** | `app/core/config.py` |
| `PAPER_API_KEY` | `change-me` | Bearer 密钥（比对对象）；空值 → 全部 403 | `app/core/config.py` |
| `PAPER_API_HOST` / `PAPER_API_PORT` | `0.0.0.0` / `8077` | 监听地址/端口（仅 uvicorn 启动参数使用） | `config.py` |
| `APP_ENV` / `LOG_LEVEL` | `local` / `INFO` | 启动日志内容与级别（lifespan 首行） | `config.py` |
| `INGEST_MAX_FILE_MB` | `100` | 单文件上限（超限 → `rejected`/422） | `config.py` |
| `INGEST_MAX_FILES_PER_REQUEST` | `20` | `/files` 文件数上限（超 → 422） | `config.py` |
| `INGEST_MAX_REQUEST_MB` | `200` | `/files` 单请求总字节上限（超 → 413） | `config.py` |
| `INGEST_UPLOAD_CONCURRENCY` | `2` | 在途上传请求上限（超 → 429） | `config.py` |
| `INGEST_QUEUE_HIGH_WATERMARK` | `50` | 积压水位，仅拒多文件（0 = 关闭） | `config.py` |
| `INGEST_LOCAL_ROOTS` | `""` | `/ingest/dir` 白名单（空 = 端点 404） | `config.py`|
| `INGEST_ARCHIVE_MAX_MB` | `500` | 上传 zip 体积上限 | `config.py` |
| `INGEST_ARCHIVE_MAX_FILES` / `_MAX_UNCOMPRESSED_MB` / `_MAX_RATIO` | `2000` / `5000` / `100` | zip-bomb 三重上限 | `config.py` |
| `INGEST_ARCHIVE_TMP_DIR` / `INGEST_ARCHIVE_TTL_HOURS` | `""`（系统 temp）/ `24` | 解包位置与保留时长（GC 用） | `config.py` |
| `INGEST_GC_INTERVAL_S` | `300` | housekeeping 间隔（首轮启动即跑） | `config.py` |
| `INGEST_CONCURRENCY` | `2` | 并行流水线数（队列 worker 数，`/api/jobs/queue` 的 `concurrency`） | `config.py` |
| `OPENSEARCH_URL` / `MINIO_BUCKET` / `EMBEDDING_URL` / `DOCLING_URL` | `http://localhost:9200` / `paperbox` / `http://localhost:8090` / `""` | `/health` 探针目标：embedding 与 docling 各探自己的 `GET /health`（docling 要求 **200**，未配置 `DOCLING_URL` 时该项为 `disabled`） | `config.py`, ,；`health.py` |
| `SEARCH_LOG_ENABLED` / `SEARCH_LOG_RESULTS_LIMIT` | `True` / `20` | `POST /api/search` 写日志开关与结果条数上限 | `config.py` |
| `RERANK_ENABLED` / `RERANK_TIMEOUT` | `True` / `10.0` | 响应 `rerank` 块与两阶段检索 | `config.py`；`app/services/search_pipeline.py` |
| `QUERY_REWRITE_ENABLED` + `_URL`/`_MODEL`/`_API_KEY` | `False` / `""` | 改写开关；开启时三者必填否则启动即报错 | `config.py`|
| `MCP_ENABLED` / `MCP_ALLOWED_HOSTS` | `False` / `""` | 是否挂载 `/mcp`；开启时白名单**必填**（空则启动报错）。白名单要同时写 `host` 与 `host:*` | `config.py`, , `_check_mcp` |
| `MCP_WRITE_ENABLED` + `MCP_ALLOW_DELETE`/`_METADATA_WRITE`/`_REINDEX` | `False` | 写工具总闸与三个分开关；关掉的工具**不注册** | `config.py` |
| `PAPER_API_KEYS` | `""` | 多 key 的 `prefix:key`（分号分隔），左边那半即该钥匙的 prefix（审计与日志都用它）；**按值认身份**，共用一把 key 会共用同一个 prefix | `config.py`, `app/mcp/auth.py` |
| `MCP_MAX_CHARS` / `_CEILING`、`MCP_WAIT_SECONDS`、`MCP_TOOLSET`、`MCP_PUBLIC_BASE_URL` | `8000`/`32000`、`120`、`v1`、`""` | 正文预算与上限（超上限报错不夹紧）、写工具等待上限、工具集版本、无请求上下文时的下载兜底基址 | `config.py` |
| `INGEST_ALLOW_PRIVATE_HOSTS` | `""` | `paper_import`/`/ingest` 访问私网地址的白名单（主机名或 CIDR）；空 = 只允许公网（SSRF 闸逃逸阀） | `config.py`, `app/services/net_guard.py` |

非配置常量：`RETRY_AFTER_SECONDS = 2`（`app/services/upload_admission.py` 的 `RETRY_AFTER_SECONDS`）、chunk 分页上限 200（`app/api/papers.py` 的 `get_paper_degradations()`）。探针超时不再是非配置常量：统一取 `HEALTH_HTTP_TIMEOUT`（默认 10s，`HEALTH_TIMEOUT_OVERRIDES` 可按依赖覆盖），见 `app/services/health_service.py` 的 `_timeout_for()` / `_effective_timeout()`。

## 7. 测试位置与覆盖（tests/xxx.py → 覆盖什么）

| 文件 | HTTP 层覆盖 | 证据 |
|---|---|---|
| `tests/test_ingest_files.py` | `/ingest/files`：逐 part 失败隔离、422、413、429+`Retry-After`、水位放行单文件 | `TestClient`；依赖覆盖；413；429 |
| `tests/test_ingest_file.py` | `/ingest/file` 旧契约回归、429 | `test_a_saturated_upload_gate_is_a_429()` |
| `tests/test_ingest_dir.py` | `/ingest/dir`：白名单 403、`..`/链接逃逸、dry_run | |
| `tests/test_ingest_compressed.py` | `/ingest/compressed`：非 zip 415、429、临时目录清理 | , `test_backlog_watermark_refuses_an_archive()` |
| `tests/test_metadata_api.py` | `/api/metadata/import`（dry_run/apply/覆写）、`review`、`attach`、`apply`、415 | , , |
| `tests/test_manual_metadata.py` | `GET/PATCH /api/papers/{id}/metadata`（含 404）、rollback | |
| `tests/test_deletion.py` | `papers_api.delete_paper` 直调：204、OpenSearch/MinIO 失败 503、顺序与幂等 | （非 TestClient） |
| `tests/test_upload_gc.py` | housekeeping 生命周期（周期任务启停）、幂等、不改作业行 | `README.md`（已精简，原测试清单移除） |
| `tests/test_ingest_queue.py` / `test_queue_priority.py` | `recover_jobs`/`mark_queued`、优先级与 `queued_high/low` | `README.md`（已精简） |
| `tests/test_job_retry.py` | 重试的原子认领与路由（服务层） | `README.md`（已精简） |
| `tests/test_search_log.py` | 检索日志行序列化与写入降级（服务层） | `README.md`（已精简） |

未覆盖（本模块视角）：`security.py` 的 `require_role` 内部分支细节（`extract_api_key` 的 Bearer/`X-API-Key` 回退有 `tests/test_auth_rest.py` 覆盖，Authorization 头存在但 scheme 非 Bearer 就不回退这一条仍无单测）、`GET /health`、`GET /api/search-logs` 路由、`GET /api/jobs/queue` 路由、`GET /api/jobs` 路由、`main.py` 的 lifespan 与 `request_id_middleware`（`TestClient` 只出现在 5 个文件：`test_ingest_files/file`、`test_ingest_dir`、`test_ingest_compressed`、`test_metadata_api`、`test_manual_metadata`）。

## 8. 未做 / 已知缺口

1. **无统一异常处理器**：Pydantic 校验失败的 422 响应体 `detail` 是列表，与 [MVP-SPEC.md](MVP-SPEC.md) 的「统一 `{"detail": "…"}`」不一致；代码里没有任何 handler 可以覆盖它。
2. **`/docs`、`/redoc`、`/openapi.json` 无鉴权**，匿名可获取完整接口清单。
3. **无 CORS、无通用限流**：跨源浏览器调用不可用；限流仅存在于上传准入（`upload_admission.py`）。是否存在反向代理层的限流未在本次阅读范围内，**未确认**。
4. **关闭不等待在途作业**：`job_queue.join()` 存在但 lifespan 未调用（`main.py`），重启会把中间态作业标 `INTERRUPTED`；UI 侧需提示用户重试。
5. **启动不校验 OpenSearch 索引/别名**，也不跑 DB 迁移（`main.py` 无相关调用）；索引由脚本与 worker 自行处理（`ensure_index` 的调用点未在本模块范围内确认）。
6. **`GET /api/jobs`、`GET /api/papers` 的 `limit/offset` 无上下界**，可通过极大值放大查询。
7. **`GET /health` 恒 200**，无法作为就绪探针区分「进程活」与「依赖可用」。
8. **`Retry-After` 固定 2 秒**，不是配置项，积压很深时可能过于乐观。
9. **MCP 侧的四项细化未做**（非缺口，见 `docs/architecture/11-mcp-agent-interface.md` [#12](11-mcp-agent-interface.md#12-未做与未验证事项当前口径)）：审计字段细化、错误码细化、预算与翻页边界细化、作业等待语义细化。
10. **MCP 无速率限制**：审计能看见谁调了多少，但没有任何 per-key 限流（"限流未做"是明确记录在案的缺口）。
11. **MCP 只在 Hermes 与 codex 上做过真机验收**：Claude Code（本机未安装）与自研 harness 的片段**未验证**；SSRF 闸残留 DNS rebinding 风险（解析与连接之间的竞态，已在 `net_guard.py` 明文标注）。
9. **文档漂移（以代码为准）**：`docs/architecture/MVP-SPEC.md` [#2](MVP-SPEC.md#2-api-一览实测-openapijson)未列 `POST /api/jobs/{job_id}/retry` 与 `GET /api/search-logs`；`README.md` 第 3.4 节 的导入示例含无效的 `-F source_type=…`（README 已精简，原 第 3.4 节 示例移除）且响应示例字段名写作 `total_records`（同上），代码实际返回 `total`（`app/schemas/metadata.py`、`app/services/metadata_import.py`）；[MVP-SPEC.md](MVP-SPEC.md) 提示该文件部分表述已过期。
10. **鉴权测试覆盖不全**：路由表（谁有门谁没门）与 401/403 语义已由 `tests/test_public_reads.py`、`tests/test_auth_rest.py` 钉住；仍缺「`Authorization` 头存在但 scheme 非 Bearer 时**不回退** `X-API-Key`」与 `AuthContextMiddleware` 数据库探测失败路径的用例。
