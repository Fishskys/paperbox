# paperbox 模块架构总览（00）

> 这是项目**按模块架构文档集的入口**。阅读顺序：先读本文件，再按需进具体模块。
> 每个模块文档均为独立文件（`docs/architecture/NN-*.md`），同一标注工程，交叉引用见文末。

| 项 | 内容 |
|---|---|
| 状态 | 依据 commit `3911a6b` 的工作树实测（2026-09-22） |
| 关键文件 | `app/main.py`、`app/core/config.py`、本文档集 |
| 相关文档 | `AGENTS.md`（环境契约/目录）、`.hermes/plans/2026-09-10_215600-paperbox-master-plan.md`（需求）、`docs/progress/project.md`（演进与实测）、`README.md`（使用）、`docs/architecture/MVP-SPEC.md`（接口摘要） |

## 1. 职责边界

- 本文件是**一览**：模块地图、三个数据源、一条请求全链路、依赖关系、文档索引。
- 不重复各模块文档里的细节；只说结构关系。
- paperbox 对外有**两套面**，同一进程、共用配置/鉴权/日志与 service 层：
  **REST**（`/api/**`，人/程序用）与 **MCP**（`/mcp`，agent 用，默认关闭，见 11 篇）。

## 2. 模块地图

| 目录 | 模块 | 内容 | 行数 | 架构文档 |
|---|---|---|---|---|
| `app/` | 入口 | FastAPI 应用、lifespan、请求 ID 中间件 | 96 | 07 |
| `app/api/` | HTTP 层 | 路由 + 鉴权 + 错误映射 | ~2000 | 07 |
| `app/core/` | 内核 | 配置 / 日志 / 安全 / 错误码 | ~660 | 07 |
| `app/db/` | 持久层 | 14 张表、session | ~820 | 01 |
| `app/services/` | 业务层 | 23 个服务模块（导入/检索/元数据/存储/一致性…） | ~9000 | 各模块 |
| `app/search/` | 检索 | hybrid / RRF / OpenSearch 客户端 / snapshot（元数据快照） | ~1200 | 05 |
| `app/parsing/` | 解析 | PDF 文本 / 结构 / 切块 | ~1080 | 03 |
| `app/workers/` | 任务层 | 流水线 / 队列 / 清理 | ~1850 | 02 / 09 |
| `app/mcp/` | agent 接口 | MCP 端点（传输 / 静态 Bearer / 审计 / 引用）+ 10 个工具（读 6、写 4），直调 service 层 | ~2600 | 11 |
| `app/eval/` | 评测 | 指标纯函数 | ~180 | 10 |
| `infra/` | 部署 | 四依赖容器 + embedding 服务 | — | 10 |
| `scripts/` | 工具 | 运维脚本 / 验收脚本 | — | 10 |
| `migrations/` | 数据 | Alembic 迁移 | — | 01 |
| `tests/` | 测试 | 单元测试（含 conftest 内存 SQLite） | — | 各模块§7 |
| `docs/` | 文档 | 架构文档集（本目录）+ 既有文档 | — | 本文 |

## 3. 数据源与存储位置

| 数据 | 位置 | 模块文档 |
|---|---|---|
| 结构化元数据（论文/来源/provenance/标识符/venue/标签） | PostgreSQL | 01 / 08 |
| 来源原始快照 `raw` | PG JSONB 列 | 08 |
| PDF 原件（多来源，主版本/非主版本） | MinIO | 01 / 09 |
| 可检索文本 + 向量（chunks） | OpenSearch | 05 |
| 三端一致性视图（运行时对账，不落库） | PG + MinIO + OpenSearch | 01 / 05 |
| chunk 向量 | OpenSearch `knn_vector` | 04 / 05 |
| 检索日志（`search_queries`） | PostgreSQL | 05 |
| 任务状态（`ingestion_jobs`） | PostgreSQL | 02 |

## 4. 依赖关系（模块间）

```
HTTP 层 (app/api)
   │
   ├── 服务层 (app/services)
   │     ├── 流水线 (app/workers/tasks.py) ← 队列 (app/workers/queue.py)
   │     ├── 解析 (app/parsing) ← 切块 (chunking) ← structure
   │     ├── 检索 (app/search/hybrid + opensearch) ← ranking(RRF)
   │     ├── 元数据 (metadata_*) ← provenance ← identifiers ← tags ← venue
   │     ├── 存储 (object_storage: MinIO)
   │     └── 外部服务 (embedding_service: 容器 :8090)
   │             ├── /embed （向量）
   │             └── /rerank （精排）
   │
   └── 数据层 (app/db: PostgreSQL / OpenSearch / MinIO)
```

关键依赖事实：
- **队列是唯一入口**：所有导入作业（含 reindex、重试）都进进程内队列调度（`queue.py`），应用必须 `--workers 1`。
- **`provenance_service.write_field` 是唯一的「claim → papers 列」映射表**（AGENTS §3.9，08 文档详述）：新增可合并字段必须先加映射，否则字段漂移。
- **过滤字段是索引时快照**：改元数据不自动影响检索过滤 —— 改元数据用 `scripts/refresh_index_metadata.py`（秒级、不重算向量），要连向量一起重算才 reindex（05 文档）。
- **三端一致性可自检**：`GET /api/consistency` / `scripts/check_consistency.py` 只读对账 PG ↔ MinIO ↔ OpenSearch，不抛异常（01 文档）。
- **主版本规则**：`published_pdf > original > arxiv_pdf`，只有主版本被解析/索引（08 文档）。

## 5. 一条请求全链路

以「导入一个 PDF」为例（高亮各模块分工）：

```
POST /api/papers/ingest/files（多文件 multipart，流式写 staging 边算 sha256）
  → 准入检查（429 / 422 / 413）
  → ingestion_service（建 job，写 ingestion_jobs，状态 QUEUED）
  → queue.py（优先级：单文件=交互插队 / 多文件=批；并发上限 INGEST_CONCURRENCY）
  → workers/tasks.py 流水线：
        STORED（存 MinIO，删 staging，检查点）
        → 回溯元数据（pdf_embedded → 启发式 → merge）
        → PARSING（parsing/pdf.py 抽文本 + structure 识别章节）
        → CHUNKING（chunking.py 切块）
        → EMBEDDING（embedding_service → 容器 :8090 /embed）
        → INDEXING（`_index_rows` 组装文档：文本 + 向量 + `snapshot.paper_metadata_snapshot`；写别名 paper_chunks_current）
        → COMPLETED（fingerprint 可能升级，DOI > arXiv > title > sha256）
  → housekeeping（周期清理 staging / 解包临时目录）
```

检索链路（GET/POST /api/search）见 05、06；元数据导入链路（POST /api/metadata/import）见 08。

一条 **MCP 链路**（agent 视角，同一进程内的另一条入口）：

```
POST /mcp（Streamable HTTP，Bearer）
  → McpAuthMiddleware（最外层：401/403，并把 agent 名放进 ContextVar）
  → McpMountPathMiddleware（/mcp → /mcp/，避免 307）
  → SDK 的 Host 白名单校验（不在 MCP_ALLOWED_HOSTS → 421）
  → tools/call
       读工具 → search_pipeline / chunk_service（与 REST 同一实现，结果逐位相同）
       写工具 → 未开开关时**根本没注册**；开了则调 ingestion/paper_service，dry_run 默认 true
  → 统一信封 {data, meta, warnings, citations}；失败抛 ToolFailure → isError + 契约错误 JSON
  → 审计一行结构化日志（工具 / agent / 参数摘要 / 结果 / 影响面 / 耗时）
```

## 6. 配置项

所有配置键以 `app/core/config.py`（pydantic-settings）为源，仓库根 `.env` 提供值；容器侧变量在 `infra/.env`（AGENTS §3.4）。键名/默认值/作用见各模块文档 §6，或 `.env.example`。

## 7. 测试

- 单元测试全套在 `tests/`（pytest，跑内存 SQLite，不碰真机）；每个模块文档 §7 列出对应测试文件与覆盖点。
- 真机验证放 `scripts/`（如 `acceptance_metadata.py`、`bulk_ingest*.py`），脚本自带 `--cleanup`，不污染真实数据（AGENTS §6）。

## 8. 未做 / 已知缺口

- 前端在独立仓库 `paperbox-webui`；本仓库只提供 REST API。
- 已知未连通：`Redis/Celery`（任务用进程内队列）、`OCR`、`arm64` 等（plan §30）。
- 文档口径：`docs/architecture/metadata-architecture.md` 是**设计背景**（目标/取舍/逐字段映射/术语表），**实现现状**看 `docs/architecture/08-metadata.md`（表 + 实现架构），实测数字看 `docs/progress/project.md` §17。
- MCP 侧：**无速率限制**；仅在 Hermes 与 codex 上做过真机验收（Claude Code 本机未装、自研 harness 片段未验证）；
  四项细化（审计字段 / 错误码 / 预算翻页 / 作业等待语义）未做，见 11 篇 §12。
- 各模块的更细缺口见对应文档 §8。

## 文档索引

| # | 文档 | 模块 |
|---|---|---|
| 00 | `00-overview.md` | 总览（本文） |
| 01 | `01-storage.md` | 存储层：PostgreSQL / MinIO / OpenSearch / 迁移 |
| 02 | `02-ingestion-pipeline.md` | 导入流水线状态机与队列调度 |
| 03 | `03-parsing-chunking.md` | PDF 解析、结构识别、切块、内嵌元数据 |
| 04 | `04-embedding.md` | embedding 容器服务与应用侧调用 |
| 05 | `05-search.md` | 检索：hybrid / RRF / 过滤 / 聚合 |
| 06 | `06-rerank-rewrite.md` | 两阶段精排与查询改写 |
| 07 | `07-api.md` | HTTP API 层 |
| 08 | `08-metadata.md` | 元数据层（现状版） |
| 09 | `09-upload-queue.md` | 上传入口与文件处理 |
| 10 | `10-eval-ops.md` | 评测闭环与运维部署 |
| 11 | `11-mcp-agent-interface.md` | MCP agent 接口：传输、鉴权、10 个工具契约、验收与客户端接入 |

**同级补充文档**（非编号）：`metadata-architecture.md`（元数据层设计稿：逐字段映射、术语表）、
`hermes-integration.md`（Hermes 工具定义与调用范式）、`MVP-SPEC.md`（接口摘要）。

**其他目录**：`docs/progress/`（`project.md` 项目级进度 · `parser.md` 解析线）、`docs/examine/`（审查报告快照）、
`docs/old/`（已完成阶段的规范）、`.hermes/plans/`（各阶段 plan；`2026-09-10_215600-paperbox-master-plan.md` 是权威需求）。