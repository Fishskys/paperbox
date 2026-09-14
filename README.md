# paperbox

论文知识服务：**导入 → 解析 → 分块 → Embedding → 索引 → 混合检索 → REST API**。
无前端、无 Agent 逻辑，只提供事实与检索能力，供 Hermes 主 Agent 通过 HTTP 调用
（架构与需求见 `plan.md`，实现规范见 `MVP-SPEC.md`）。

```
Hermes / 其他调用方
        │  HTTP + Bearer PAPER_API_KEY
        ▼
   paperbox (FastAPI, 端口 8077)
        │
   ┌────┼──────────┬───────────┐
   ▼    ▼          ▼           ▼
PostgreSQL  OpenSearch     MinIO     Embedding Server
元数据/关系  全文+向量索引   PDF 原文   intfloat/multilingual-e5-large
```

## 1. 能力一览

| 能力 | 端点 |
|------|------|
| 依赖健康 | `GET /health` |
| 论文列表（分页 / 按状态 / 标题搜索） | `GET /api/papers` |
| 任务列表（最近 N 条） | `GET /api/jobs` |
| 任务重试（手动触发，plan §22） | `POST /api/jobs/{job_id}/retry` |
| URL 导入 / 文件上传导入 | `POST /api/papers/ingest`、`POST /api/papers/ingest/file` |
| 任务状态 | `GET /api/jobs/{job_id}` |
| 论文元数据 | `GET /api/papers/{paper_id}` |
| 论文原文 | `GET /api/papers/{paper_id}/file` |
| 论文 chunks | `GET /api/papers/{paper_id}/chunks` |
| 删除（索引 + 对象 + 记录） | `DELETE /api/papers/{paper_id}` |
| 重建单篇索引 | `POST /api/papers/{paper_id}/reindex` |
| 检索（keyword / semantic / hybrid） | `POST /api/search` |
| 检索日志（最近的查询与命中） | `GET /api/search-logs` |
| 检索日志（Bad Case 分析，最近 N 条） | `GET /api/search-logs` |

除 `/health` 外全部端点需要 `Authorization: Bearer <PAPER_API_KEY>`。

## 2. 依赖服务

paperbox 只打包应用本身，基础设施在 `infra/docker-compose.yml`（PostgreSQL 15 /
OpenSearch 3.6 / MinIO / Embedding Server）。**应用跑在 Windows 宿主，基础设施跑在
WSL2 的 Docker 里**——本机 WSL2 为 mirrored 网络模式，Windows 经 `127.0.0.1` /
`localhost` 直连 WSL 端口是通的，前提是该端口在 WSL 内的 **ufw 白名单**里（默认
deny incoming；镜像回环流量走 `loopback0` 接口，不在白名单的端口会 `curl` exit 28 超时）。四个
依赖端口已放行，因此 `.env` 中使用 `127.0.0.1`（**不要写 `localhost`**：WSL 镜像模式不转发 IPv6 回环，而 Windows 的 `localhost` 优先解析到 `::1`，每次连接要先撞 8 秒超时再退回 IPv4）：

```bash
wsl -e bash -lc "cd /mnt/<盘>/hermes/paperbox/infra && docker compose up -d"   # 启动依赖（换成本机实际路径）
wsl -e bash -lc "ufw allow 9200/tcp"   # 新增容器 publish 端口时放行（9200/9000/8090/5432 已放行）
uv run python scripts/healthcheck.py       # 检查四个依赖 + 应用端口
```

| 服务 | 默认地址 | 说明 |
|------|----------|------|
| PostgreSQL | `127.0.0.1:5432` | db `paperbox`，Alembic 管理 schema |
| OpenSearch | `http://127.0.0.1:9200` | 单节点，安全插件关闭（内网自用） |
| MinIO | `127.0.0.1:9000` | bucket `paperbox`，路径 `papers/<paper_id>/original.pdf` |
| Embedding | `http://127.0.0.1:8090` | `POST /embed`，模型 `intfloat/multilingual-e5-large`（1024 维，512 token 上限，`MAX_BATCH` 限批）；`POST /rerank` 交叉编码器精排（模型 `RERANK_MODEL`，单次候选上限 `RERANK_MAX_BATCH`）；`GET /health`、`GET /info` 均含 `rerank_model`，`/info` 另含 `max_batch` 与 `rerank_max_batch` |

## 3. 快速开始

> 本项目的 Python 环境**统一由 [uv](https://docs.astral.sh/uv/) 管理**：依赖声明在
> `pyproject.toml`，锁定在 `uv.lock`，解释器由 `.python-version` 指定（3.12）。
> 不要再用 `pip install` 或手建 venv —— `uv sync` 会按锁文件把 `.venv` 建好。

```bash
# 1) 依赖服务（PostgreSQL/OpenSearch/MinIO/Embedding）
cd infra && docker compose up -d && cd ..

# 2) Python 环境（uv 建 .venv 并装依赖，含 dev 组）
uv sync

# 3) 配置
cp .env.example .env        # 填好 DSN / MinIO / API Key，或从现有 .env 调整

# 4) 数据库 schema 与索引
uv run alembic upgrade head
uv run python scripts/create_index.py

# 5) 启动（监听地址/端口由 PAPER_API_HOST / PAPER_API_PORT 决定）
uv run uvicorn app.main:app --host 0.0.0.0 --port 8077
```

> **Windows 开发机**只有两处不同：依赖服务跑在 WSL 里 →
> `wsl -e bash -lc "cd /mnt/<盘>/hermes/paperbox/infra && docker compose up -d"`；
> 拷配置用 `copy .env.example .env`。其余命令一致（`scripts/*.py` 里需要 docker 的地方会自动
> 判断用 `docker` 还是 `wsl -e docker`，见 `scripts/build_eval_set.py::_docker_argv`）。

### 3.1 部署到 Linux 服务器

**两种形态，按需要选**：

- **A. 应用直跑在宿主**（最简单，与开发机相同）：依赖四个容器跑 docker，应用用 `uv run uvicorn`
  或 systemd 托管，依赖地址填 `127.0.0.1:PORT`。
- **B. 应用也容器化**：`docker compose -f docker-compose.yml up -d --build`（根目录那份 compose
  只打包应用）。此时依赖地址要么走宿主网关（`host.docker.internal`，compose 已加
  `extra_hosts: host.docker.internal:host-gateway`，Linux 上 Docker 引擎**不自带**这个名字），
  要么把两份 compose 加进同一张 external 网络、地址直接写服务名（`postgres:5432` 等）。

**数据持久化与目录**：compose 默认把数据落在 compose 文件旁的 `./data/<service>`
（`postgres` / `opensearch` / `minio` / `embedding-models`，已进 `.gitignore` 与 `.dockerignore`）。
服务器上建议显式指定绝对路径（写进 `infra/.env`，docker compose 只读该目录的 `.env`）：

```bash
POSTGRES_DATA_DIR=/srv/paperbox/data/postgres
OPENSEARCH_DATA_DIR=/srv/paperbox/data/opensearch
MINIO_DATA_DIR=/srv/paperbox/data/minio
EMBEDDING_MODELS_DIR=/srv/paperbox/data/embedding-models
# 依赖端口只对本机开放（API 端口另经反代/防火墙放行）
PAPERBOX_BIND_IP=127.0.0.1
```

**迁移已有数据**（三处都要搬，别重算向量）：
PG 用 `pg_dump`/`pg_restore`；MinIO 用 `mc mirror`；OpenSearch 可照 `scripts/create_index.py`
的 `--migrate-from` 做服务端 `_reindex`（复制文档、不重算 embedding，~2883 docs 仅数秒），
或对索引做 snapshot/restore。**66 篇论文的向量已经算好（2883 chunks，重算约 1.2h），迁移时务必搬运而非重建。**

**内存/并发调参**（下表是本机 9GB WSL 上的实测安全值，服务器按物理内存放大）：
`ORT_THREADS=4`、`MAX_BATCH=16`（/embed 限批）、`RERANK_MAX_BATCH=4`（多语言精排 jina 的激活内存
随 `token × 候选数` 增长，16 条候选峰值 5.1GB）、`OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g`。

**注意单 worker**：导入流水线走 FastAPI `BackgroundTasks`（进程内），因此应用要保持
`--workers 1`；要横向扩 worker/多副本，得先把导入改成外部队列（当前不做）。

常用的 uv 命令：`uv sync`（对齐环境）、`uv add <pkg>`（加依赖）、`uv lock --upgrade`（升级并重锁）、`uv run <cmd>`（在项目环境里执行）。

## 4. 使用示例

```bash
API_KEY=$(grep PAPER_API_KEY .env | cut -d= -f2)

# 导入（arXiv 直达 PDF）
curl -X POST http://127.0.0.1:8077/api/papers/ingest \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"source_type":"url","source":"https://arxiv.org/pdf/1706.03762"}'
# -> {"job_id":"...","status":"RECEIVED"}

# 轮询任务（RECEIVED -> DOWNLOADING -> STORED -> PARSING -> CHUNKING -> EMBEDDING -> INDEXING -> COMPLETED）
# 每个阶段转换都会 commit，因此轮询能看到真实中间态（不再只有 RECEIVED / COMPLETED）；
# 失败时 stage/progress 保留在失败发生的那一步，error_code 给出结构化归因
# （NO_TEXT_LAYER / ENCRYPTED_PDF / CORRUPT_PDF / DOWNLOAD_FAILED / OVERSIZED /
#  UNSUPPORTED_TYPE / DUPLICATE_FINGERPRINT / EMBEDDING_FAILED / INDEX_FAILED /
#  STORAGE_FAILED / INTERNAL）。
curl http://127.0.0.1:8077/api/jobs/<job_id> -H "Authorization: Bearer $API_KEY"
# -> {"job_id":"...","paper_id":"...","stage":"EMBEDDING","progress":80.0,"duplicate":false,
#     "error_code":null,"error_message":null,"created_at":"...","updated_at":"...","finished_at":null}
# 失败样例："error_code":"NO_TEXT_LAYER"（该 PDF 无文本层，需 OCR，MVP 不支持）

# 手动重试一个 FAILED 作业（plan §22）
# - STORED 检查点之后才失败的（job 已有 paper_id）：复用论文行与 MinIO 原文，
#   按 reindex 语义从 PARSING 重跑到 INDEXING（不做指纹弃单）
# - STORED 之前失败的（下载/校验阶段）：payload 里源信息还在，整体重跑
# - 成功后清空 error_code/error_message；论文状态回到 INDEXED
# - 非 FAILED 作业返回 409；确定性失败（如 NO_TEXT_LAYER）重试只会原样再失败
curl -X POST http://127.0.0.1:8077/api/jobs/<job_id>/retry \
  -H "Authorization: Bearer $API_KEY"
# -> 202，body 为重置后的作业（stage=RECEIVED），继续轮询 GET /api/jobs/<job_id> 即可

# 上传本地 PDF
curl -X POST http://127.0.0.1:8077/api/papers/ingest/file \
  -H "Authorization: Bearer $API_KEY" -F file=@paper.pdf

# 混合检索（BM25 + 向量 + RRF 融合 + 论文级聚合）
curl -X POST http://127.0.0.1:8077/api/search \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"low power SRAM leakage reduction","mode":"hybrid","top_k":5,
       "filters":{"year_from":2015,"year_to":2026}}'
```

检索响应（论文级结果 + 证据片段，`score` 为本题内归一化的 0~1）：

```json
{"query":"...","rewritten_query":null,"mode":"hybrid","total":1,"took_ms":620,
 "rerank":{"enabled":false,"model":null,"took_ms":null},
 "rewrite":{"enabled":false,"applied":false,"model":null,"took_ms":null},
 "results":[{"paper_id":"...","title":"Attention Is All You Need","authors":["Ashish Vaswani","..."],
   "year":2017,"doi":null,"score":1.0,"relevance":"high",
   "evidence":[{"chunk_id":"...","page":3,"section":"3.2 Attention","text":"..."}]}]}
```

> `rewritten_query` / `rewrite` 只在服务端开启查询改写时参与（见 §5）；关闭时恒为上面的
> `null` / `false`，响应与旧版本一致。

## 5. 检索模式

- `keyword`：OpenSearch BM25（`title^2` + `text`）
- `semantic`：查询向量 → `knn`（hnsw/l2，1024 维）
- `hybrid`（默认）：两路各取 `top_k*5` 后用 **RRF(k=60)** 融合
- 过滤器：`year_from`、`year_to`、`authors`、`venue`、`doi`、`arxiv_id`、`tag`
- `rerank=true`（`POST /api/search`）：两阶段精排——先按 `top_k * RERANK_CANDIDATES` 扩大候选，再用交叉编码器（`RERANK_MODEL`）重排，取 `top_k * 2` 交给论文级聚合；服务不可用时自动降级为原顺序（`rerank_score` 为 `null`），不报错
- **精排模型与限批（`RERANK_MODEL` / `RERANK_MAX_BATCH`）**：交叉编码器的激活内存随 `(token × 候选数)` 增长，所以精排有独立上限，与 embedding 的 `MAX_BATCH` 解耦（共用一个旋钮要么撑爆精排、要么让正常导入吃 422）。本机实测（WSL 9GB，`ORT_THREADS=4`，文档截断 2000 字符）：`jinaai/jina-reranker-v2-base-multilingual` 加载占 1.9GB，单批 4 条峰值 ~2.4GB、8 条 ~3.3GB、**16 条 ~5.1GB**，速度 ~1.1–1.4 s/候选；`Xenova/ms-marco-MiniLM-L-6-v2` 16 条仅 ~0.7GB、0.09 s/候选。**本机现用多语言档：`RERANK_MODEL=jinaai/jina-reranker-v2-base-multilingual` + `RERANK_MAX_BATCH=4`**（换轻量档请把 `RERANK_MAX_BATCH` 一起调回 16）
- **精排超时必须跟着放大（`RERANK_TIMEOUT`）**：应用侧候选数 = `top_k × RERANK_CANDIDATES`（默认 5），精排后保留 `top_k × 2`。多语言档实测 ≈ **0.4–0.47 s/候选**（文档截断 2000 字符）⇒ `top_k=1` 约 2.3s、`top_k=10`（50 条候选）约 20s。默认 10 秒会让精排**静默降级**（响应里 `rerank.model=null`、`rerank_score=null`，日志 `rerank request failed ... {"error":"timed out"}`，结果仍是"能搜到但没重排"）→ 本机设 `RERANK_TIMEOUT=60`，覆盖到约 `top_k ≤ 28`；`top_k=50`（250 条候选 ≈100s）会超时降级，需要继续调大超时或调小 `RERANK_CANDIDATES`。实测端到端：`top_k=3` → 6.9s（精排 6.05s）、`top_k=10` → 20.4s（精排 19.7s），响应正常回报 `model` 与 `took_ms`
- **查询改写（可选开关，P1 I，默认关闭）**：`QUERY_REWRITE_ENABLED=true` 时，含 CJK 的查询先经 `POST {QUERY_REWRITE_URL}/chat/completions` 改写成英文检索式再检索；响应多出 `rewritten_query`（实际检索文本）与 `rewrite{enabled,applied,model,took_ms}`，`query` 始终返回原始值（不入日志表之外的任何替换）。只对含 CJK 的查询改写，纯英文查询零额外调用；LLM 不可达/超时自动降级为原查询（仍 200，`rewrite.applied=false`）。开启时 `QUERY_REWRITE_URL`/`QUERY_REWRITE_API_KEY`/`QUERY_REWRITE_MODEL` 必须齐备，否则启动即报错（不会静默失效）。实测收益（10 条中文定标查询）：HR@1 **0.30 → 0.90**、MRR 0.473 → 0.950（真实 LLM 改写，`evals/report-zh-llm-rewrite.md`；阈值细节与配置键见 `progress.md` §12）

只用一个 embedding space：换模型时必须新建 `paper_chunks_v2` 并切别名，不要覆盖旧向量。当前生产索引正是 `paper_chunks_v2`（CJK 分词），别名 `paper_chunks_current` → v2。

## 6. 脚本

| 脚本 | 用途 |
|------|------|
| `scripts/create_index.py` | 幂等创建 `paper_chunks_v1` + 别名 `paper_chunks_current`；`--index/--alias` 可指定，`--migrate-from <old>` 服务端 `_reindex` 整批拷贝（**不重新 embedding**）后原子切别名，旧索引保留供回滚 |
| `scripts/reindex.py` | 全量/指定论文重建（`--missing` 只补没有 chunks 的论文） |
| `scripts/purge_deleted.py` | 清理已删论文遗留的索引文档与 MinIO 对象（`--dry-run` 可先预览） |
| `scripts/healthcheck.py` | 四个依赖 + 应用健康检查与文档数统计 |
| `scripts/acceptance.py` | 端到端验收：跑 plan §38 的 8 条 MVP 标准（真实导入/检索/鉴权） |
| `scripts/bulk_ingest.py` | 批量导入语料（`evals/arxiv_ids.txt`，逐条串行 + 轮询作业；`--resume` 跳过已入库，`--dry-run` 只清单） |
| `scripts/eval.py` | 检索评测：对运行中的服务跑 `evals/queries.jsonl` + `labels.jsonl`，出 Hit Rate / Recall / MRR / NDCG 报告（JSON + Markdown） |
| `scripts/eval.py` | 检索评测：对**运行中**的服务跑 Hit Rate@K / Recall@K / MRR / NDCG@K，按 mode × rerank 分组，产出 JSON + Markdown 报告（`--out` / `--markdown`） |

验收（对着真实服务跑，约 1 分钟）：

```powershell
uv run python scripts\acceptance.py
```

## 7. 测试

```powershell
uv run pytest            # 或 uv run pytest tests -q
```

当前测试（`uv run pytest` 共 **78** 个用例）：

| 文件 | 覆盖 |
|------|------|
| `tests/test_parsing.py` | PDF 抽取、section 识别、分块（不跨 section、token 上限、overlap、无空洞） |
| `tests/test_aggregation.py` | 论文级聚合与 evidence 选择 |
| `tests/test_filters.py` | 过滤器构造（year/authors/venue/doi/arxiv_id/tag） |
| `tests/test_rrf.py` | RRF 融合排序 |
| `tests/test_deletion.py` | 删除清理顺序、失败中止与可重试 |
| `tests/test_fingerprint_release.py` | 删除即释放指纹（部分唯一索引） |
| `tests/test_job_progress.py` | 阶段进度落库：中间态对其它会话可见、失败保留失败阶段 |
| `tests/test_job_retry.py` | 手动重试：原子 FAILED→RECEIVED 认领（防重复触发）、按 paper_id 路由 reindex/整体重跑、成功清错误字段、再失败落新归因 |
| `tests/test_search_log.py` | 检索日志：结果压缩/截断、写入降级、行序列化 |
| `tests/test_failure_classification.py` | 失败归因：11 个 error_code + 无文本层 PDF |

**指纹与去重**：`papers.fingerprint` 按 `DOI > arXiv > 归一化标题+首作者+年 > sha256` 生成（`app/services/paper_service.py`）。导入时先以 `sha256` 占位，解析出元数据后**重算并落库**；若与另一篇存活论文撞指纹，则清理本次 chunks/索引/对象、软删本论文，作业以 `completed` + `duplicate=true` 指向既有论文结束（`app/workers/tasks.py`）。

**未覆盖（尚无单测）**：元数据规整（metadata normalization）。

失败作业的 `error_code` 取值固定为：`NO_TEXT_LAYER`、`ENCRYPTED_PDF`、`CORRUPT_PDF`、
`DOWNLOAD_FAILED`、`OVERSIZED`、`UNSUPPORTED_TYPE`、`DUPLICATE_FINGERPRINT`、
`EMBEDDING_FAILED`、`INDEX_FAILED`、`STORAGE_FAILED`、`INTERNAL`（见 `app/core/errors.py`）。

## 8. 目录结构

```
app/
  api/        health / papers / ingestion / jobs / search 路由
  core/       config(pydantic-settings) logging security(Bearer)
  db/         SQLAlchemy 2.x models（9 张表）+ session
  schemas/    Pydantic 请求/响应
  services/   paper / ingestion / embedding / metadata / object_storage / search
  parsing/    pdf 抽取、section 识别、分块
  search/     mappings / opensearch / ranking(RRF) / hybrid
  workers/    BackgroundTasks 流水线与 reindex
migrations/   Alembic
infra/        依赖服务 docker-compose（PG/OpenSearch/MinIO/Embedding）
scripts/      create_index / reindex / purge_deleted / healthcheck / acceptance / bulk_ingest / eval
tests/        单元测试
```

## 9. 边界（明确不做）

前端（另仓库）、用户系统、多租户、内置 Agent、本地 LLM、OCR、多模态检索、Citation Graph、
Redis/Celery。这些属于 plan 的 P1/P2，接口已为其预留（`rerank`、`paper_chunks_v2` 别名切换、
`ingestion_jobs` 状态机）。**注意**：两阶段精排、评测闭环、查询改写、作业重试、失败归因已在
P1 落地（见 §5 与 `progress.md`），不在"不做"之列。

## 10. 给 AI Agent 的构建说明

> **文档跟踪范围**：本仓库只跟踪 `README.md`。`plan.md`（权威需求）、`progress.md`（进度与实测
> 数字）、`MVP-SPEC.md`（接口摘要）、`AGENTS.md`（环境契约与执行纪律）、`docs/`、`evals/*.md`
> 都是**本地工作副本**（已在 `.gitignore`），克隆仓库看不到它们 —— 所以实现事实以**代码 + 本文**为准。

### 10.1 一次性构建

```bash
# 1) 依赖服务（PostgreSQL / OpenSearch / MinIO / Embedding）
cd infra && docker compose up -d && cd ..
#    Windows 开发机：wsl -e bash -lc "cd /mnt/<盘>/hermes/paperbox/infra && docker compose up -d"
#    数据落在 compose 旁的 ./data/<service>；服务器上给 POSTGRES_DATA_DIR 等变量写绝对路径

# 2) Python 环境（统一 uv；勿用 pip / 手建 venv）
uv sync                      # 读 pyproject.toml + uv.lock，建 .venv（Python 3.12）

# 3) 配置：两个 .env 别混
cp .env.example .env                  # 应用侧：DSN / URL / 密钥 / 开关
cp infra/.env.example infra/.env      # 容器侧：compose **只读** compose 同目录的 .env（根 .env 对容器无效）
#    infra/.env 里要填：POSTGRES_PASSWORD · MINIO_ROOT_USER/PASSWORD
#      可选：EMBEDDING_MODEL · RERANK_MODEL · RERANK_MAX_BATCH · ORT_THREADS · MAX_BATCH
#            · POSTGRES_DATA_DIR / OPENSEARCH_DATA_DIR / MINIO_DATA_DIR / EMBEDDING_MODELS_DIR
#            · PAPERBOX_BIND_IP · OPENSEARCH_ADMIN_PASSWORD

# 4) schema 与索引
uv run alembic upgrade head          # 9 张表（papers.fingerprint 是部分唯一索引）
uv run python scripts/create_index.py  # 建 paper_chunks_v2（CJK 分词）+ 别名 paper_chunks_current

# 5) 启动 / 自检 / 测试
uv run uvicorn app.main:app --host 0.0.0.0 --port 8077   # 监听由 PAPER_API_HOST / PAPER_API_PORT 决定
uv run python scripts/healthcheck.py     # 四依赖连通性
uv run pytest                            # 310 用例；必须全绿才继续
```

### 10.2 容器化构建（Linux 服务器）

```bash
docker build -t paperbox/api:0.1.0 .                 # 应用镜像（非 root 运行；.dockerignore 已裁剪上下文）
docker compose -f docker-compose.yml up -d --build    # 应用容器（依赖服务见 infra/）
# 容器内依赖寻址二选一：
#   A) host.docker.internal —— compose 已加 extra_hosts: ["host.docker.internal:host-gateway"]
#      （Linux 的 Docker 引擎不自带这个名字，不加则四依赖全不可达）
#   B) 与依赖加入同一 docker 网络，地址写服务名：postgres:5432 / opensearch:9200 / minio:9000 / embedding:8090
```

### 10.3 改代码前必读的硬性约束

1. **单 worker**：导入流水线走 FastAPI `BackgroundTasks`（进程内）⇒ 必须 `--workers 1`；要多副本先把导入改成外部队列。
2. **容器配置改 `infra/.env`**（根 `.env` 对容器无效）；改 `infra/embedding/server.py` 后必须
   `cd infra && docker compose build embedding && docker compose up -d embedding`（代码烤进镜像，只 `up -d` 会"环境变量生效但新代码不生效"）。
3. **精排超时必须放大**：多语言档实测 ≈0.4–0.47 s/候选、候选数 = `top_k × RERANK_CANDIDATES` ⇒ `top_k=10` 约 20s；
   `RERANK_TIMEOUT` 保持默认 10 会让精排**静默降级**（响应 `rerank.model=null`、`rerank_score=null`，日志 `timed out`）。
4. **两套限批别混用**：`MAX_BATCH`（`/embed`，默认 16，与 `EMBEDDING_BATCH_SIZE` 对齐）与 `RERANK_MAX_BATCH`（精排，多语言档必须 4）。
5. **索引读写走别名** `paper_chunks_current`；换分词器/embedding 模型必须新建索引再原子切别名，旧索引保留回滚（`scripts/create_index.py --migrate-from`）。
6. **密钥不进库**：`.env`、`infra/.env`、`data/`、`logs/` 均在 `.gitignore`。
7. **日志位置**：运行日志进 `logs/{codex,app,eval}/`，不要写仓库根目录。
8. **不要写自指统计**：不写"共 N 个 commit / 文件"，或写成「N（截至 `<sha>`；以 `git rev-list --count HEAD` 为准）」。
9. **连不上依赖先自检**：`curl` 一下服务再谈改代码；`scripts/healthcheck.py` 是四依赖一键体检。

### 10.4 代码地图与常用入口

- 路由：`app/api/`（health / papers / ingestion / jobs / search / search_logs）→ 业务：`app/services/`
  → 检索与精排：`app/search/`（mappings / opensearch / hybrid / ranking）→ 流水线：`app/workers/tasks.py`
- 解析与切块：`app/parsing/`（pdf / structure / chunking）；评测指标：`app/eval/metrics.py`
- 定标评测集：`evals/queries.jsonl` + `evals/labels.jsonl`（源头是 `evals/queries-spec.json`，
  用 `uv run python scripts/build_eval_set.py` 重新解析）；跑分：
  `uv run python scripts/eval.py --modes keyword,semantic,hybrid --rerank both --k 1,3,5,10`
- 语料批量导入：`uv run python scripts/bulk_ingest.py --dry-run` 先看计划，再去掉 `--dry-run` 真跑

