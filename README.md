# paperbox

## 1. 项目简介

paperbox 是一个论文知识库后端服务：把 PDF 变成可编程调用的检索与元数据能力，不含前端与 Agent 逻辑，只提供事实与接口。亮点：① 五种导入入口（URL/单文件/批量/目录/zip），自动解析、切块、向量化、建索引；② 混合检索：关键词与向量双路融合、交叉编码器精排，按论文聚合并附证据块；③ 元数据三层模型 + 标识符去重，可合并、修正与回滚；④ 可运维：导入进度、失败归因、三端对账与快照备份。

## 2. 快速开始

依赖 [Docker](https://docs.docker.com/engine/install/) 与 [uv](https://docs.astral.sh/uv/)。
下面按仓库默认配置走，不需要改任何配置项。

```bash
# 1) 依赖服务：PostgreSQL / OpenSearch / MinIO / Embedding
git clone https://github.com/Fishskys/paperbox.git && cd paperbox
cp infra/.env.example infra/.env
cd infra && docker compose up -d && cd ..

# 2) Python 环境（按 uv.lock 建 .venv 并装依赖）
uv sync

# 3) 应用配置（模板值即为默认值，直接复制）
cp .env.example .env

# 4) 建表结构与检索索引（幂等，可重复执行）
uv run alembic upgrade head
uv run python scripts/create_index.py

# 5) 启动 API（默认 0.0.0.0:8077，Bearer 鉴权）
uv run uvicorn app.main:app --host 0.0.0.0 --port 8077
```

另开一个终端自检：`uv run python scripts/healthcheck.py`（四个依赖 + 应用 + 检索管道逐项检查），
交互式接口文档在 <http://127.0.0.1:8077/docs>。

> 只解析 PDF 时无需额外服务：默认解析后端 docling 是可选的独立服务，未配置地址时会自动降级为内置的 pypdf 解析，
> 并把降级原因记进账本。Windows 开发机上依赖服务跑在 WSL2 里，把第 1 步换成
> `wsl -e bash -lc "cd /mnt/<盘>/.../paperbox/infra && docker compose up -d"` 即可。

具体部署（服务器形态、数据目录、备份迁移）与全部配置项、接口参数、使用示例，参考[用户手册](UserManual.md)。

## 3. 项目架构

```
调用方（Hermes / 其他程序）
      │  HTTP + Bearer
      ▼
  paperbox API（FastAPI）
      │
      ├─ 导入流水线：上传 → 解析 → 切块 → 向量化 → 索引
      ▼
PostgreSQL（元数据）  MinIO（PDF 原件与解析产物）  OpenSearch（全文 + 向量）  Embedding 容器（向量与精排推理）
```

| 环节 | 做什么 | 用什么实现 |
|---|---|---|
| 采集与上传 | 五种入口接收 PDF、算指纹判重、落 staging | FastAPI 路由 + 进程内优先级队列（`app/workers/queue.py`），单 worker 跑流水线 |
| 解析 | PDF → 结构化 markdown（页码 / 章节 / 表格） | 远端 **docling-serve**（主）与内置 **pypdf**（降级）双后端，产物缓存到 MinIO 可重放 |
| 切块 | 切成约 400 token 的块，带页码与章节标题 | 定长与「语义低谷」两种策略（`app/parsing/chunking.py`） |
| 向量化 | 文本 → 1024 维向量 | 独立推理容器（fastembed / `intfloat/multilingual-e5-large`），单队列限批 |
| 精排 | 查询-候选交叉编码重排 | 同一容器内的交叉编码器（可换任意 ONNX 模型，现用 int8 多语言档） |
| 索引 | 块级文档进检索库（全文 + 向量） | OpenSearch，CJK bigram 分词 + kNN；别名切换做迁移 |
| 检索 | 双路召回 → 融合 → 按论文折叠 → 精排 → 聚合证据 | **引擎侧 RRF(k=60) + collapse**（默认，`app/search/native.py`）；可切回应用侧双路融合 + 聚合（`app/search/hybrid.py`） |
| 元数据 | 论文 / 来源记录 / 字段级账本三层模型，标识符去重 | PostgreSQL + SQLAlchemy + Alembic 迁移 |
| 服务与运维 | REST API、检索日志、一致性对账、健康检查、快照 | FastAPI + `scripts/` 下的运维脚本 |

## 4. 目录结构

```
paperbox/
├─ app/           应用代码：api（路由）· services（业务）· search（检索）· parsing（解析与切块）
│                · workers（进程内队列与流水线）· db（模型）· schemas（契约）· core（配置与日志）· eval（指标）
├─ infra/         依赖服务的 docker compose（PostgreSQL / OpenSearch / MinIO / Embedding）+ docling 镜像
├─ migrations/    Alembic 数据库迁移
├─ scripts/       运维与验收脚本（建索引 / 批量导入 / 评测 / 对账 / 快照 / 健康检查 / 真机验收）
├─ tests/         单元测试（不连真机依赖）
├─ docs/          设计文档：architecture · progress · examine · old
├─ evals/         人工定标评测集与评测报告
├─ logs/          运行日志：app · eval · codex
├─ pyproject.toml uv 依赖与工具配置（另有 uv.lock / .python-version）
├─ Dockerfile     应用镜像；docker-compose.yml 只用它编排应用本身
└─ .env.example   应用配置模板（依赖服务的模板是 infra/.env.example，两份都要复制）
```

## 5. 能力一览

> **给 agent 用**：paperbox 也能作为 **MCP 服务**被 codex / Claude Code / Hermes 等直接调用
> （检索 + 读正文 + 导入/维护，默认关闭，需配 `MCP_ENABLED` 与白名单）。接入片段与排障见
> `UserManual.md` §1.7 与 `docs/architecture/11-mcp-agent-interface.md` §10。

所有接口都在 `/api/**` 下，除 `/`、`/health`、`/docs` 外均需 `Authorization: Bearer <PAPER_API_KEY>`。

| 分组 | 方法 | 端点 | 说明 |
|---|---|---|---|
| 服务 | GET | `/` | 服务名与版本 |
| 服务 | GET | `/health` | 应用与四个依赖的健康状态 |
| 服务 | GET | `/api/consistency` | 三端（数据库 / 对象存储 / 检索库）只读对账 |
| 导入 | POST | `/api/papers/ingest` | 按 URL 导入 |
| 导入 | POST | `/api/papers/ingest/file` | 上传单个 PDF |
| 导入 | POST | `/api/papers/ingest/files` | 一次上传多个 PDF |
| 导入 | POST | `/api/papers/ingest/dir` | 导入服务器本机目录（零传输） |
| 导入 | POST | `/api/papers/ingest/compressed` | 导入 zip 压缩包 |
| 论文 | GET | `/api/papers` | 论文列表（支持过滤与分页） |
| 论文 | GET | `/api/papers/{paper_id}` | 论文详情 |
| 论文 | DELETE | `/api/papers/{paper_id}` | 删除论文（软删） |
| 论文 | GET | `/api/papers/{paper_id}/chunks` | 该论文的切块 |
| 论文 | GET | `/api/papers/{paper_id}/file` | 下载 PDF 原件 |
| 论文 | GET | `/api/papers/{paper_id}/degradations` | 该论文的解析降级记录 |
| 论文 | POST | `/api/papers/{paper_id}/reindex` | 重建该论文的切块与向量 |
| 检索 | POST | `/api/search` | 检索（关键词 / 语义 / 混合，可精排、可出过滤面） |
| 检索 | GET | `/api/search-logs` | 历史检索日志（复盘用） |
| 作业 | GET | `/api/jobs` | 导入作业列表 |
| 作业 | GET | `/api/jobs/queue` | 队列状态（在跑 / 排队 / 并发上限） |
| 作业 | GET | `/api/jobs/{job_id}` | 单个作业的进度与失败归因 |
| 作业 | POST | `/api/jobs/{job_id}/retry` | 重试失败的作业 |
| 元数据 | GET | `/api/papers/{paper_id}/metadata` | 论文元数据与字段来源账本 |
| 元数据 | PATCH | `/api/papers/{paper_id}/metadata` | 手动修正元数据 |
| 元数据 | POST | `/api/papers/{paper_id}/metadata/rollback` | 回滚到某个历史版本 |
| 元数据 | GET | `/api/metadata/review` | 待复核清单（冲突 / 歧义） |
| 元数据 | POST | `/api/metadata/import` | 导入外部题录（IEEE raw / CSL-JSON / JSON） |
| 元数据 | POST | `/api/metadata/apply` | 提交复核决定 |
| 元数据 | POST | `/api/metadata/sources/{source_id}/attach` | 把来源记录挂到指定论文 |

## 6. 声明

本项目基于 [MIT License](LICENSE) 开源，版权归 Fishskys 所有；当前版本 **0.2.0**，变更历史见 [CHANGELOG.md](CHANGELOG.md)。

使用中遇到问题或有想法，欢迎提 [Issue](https://github.com/Fishskys/paperbox/issues) 与 Pull Request；
如果它对你的工作有帮助，欢迎点一个 ⭐ [Star](https://github.com/Fishskys/paperbox)。
