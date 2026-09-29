# paperbox

论文知识服务：**导入 → 解析 → 分块 → Embedding → 索引 → 混合检索 → REST API**。
无前端、无 Agent 逻辑，只提供事实与检索能力，供 Hermes 主 Agent 通过 HTTP 调用
（架构与需求见 `.hermes/plans/2026-09-10_215600-paperbox-master-plan.md`，实现规范见 `docs/architecture/MVP-SPEC.md`）。

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
| 论文列表（分页 / 按状态 / 标题搜索 / 按 venue、年份区间、paper_type、tag 过滤） | `GET /api/papers` |
| 三端一致性检查（PG / MinIO / OpenSearch 只读对账） | `GET /api/consistency` |
| 任务列表（`limit`/`offset` 分页 + `stage`/`paper_id` 过滤，响应回显窗口与 `total`） | `GET /api/jobs` |
| 导入队列深度（并发上限 / 在跑 / 排队） | `GET /api/jobs/queue` |
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
| Embedding | `http://127.0.0.1:8090` | `POST /embed`，模型 `intfloat/multilingual-e5-large`（1024 维，512 token 上限，`MAX_BATCH` 限批）；`POST /rerank` 交叉编码器精排（模型 `RERANK_MODEL`，单次候选上限 `RERANK_MAX_BATCH`）；`GET /health`、`GET /info` 均含 `rerank_model`，`/info` 另含 `max_batch` 与 `rerank_max_batch` || Embedding | `http://127.0.0.1:8090` | `POST /embed`，模型 `intfloat/multilingual-e5-large`（1024 维，512 token 上限，`MAX_BATCH` 限批）；`POST /rerank` 交叉编码器精排（模型 `RERANK_MODEL`，单次候选上限 `RERANK_MAX_BATCH`）；`GET /health`、`GET /info` 均含 `rerank_model`，`/info` 另含 `max_batch` 与 `rerank_max_batch` |
| Docling（可选，第二解析后端） | `http://192.168.31.53:8091`（fnOS NAS） | `POST /v1/convert/file`，`GET /health`、`GET /version`；镜像 `paperbox-docling-cpu:v1.35.0-formula`（公式模型 CodeFormulaV2 已烤进镜像）。**不在本机 WSL 里跑**：WSL 只剩 ~1.7 GiB 余量，公式密集论文会被 OOM-kill。本机保留 compose profile `local-docling` 作回滚：`cd infra && docker compose --profile local-docling up -d docling`（然后给 WSL `ufw allow 8091/tcp`）。地址写进 `.env` 的 `DOCLING_URL`；**留空 = 关闭 docling 后端**，客户端直接抛 `DoclingUnavailable` |

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

**注意单 worker**：导入流水线跑在进程内（由 `app/workers/queue.py` 的队列调度），因此应用要保持
`--workers 1`；要横向扩 worker/多副本，得先把导入改成外部队列（当前不做）。队列本身只控制**并发度**，
不改变这一点。详见 §3.2。

常用的 uv 命令：`uv sync`（对齐环境）、`uv add <pkg>`（加依赖）、`uv lock --upgrade`（升级并重锁）、`uv run <cmd>`（在项目环境里执行）。

### 3.2 导入队列与并发（`INGEST_CONCURRENCY`）

导入任务不直接开跑，而是先进**进程内 FIFO 队列**（`app/workers/queue.py`，无 Redis/Celery）：

- 同时最多跑 `INGEST_CONCURRENCY` 条流水线（**默认 2**）；多出来的上传停在 `stage=QUEUED` 等空位，
  先进先出。短时间连续上传 N 个文件 = 2 个在处理、N-2 个排队，不会一起压垮 embedding 服务。
- `GET /api/jobs/queue` 看队列深度：`{"started":true,"concurrency":2,"running":2,"queued":2,
  "queued_high":0,"queued_low":2,"running_job_ids":[...],"queued_job_ids":[...]}`。
  `queued_high`/`queued_low` 是等待中作业按优先级分类的计数（见 §3.3）。
- **优先级**：1 个文件的上传是**交互**优先级（有人等着答案），多文件上传是**批**优先级；
  交互作业插队到等待中的批作业之前，同类保持 FIFO。
- 上传/URL/重试/重建索引四条路径共用这一条队列，所以重试和 reindex 也受同一个上限约束。
- **进程重启不丢单**：启动时 `recover()` 把 `RECEIVED`/`QUEUED` 的作业重新入队；重启时正跑在中途的
  作业被标为 `FAILED` + `error_code=INTERRUPTED`（可 `POST /api/jobs/{id}/retry` 重跑），不会永远挂在那。
- **调大之前先想清楚**：本机实测 embedding 是瓶颈（e5-large、`ORT_THREADS=4`，约 **1 chunk/s**，
  400 token 的 chunk）。并发 2 时两条流水线抢同一批 ORT 线程，聚合吞吐反而略低于单条
  （实测 320 chunks / 401s ≈ 0.8 chunk/s）。`INGEST_CONCURRENCY` 主要买的是"上传即返回 + 不让 CPU 空转"，
  不是线性加速；本机建议保持 2，内存紧张时降到 1。

### 3.3 批量上传：三个入口（2026-09-19）

要导入一个**文件夹**或一个**压缩包**时用下面三个入口；三者共用同一套状态机（`ingestion_jobs`）与同一条队列，
**不新增表 / 迁移 / 阶段 / 错误码**。服务端是唯一的并发决策者，客户端**没有并发参数**。

| 入口 | 适用场景 | 传输量 | 说明 |
|------|----------|--------|------|
| `POST /api/papers/ingest/files` | 文件在**客户端** | 全量上传 | multipart 字段 `files` 可重复（1..`INGEST_MAX_FILES_PER_REQUEST`）；逐 part 流式写 staging，**边写边算 sha256** |
| `POST /api/papers/ingest/dir` | PDF 与 app **同机 / 同挂载卷** | **零传输** | 服务端自己遍历目录 + 预哈希判重，1000 文件≈秒级 |
| `POST /api/papers/ingest/compressed` | 已打成**一个 zip** | 压缩包大小 | 服务端安全解包后逐个入库；**只支持 zip** |

**判重**：内容命中库内已有论文 → 该文件记为 `duplicate`，**不产生新论文、不写 staging**
（`/files` 已写入的立即删除）。`/files` 无法在传输前判重（重复字节仍会被传一遍后丢弃），
同机场景请直接用 `/ingest/dir`（**有**传输前预哈希）。

**限流与错误码语义**（客户端义务：收到 `429` 按 `Retry-After` 退避重试）：

| 码 | 含义 | 触发条件 |
|----|------|----------|
| `429 + Retry-After: 2` | 服务端忙 | 在途 `/ingest/files` 请求 > `INGEST_UPLOAD_CONCURRENCY`；或**多文件**请求遇到处理积压 ≥ `INGEST_QUEUE_HIGH_WATERMARK`（单文件请求豁免——一个人等一个答案是 1 个作业的代价） |
| `422` | 请求/文件不合法 | 文件数 > `INGEST_MAX_FILES_PER_REQUEST`；压缩包超条目数/解压总量/压缩比；单文件端点收到非 PDF 或超 `INGEST_MAX_FILE_MB` |
| `413` | 请求体过大 | 单请求总字节 > `INGEST_MAX_REQUEST_MB`（此时**尚未**写 staging） |
| `415` | 格式不支持 | 压缩包不是 zip（7z/rar/tar 明确不做，请重新打包为 .zip） |
| `403` | 目录不在白名单 | `/ingest/dir` 的 root 在 realpath 归一化后落在 `INGEST_LOCAL_ROOTS` 之外（含 `..` 与符号链接/junction 逃逸） |
| `404` | 目录端点未启用 | `INGEST_LOCAL_ROOTS` 为空（**默认关闭**） |

**逐文件结果**：`/files` 与 `/compressed` 都返回逐文件/逐条目的结果数组
（`accepted` / `duplicate` / `rejected` + `error_code`），**一个坏文件不会让整个请求失败**。

**`/ingest/dir` 的安全边界**（本仓库唯一新增的文件系统访问面）：白名单 realpath 收敛、只读、
**不跟随符号链接与 Windows 目录 junction**（`os.walk(followlinks=False)` 仍会进入 junction，已显式剪除）、
跳过隐藏/临时文件；`dry_run=true` 只回清单与统计、不建作业。

**`/ingest/compressed` 的安全边界**：zip-slip（绝对路径 / 盘符路径 / `..` / 符号链接 / 设备文件逐条拒收，
且解出的目标必须落在解包目录内）、zip bomb 三重上限（条目数 / 解压总量 / 压缩比，均在建条目**之前**
从中央目录判定）、嵌套压缩包不递归解（计入 `entries_ignored`）、`.pdf` 扩展名 + `%PDF` 魔数双校验。

**配置**（仓库根 `.env`，全部应用侧；示例见 `.env.example`）：

| 键 | 默认 | 含义 |
|----|------|------|
| `INGEST_UPLOAD_CONCURRENCY` | 2 | 在途 `/ingest/files` 请求上限（超出 429） |
| `INGEST_QUEUE_HIGH_WATERMARK` | 50 | 队列深度阈值；达到后拒**多文件**请求（0 = 关闭该限制） |
| `INGEST_MAX_FILES_PER_REQUEST` | 20 | 单请求文件数上限 |
| `INGEST_MAX_REQUEST_MB` | 200 | 单请求总字节上限 |
| `INGEST_LOCAL_ROOTS` | 空 | 目录导入白名单根（`;`/`,` 分隔；**空 = 端点关闭**） |
| `INGEST_ARCHIVE_MAX_MB` | 500 | 压缩包本体上限 |
| `INGEST_ARCHIVE_MAX_FILES` | 2000 | 解包条目数上限 |
| `INGEST_ARCHIVE_MAX_UNCOMPRESSED_MB` | 5000 | 解压总量上限 |
| `INGEST_ARCHIVE_MAX_RATIO` | 100 | 压缩比上限（zip bomb） |
| `INGEST_ARCHIVE_TMP_DIR` | 空 | 临时解包目录（空 = 系统 temp） |
| `INGEST_ARCHIVE_TTL_HOURS` | 24 | 临时解包目录保留上限 |
| `INGEST_GC_INTERVAL_S` | 300 | 清理任务间隔（启动必跑一次） |

**临时副本与清理**：staging 对象在 `STORED` 检查点删除（判重命中立即删除）；压缩包解出的本地文件
（`cleanup_after`）在 `STORED` 后删除并剪掉空目录；`app/workers/housekeeping.py` 在启动时跑一次、
之后每 `INGEST_GC_INTERVAL_S` 清理孤儿/终态作业的 staging、过期解包目录与残留压缩包（**例外**：`STORED` 之前失败＝`stage=FAILED` 且 `paper_id IS NULL` 的行，自 `finished_at` 起保留 72h 供重试）——
**只删文件、不改作业状态、幂等**。

**客户端脚本**：`scripts/bulk_ingest_dir.py`（同机默认走 `/ingest/dir`；远端用 `--via-http` 走 `/ingest/files`，
默认每请求 1 个文件 + 429 退避；`--resume` 读上次报告跳过已完成）。

### 3.4 元数据：多来源、外部导入、手动编辑（2026-09-21）

架构权威是 `docs/architecture/metadata-architecture.md`；实测数字与坑见 `docs/progress/project.md` §17。

**三层模型**：论文（`papers`，唯一）+ 来源记录（`paper_sources`，一份外部数据/一次解析一行）+
字段声明（`paper_field_provenance`，谁在什么时候把哪个字段写成了什么）。标识符表
（`paper_identifiers`）作去重骨架，`venue` 与年份分离（`venues` + `venue_editions`）。

**合并规则（R2，只有两条）**：① 只填空；② 结构化来源（`import_file`/`ieee_api`/`arxiv_api`/
`crossref`/`pdf_embedded`/`manual`）可以覆盖 `pdf_heuristic`（首页启发式）。结构化来源之间**不比较
权威性**——冲突登记进 `paper_field_provenance`（`is_current=false`）并出现在复核清单里，由人决定。
`manual`（PATCH）不受 R2 约束。

**标识符阶梯**：`DOI > arXiv > 标题+首作者+年 > sha256`。主标识符决定 `papers.fingerprint`
（`doi:…` / `arxiv:…` / `title:…|作者|年` / `sha256:…`），改 DOI 会升级指纹。一个标识符只能属一篇
论文；**删除论文会释放它的标识符**（否则墓碑会永久占住 DOI）。

**主版本规则**：一篇论文的多个 PDF 里只有一个是主版本（`published_pdf > original > arxiv_pdf`），
只有主版本会被解析、切块、索引；其余照样入库登记（`is_primary=false`）但不解析。`GET /api/papers/{id}/file`
返回主版本。

**两种导入顺序都支持**：先 PDF 后元数据（摄取后导入补全），或先元数据后 PDF（导入未知记录建
`status=AWAITING_FILE` 的壳论文，PDF 到达时**复用同一 `paper_id`**）。

```bash
# 导入外部记录（默认 dry_run：只匹配并报告，不落库）
curl -X POST http://127.0.0.1:8077/api/metadata/import \
  -H "Authorization: Bearer ***" -F file=@ieee-export.json -F source_type=import_file
# -> {"total_records":1,"matched":1,"created_shell":0,"ambiguous":0,"unchanged":0,
#     "sources":[{"source_ref":"doi:10.1109/...","paper_id":"...","decision":"matched"}],
#     "conflicts":[],"dry_run":true}
# 真正落库：加 ?apply=true（或 dry_run=false）

# 复核清单（没匹配上的来源 + 已登记的字段冲突）
curl -H "Authorization: Bearer ***" 'http://127.0.0.1:8077/api/metadata/review?limit=20'
# 人工把一条来源挂到某篇论文上
curl -X POST http://127.0.0.1:8077/api/metadata/sources/<source_id>/attach \
  -H "Authorization: Bearer ***" -H 'Content-Type: application/json' -d '{"paper_id":"<uuid>"}'

# 看某篇论文的当前值 + 每字段来源与历史
curl -H "Authorization: Bearer ***" http://127.0.0.1:8077/api/papers/<paper_id>/metadata
# 手动改（decided_by='manual'，不受 R2 约束；未知字段在 rejected 里回显）
curl -X PATCH http://127.0.0.1:8077/api/papers/<paper_id>/metadata \
  -H "Authorization: Bearer ***" -H 'Content-Type: application/json' \
  -d '{"volume":"62","issue":"7","doi":"10.1109/JSSC.2015.2441234"}'
# 回滚某字段到历史主张（历史不删）
curl -X POST http://127.0.0.1:8077/api/papers/<paper_id>/metadata/rollback \
  -H "Authorization: Bearer ***" -H 'Content-Type: application/json' \
  -d '{"field":"volume","provenance_id":"<claim uuid>"}'
```

**脚本**：

```bash
uv run python scripts/import_metadata.py records.json --dry-run     # 或 --apply / --report out.json
uv run python scripts/backfill_metadata.py --dry-run                # 给历史论文补来源/声明/标识符/主版本
uv run python scripts/acceptance_metadata.py [--cleanup]            # 真机验收 7 项（需要 API 已启动）
```

**注意（已知行为，非缺陷）**：检索的过滤字段（venue/year/tags/doi/新元数据列）在 chunk 文档上，
`PATCH /metadata` 与导入只改 PostgreSQL；想让改动进入检索过滤，**改元数据用
`uv run python scripts/refresh_index_metadata.py`**（秒级、不重算向量），要连向量一起重算才用
`POST /api/papers/{id}/reindex`（≈1 chunk/s）。

### 3.5 三端一致性检查（2026-09-22）

PostgreSQL / MinIO / OpenSearch 存的是同一篇论文的三个投影，日常运维很容易漂移：删除清了一边、
另一边失败；reindex 中途被打断；有人手删了对象。**`GET /api/consistency`** 回答"库、对象、索引现在还
对不对得上"：

```bash
uv run python scripts/check_consistency.py            # 真机对账（有漂移返回非 0）
uv run python scripts/check_consistency.py --no-fail  # 只报告，永远返回 0
```

逐篇核对 `paper_files.object_key` ↔ MinIO 的 `papers/<id>/` 对象，以及每篇论文的 chunk 行数 ↔ 索引里
该 `paper_id` 的文档数；另外列出没有任何论文认领的对象与文档。问题码固定：
`missing_object` / `orphan_object` / `missing_chunks` / `missing_index` / `orphan_index` /
`chunk_count_mismatch` / `deleted_paper_residue`。

**只读，且永不抛**：某个 store 连不上只记进 `errors`，另外两端照样给出结果（与 `/health` 同一套纪律）。
软删论文按"已内联清理"预期（PG 行保留、文档与对象应已消失），有残留才算问题。
本机 2026-09-22 实测：**68 篇存活论文 / 68 个对象 / 2883 个文档 / 0 漂移**。

### 3.6 解析后端：docling（主）与 pypdf（降级）（2026-09-29）

PDF 解析有两个后端，输出**同一份 markdown 方言**（标题层级、`<!-- page-break -->` 页标记、表格/公式约定），
所以下游切块只认 markdown、不认后端：

| 后端 | 做什么 | 何时用 |
|------|--------|--------|
| `docling` | 远端 `docling-serve` 转换：版面模型给阅读顺序、表格结构、公式 LaTeX；页眉页脚与页号在客户端剔除 | 论文主路径（两栏、公式、表格） |
| `pypdf` | 进程内纯 Python：`extract_pages` + 分栏修复 + 启发式标题；无公式、表格只留占位 | docling 不可用时的降级；或刻意摸底对比 |

```bash
PARSER_BACKEND=pypdf    # 默认。pypdf 一档，零依赖
PARSER_BACKEND=docling  # 走远端 docling-serve（地址见 DOCLING_URL）
PARSER_CONCURRENCY=1    # 同时允许几个 docling 转换（进程内信号量）
PARSER_CACHE=true       # 解析产物存 MinIO，按 (paper_id, backend, 解析器版本) 复用
PARSER_MAX_PAGES=0      # 0=整篇；>0 只解析前 N 页（只作用于 docling，会记进 degraded_reason）
```

**降级永远是显式的**，绝不静默：

* docling 连不上 / 超时 / 返回非 JSON → 回落到 pypdf，`ParseBundle.degraded_reason` 写上原因，
  同时进 `paper_degradations` 账本（`stage=parsing`, `code=docling_unavailable`），
  `GET /api/papers/{id}/degradations` 能看到，`scripts/reindex.py --degraded` 能筛出来。
* docling 第一次带公式失败（缺模型、超时）→ 关掉公式重试一次，成功则记 `formulas=text`
  （公式退化为纯文本），不是整篇失败。
* pypdf 侧带 `no formula latex`（一定能变；再按文档实际情况加 `table structure`、`reading order not verified`）
  — 这是降级后端的能力边界，不是异常。

**解析产物缓存**（MinIO `papers/<id>/extracted/parsed/`）：命中要求 markdown 对象还在、
docling 服务端报的版本与当初一致（`GET /version`，毫秒级）、当初不是降级产物；
**部分解析（`--page-range` 或 `PARSER_MAX_PAGES>0`）既不读缓存也不写缓存** — 拿半篇冒充整篇比慢一点更糟。

**两条后端的真机对照**（`scripts/acceptance_parser.py`，只读、不写库，跑完自净）：

```bash
uv run python scripts/acceptance_parser.py \
  --pdf logs/eval/docling/corpus/1807.11311.pdf logs/eval/docling/corpus/smoke_sample.pdf \
  --mem-ssh fishsky@192.168.31.53:65422          # docling 在 NAS 上，用 SSH 采内存
uv run python scripts/acceptance_parser.py --paper-id <uuid>   # 额外探一次解析缓存
```

报告写到 `logs/eval/docling/acceptance-<时间戳>/report.json`（外加每篇的
`.docling.md` / `.pypdf.md` / `.diff.txt`）。2026-09-29 实测 5 份输入 × 2 后端：10 次运行
0 失败、0 次 docling 降级、页标记全部对齐（页数−1）、库总量前后不变；docling 端点峰值 2.20 GiB
（NAS 8 GiB 上限）。逐篇判断见 `docs/examine/解析双后端验收-20260929.md`。

## 4. 使用示例

```bash
API_KEY=$(grep PAPER_API_KEY .env | cut -d= -f2)

# 导入（arXiv 直达 PDF）
curl -X POST http://127.0.0.1:8077/api/papers/ingest \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"source_type":"url","source":"https://arxiv.org/pdf/1706.03762"}'
# -> {"job_id":"...","status":"RECEIVED","stage":"QUEUED"}

# 轮询任务（QUEUED -> DOWNLOADING -> STORED -> PARSING -> CHUNKING -> EMBEDDING -> INDEXING -> COMPLETED）
# 每个阶段转换都会 commit，因此轮询能看到真实中间态（不再只有 RECEIVED / COMPLETED）；
# QUEUED = 在队列里等流水线空位（受 INGEST_CONCURRENCY 限制，见 §3.2）；
# 失败时 stage/progress 保留在失败发生的那一步，error_code 给出结构化归因
# （NO_TEXT_LAYER / ENCRYPTED_PDF / CORRUPT_PDF / DOWNLOAD_FAILED / OVERSIZED /
#  UNSUPPORTED_TYPE / DUPLICATE_FINGERPRINT / PARSE_BACKEND_UNAVAILABLE / PARSE_FAILED /
#  EMBEDDING_FAILED / INDEX_FAILED /
#  STORAGE_FAILED / INTERRUPTED / INTERNAL）。
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
# -> 202，body 为重置后的作业（stage=QUEUED），继续轮询 GET /api/jobs/<job_id> 即可

# 任务列表：limit/offset 服务端分页 + stage/paper_id 过滤（2026-09-23）
# - total 永远是**过滤后**的总数（不是本页条数），所以一页就能算出「第 x / y 页」
# - 响应回显 limit/offset/stage，调用方不必自己记窗口
# - stage 取值限 RECEIVED/QUEUED/DOWNLOADING/STORED/PARSING/CHUNKING/EMBEDDING/INDEXING/COMPLETED/FAILED，
#   写错是 422（不是空列表——否则打错的阶段名看起来像「没有这种任务」）
curl "http://127.0.0.1:8077/api/jobs?limit=20&offset=40" -H "Authorization: Bearer $API_KEY"
# -> {"total":137,"limit":20,"offset":40,"stage":null,"jobs":[...]}
curl "http://127.0.0.1:8077/api/jobs?stage=FAILED&limit=50" -H "Authorization: Bearer $API_KEY"
# -> {"total":3,"limit":50,"offset":0,"stage":"FAILED","jobs":[...]}  ← 拿来挑要重试的作业

# 上传本地 PDF
curl -X POST http://127.0.0.1:8077/api/papers/ingest/file \
  -H "Authorization: Bearer $API_KEY" -F file=@paper.pdf

# 批量上传多个文件（同一请求内 1..20 个，字段名可重复；1 个=交互优先级，≥2=批优先级）
curl -X POST http://127.0.0.1:8077/api/papers/ingest/files \
  -H "Authorization: Bearer $API_KEY" \
  -F files=@a.pdf -F files=@b.pdf -F files=@c.pdf
# -> 202 {"request_id":"...","accepted":3,"duplicate":0,"rejected":0,
#         "results":[{"filename":"a.pdf","status":"accepted","job_id":"...","size_bytes":...}, ...]}
# 429 -> {"detail":"server busy: upload concurrency: 2 request(s) already in flight; retry after 2s"}
#        （读 Retry-After 头退避重试；多文件请求还会因积压被拒，单文件不会）

# 服务端目录导入（PDF 与 app 同机/同挂载卷时用；零传输）
# 需先设 INGEST_LOCAL_ROOTS=<白名单根>（分号分隔），否则端点返回 404；root 不在白名单 -> 403
curl -X POST http://127.0.0.1:8077/api/papers/ingest/dir \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"root":"D:/papers","glob":"**/*.pdf","recursive":true,"limit":2000,"dry_run":true}'
# -> 202 {"root":"...","matched":1024,"accepted":1000,"duplicate":20,"rejected":4,"skipped":0,"jobs":[...]}
# dry_run=true 只回清单与统计、不建任何作业；去掉它即为真实导入

# 压缩包导入（仅 zip；7z/rar/tar -> 415）
curl -X POST http://127.0.0.1:8077/api/papers/ingest/compressed \
  -H "Authorization: Bearer $API_KEY" -F file=@papers.zip
# -> 202 {"request_id":"...","entries_total":8,"entries_ignored":2,"entries_rejected":2,
#         "accepted":4,"duplicate":0,"rejected":0,"results":[...]}
# entries_ignored = 非 PDF / 嵌套压缩包；entries_rejected = zip-slip 或超限条目

# 客户端脚本（遍历文件夹 + 预筛 + 轮询 + JSON 报告 + --resume）
uv run python scripts/bulk_ingest_dir.py --root D:/papers --dry-run
uv run python scripts/bulk_ingest_dir.py --root D:/papers --limit 50
uv run python scripts/bulk_ingest_dir.py --root ./local --via-http --resume

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
- 过滤器：`year_from`、`year_to`、`authors`、`venue`、`doi`、`arxiv_id`、`tag`，以及元数据层带来的
  `venue_year`（会议/期刊**那一届**的年份——与论文自身的 `year` 是两回事，早录用/晚收录时两者不同）、
  `paper_type`、`identifier`（`<scheme>:<值>`，如 `ieee_article_number:7065247`、`doi:10.1109/...`；
  scheme 不认识直接 422，不会静默查空）、以及按 `papers_tags.kind` 分列的 `ieee_terms` / `author_terms` /
  `dynamic_index_terms` / `source_tags`（`tag` 仍是四个 kind 的并集）
- **过滤器读的是索引里的快照**（论文被索引时写进 chunk 文档，不是实时查 PostgreSQL）⇒ 改元数据
  （PATCH / 导入 / 合并）**不会自动改变检索过滤结果**。只改元数据（不动分词器与模型）用
  `uv run python scripts/refresh_index_metadata.py` 批量改写快照——**2883 个文档实测秒级**，不重算向量；
  `POST /api/papers/{id}/reindex` 同样能生效，但要把 chunk 重新 embedding（≈1 chunk/s，一篇论文几十秒）
- `rerank=true`（`POST /api/search`）：两阶段精排——先按 `top_k * RERANK_CANDIDATES` 扩大候选，再用交叉编码器（`RERANK_MODEL`）重排，取 `top_k * 2` 交给论文级聚合；服务不可用时自动降级为原顺序（`rerank_score` 为 `null`），不报错
- **精排模型与限批（`RERANK_MODEL` / `RERANK_MAX_BATCH`）**：交叉编码器的激活内存随 `(token × 候选数)` 增长，所以精排有独立上限，与 embedding 的 `MAX_BATCH` 解耦（共用一个旋钮要么撑爆精排、要么让正常导入吃 422）。本机实测（WSL 9GB，`ORT_THREADS=4`，文档截断 2000 字符）：`jinaai/jina-reranker-v2-base-multilingual` 加载占 1.9GB，单批 4 条峰值 ~2.4GB、8 条 ~3.3GB、**16 条 ~5.1GB**，速度 ~1.1–1.4 s/候选；`Xenova/ms-marco-MiniLM-L-6-v2` 16 条仅 ~0.7GB、0.09 s/候选。**本机现用多语言档：`RERANK_MODEL=jinaai/jina-reranker-v2-base-multilingual` + `RERANK_MAX_BATCH=4`**（换轻量档请把 `RERANK_MAX_BATCH` 一起调回 16）
- **精排超时必须跟着放大（`RERANK_TIMEOUT`）**：应用侧候选数 = `top_k × RERANK_CANDIDATES`（默认 5），精排后保留 `top_k × 2`。多语言档实测 ≈ **0.4–0.47 s/候选**（文档截断 2000 字符）⇒ `top_k=1` 约 2.3s、`top_k=10`（50 条候选）约 20s。默认 10 秒会让精排**静默降级**（响应里 `rerank.model=null`、`rerank_score=null`，日志 `rerank request failed ... {"error":"timed out"}`，结果仍是"能搜到但没重排"）→ 本机设 `RERANK_TIMEOUT=60`，覆盖到约 `top_k ≤ 28`；`top_k=50`（250 条候选 ≈100s）会超时降级，需要继续调大超时或调小 `RERANK_CANDIDATES`。实测端到端：`top_k=3` → 6.9s（精排 6.05s）、`top_k=10` → 20.4s（精排 19.7s），响应正常回报 `model` 与 `took_ms`
- **查询改写（可选开关，P1 I，默认关闭）**：`QUERY_REWRITE_ENABLED=true` 时，含 CJK 的查询先经 `POST {QUERY_REWRITE_URL}/chat/completions` 改写成英文检索式再检索；响应多出 `rewritten_query`（实际检索文本）与 `rewrite{enabled,applied,model,took_ms}`，`query` 始终返回原始值（不入日志表之外的任何替换）。只对含 CJK 的查询改写，纯英文查询零额外调用；LLM 不可达/超时自动降级为原查询（仍 200，`rewrite.applied=false`）。开启时 `QUERY_REWRITE_URL`/`QUERY_REWRITE_API_KEY`/`QUERY_REWRITE_MODEL` 必须齐备，否则启动即报错（不会静默失效）。实测收益（10 条中文定标查询）：HR@1 **0.30 → 0.90**、MRR 0.473 → 0.950（真实 LLM 改写，`evals/report-zh-llm-rewrite.md`；阈值细节与配置键见 `docs/progress/project.md` §12）

只用一个 embedding space：换模型时必须新建 `paper_chunks_v2` 并切别名，不要覆盖旧向量。当前生产索引正是 `paper_chunks_v2`（CJK 分词），别名 `paper_chunks_current` → v2。

## 6. 脚本

| 脚本 | 用途 |
|------|------|
| `scripts/create_index.py` | 幂等创建 `paper_chunks_v1` + 别名 `paper_chunks_current`；`--index/--alias` 可指定，`--migrate-from <old>` 服务端 `_reindex` 整批拷贝（**不重新 embedding**）后原子切别名，旧索引保留供回滚 |
| `scripts/reindex.py` | 全量/指定论文重建（`--missing` 只补没有 chunks 的论文） |
| `scripts/purge_deleted.py` | 清理已删论文遗留的索引文档与 MinIO 对象（`--dry-run` 可先预览）；`--hard` **连 PostgreSQL 行一起删**（chunks/files/identifiers/sources/provenance/authors/tags/jobs + 论文行，逐表打印计数；**不可逆**，安全网是 `backups/` 里的 `pg_dump`） |
| `scripts/check_consistency.py` | 三端（PG / MinIO / OpenSearch）只读对账：逐篇核对文件行↔对象、chunk 行↔文档，报出缺失索引/缺失 chunk/缺失文档/孤儿对象/孤儿文档/删除残留；`--no-fail` 只报告不返回非 0 |
| `scripts/refresh_index_metadata.py` | 批量改写**已索引文档的元数据快照**（不动 `embedding`/`text`，不重跑 embedding）：先 `PUT _mapping` 补新字段，再对每个 chunk 发 partial update；`--dry-run` / `--paper-id` / `--limit` / `--no-mapping` |
| `scripts/healthcheck.py` | 四个依赖 + 应用健康检查与文档数统计 |
| `scripts/acceptance.py` | 端到端验收：跑 plan §38 的 8 条 MVP 标准（真实导入/检索/鉴权） |
| `scripts/bulk_ingest.py` | 批量导入语料（`evals/arxiv_ids.txt`，逐条串行 + 轮询作业；`--resume` 跳过已入库，`--dry-run` 只清单） |
| `scripts/bulk_ingest_dir.py` | 导入**一个文件夹**：默认走 `/ingest/dir`（同机零传输），`--via-http` 改走 `/ingest/files`（每请求 1 个文件 + 429 退避）；`--glob/--limit/--no-recursive` 筛文件，`--resume` 读上次报告跳过已完成，`--dry-run` 只清单 |
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

当前测试（`uv run pytest` 共 **854** 个用例，1 skipped：文件符号链接需开发者模式；以实际输出为准）：

| 文件 | 覆盖 |
|------|------|
| `tests/test_parsing.py` | PDF 抽取、section 识别、分块（不跨 section、token 上限、overlap、无空洞） |
| `tests/test_aggregation.py` | 论文级聚合与 evidence 选择 |
| `tests/test_filters.py` | 过滤器构造（year/authors/venue/doi/arxiv_id/tag + venue_year/paper_type/identifier/四个 tag kind） |
| `tests/test_rrf.py` | RRF 融合排序 |
| `tests/test_deletion.py` | 删除清理顺序、失败中止与可重试 |
| `tests/test_fingerprint_release.py` | 删除即释放指纹（部分唯一索引） |
| `tests/test_job_progress.py` | 阶段进度落库：中间态对其它会话可见、失败保留失败阶段 |
| `tests/test_job_retry.py` | 手动重试：原子 FAILED→RECEIVED 认领（防重复触发）、按 paper_id 路由 reindex/整体重跑、成功清错误字段、再失败落新归因 |
| `tests/test_search_log.py` | 检索日志：结果压缩/截断、写入降级、行序列化 |
| `tests/test_failure_classification.py` | 失败归因：14 个 error_code + 无文本层 PDF |
| `tests/test_ingest_queue.py` | 队列并发上限、FIFO、重启恢复（`mark_queued`/`recover_jobs`） |
| `tests/test_queue_priority.py` | 优先级：交互式插队、同类 FIFO、`queued_high/low`、`depth()` |
| `tests/test_upload_admission.py` | 上传准入：在途上限（含多线程竞争）、水位谓词、快照 |
| `tests/test_upload_stream.py` | 流式上传：边传边算 sha256、不整块读内存、失败包装 |
| `tests/test_local_source.py` | `local_path` 源：直传 papers 键、缺文件/超限/非 PDF 归因、`cleanup_after` 删文件+剪目录、payload 校验 |
| `tests/test_ingest_files.py` | `/ingest/files`：单/多文件、逐 part 失败隔离、判重不产新论文、422/413、429+Retry-After、水位放行单文件 |
| `tests/test_ingest_file.py` | 旧 `/ingest/file` 契约回归（响应形状、422、429、判重、staging 键） |
| `tests/test_ingest_dir.py` | `/ingest/dir`：白名单 403/404、`..` 与符号链接/junction 逃逸、dry_run、glob/limit、隐藏与临时文件跳过 |
| `tests/test_ingest_compressed.py` | 压缩包：zip-slip（`..`/绝对/盘符/符号链接/设备条目）、zip bomb 三重上限、嵌套不递归、非 zip 415、临时目录清理 |
| `tests/test_stored_cleanup.py` | STORED 后删 staging 与解包文件；STORED 之前失败保留 staging（可重试） |
| `tests/test_upload_gc.py` | housekeeping：孤儿/终态 staging、**`STORED` 之前失败的行保留 72h 供重试 / 超期回收**、过期解包目录、残留压缩包、幂等、不改作业行、周期任务生命周期 |
| `tests/test_bulk_ingest_dir.py` | 客户端脚本纯函数：预筛、清单、glob、429 退避、`--resume`、报告计数 |
| `tests/test_consistency.py` | 三端一致性：缺失对象/孤儿对象/缺失索引/缺失 chunk/孤儿文档/删除残留/坏 store（一个挂了另两个照样答）/未传 client |

| `tests/test_jobs_api.py` | `GET /api/jobs` 的 offset 分页（新→旧、越界为空、负值 422）与 `stage` 过滤（total 随过滤变、未知 stage 422、十个阶段全放行、与 paper_id 叠加）、窗口回显 |
| `tests/test_index_snapshot.py` | 索引映射与 chunk 文档形状快照：新字段类型、tag 按 kind 分列、identifiers 形状、`embedding` 仍是 1024 维 knn |
| `tests/test_refresh_index_metadata.py` | 快照刷新：每个 chunk 一条 partial update（不含 embedding/text）、先 mapping 后文档、跳过软删与无 chunk 论文、`--dry-run`/`--paper-id`/`--no-mapping` |
| `tests/test_paper_list_filters.py` | `GET /api/papers` 过滤（venue/年份区间/paper_type/tag）+ `PaperOut` 新列序列化 |
| `tests/test_purge_deleted.py` | `purge_deleted.py`：默认只对账不删 PG 行、`--hard` 逐表删净 |

**指纹与去重**：`papers.fingerprint` 按 `DOI > arXiv > 归一化标题+首作者+年 > sha256` 生成（`app/services/paper_service.py`）。导入时先以 `sha256` 占位，解析出元数据后**重算并落库**；若与另一篇存活论文撞指纹，则清理本次 chunks/索引/对象、软删本论文，作业以 `completed` + `duplicate=true` 指向既有论文结束（`app/workers/tasks.py`）。

**未覆盖（尚无单测）**：元数据规整（metadata normalization）。

失败作业的 `error_code` 取值固定为：`NO_TEXT_LAYER`、`ENCRYPTED_PDF`、`CORRUPT_PDF`、
`DOWNLOAD_FAILED`、`OVERSIZED`、`UNSUPPORTED_TYPE`、`DUPLICATE_FINGERPRINT`、
`PARSE_BACKEND_UNAVAILABLE`、`PARSE_FAILED`、`EMBEDDING_FAILED`、`INDEX_FAILED`、
`STORAGE_FAILED`、`INTERRUPTED`、`INTERNAL`（见 `app/core/errors.py`）。

其中 `INTERRUPTED` 由队列的启动恢复盖章（进程重启时正在跑的作业），
`PARSE_BACKEND_UNAVAILABLE` / `PARSE_FAILED` 只在**明确要求 docling 且不允许降级**时才出现
（探针、以及将来的 strict-parse 调用方）—— 正常流水线遇到 docling 不可达是**降级到 pypdf** 并记
`degraded_reason` + `paper_degradations` 一行，不是失败（见 §3.6）。

## 8. 目录结构

```
app/
  api/        health / papers / ingestion / jobs / search / search_logs / metadata / consistency 路由
  core/       config(pydantic-settings) logging security(Bearer)
  db/         SQLAlchemy 2.x models（14 张表，含 T7.3 的 paper_degradations；另有 authors.normalized_name 唯一约束）+ session
  schemas/    Pydantic 请求/响应
  services/   paper / ingestion / embedding / metadata / object_storage / search /
              consistency_service（三端只读对账）
              upload_admission（上传准入）· local_scan（目录导入）· archive_service（zip 解包与清理）
  parsing/    pdf 抽取、section 识别、分块
  search/     mappings / opensearch / snapshot（元数据快照单一来源） / ranking(RRF) / hybrid
  workers/    tasks.py（流水线 + reindex）· queue.py（优先级队列）· housekeeping.py（GC）
migrations/   Alembic
infra/        依赖服务 docker-compose（PG/OpenSearch/MinIO/Embedding）
scripts/      create_index / reindex / purge_deleted / check_consistency / refresh_index_metadata /
              healthcheck / acceptance / bulk_ingest / bulk_ingest_dir / eval
tests/        单元测试
```

## 9. 边界（明确不做）

前端（另仓库）、用户系统、多租户、内置 Agent、本地 LLM、OCR、多模态检索、Citation Graph、
Redis/Celery；**压缩包只支持 zip**（7z/rar/tar 不做：7z 无本机二进制、rar 需外部工具，
上传即 415）；presign 直传（客户端直传 MinIO）不做，上传一律走 proxy；
`/ingest/dir` 只在 PDF 与 app 同机/同挂载卷时可用（容器化部署需挂卷 + 配白名单）。
这些属于 plan 的 P1/P2，接口已为其预留（`rerank`、`paper_chunks_v2` 别名切换、
`ingestion_jobs` 状态机）。**注意**：两阶段精排、评测闭环、查询改写、作业重试、失败归因已在
P1 落地（见 §5 与 `docs/progress/project.md`），不在"不做"之列。

## 10. 给 AI Agent 的构建说明

> **文档跟踪范围**：本仓库只跟踪 `README.md`。`AGENTS.md`（环境契约与执行纪律）、`.hermes/plans/`（各阶段 plan，
> 权威需求是 `2026-09-10_215600-paperbox-master-plan.md`）、`docs/` 下的四类文档 —— `architecture/`（结构文档，
> 含 `MVP-SPEC.md` 接口摘要）、`progress/`（`project.md` 进度与实测数字、`parser.md` 解析线）、`examine/`（审查报告）、
> `old/`（过时文档）—— 以及 `evals/*.md`
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
uv run alembic upgrade head          # 14 张表（papers.fingerprint 是部分唯一索引；T7.3 新增 paper_degradations）
uv run python scripts/create_index.py  # 建 paper_chunks_v2（CJK 分词）+ 别名 paper_chunks_current

# 5) 启动 / 自检 / 测试
uv run uvicorn app.main:app --host 0.0.0.0 --port 8077   # 监听由 PAPER_API_HOST / PAPER_API_PORT 决定
uv run python scripts/healthcheck.py     # 四依赖连通性
uv run pytest                            # 854 用例；必须全绿才继续
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

1. **单 worker**：导入流水线跑在**进程内队列**（`app/workers/queue.py`，`INGEST_CONCURRENCY` 默认 2，
   超出停在 `stage=QUEUED`；不是 FastAPI `BackgroundTasks`）⇒ 必须 `--workers 1`；要多副本先把导入改成外部队列。
2. **容器配置改 `infra/.env`**（根 `.env` 对容器无效）；改 `infra/embedding/server.py` 后必须
   `cd infra && docker compose build embedding && docker compose up -d embedding`（代码烤进镜像，只 `up -d` 会"环境变量生效但新代码不生效"）。
3. **精排超时必须放大**：多语言档实测 ≈0.4–0.47 s/候选、候选数 = `top_k × RERANK_CANDIDATES` ⇒ `top_k=10` 约 20s；
   `RERANK_TIMEOUT` 保持默认 10 会让精排**静默降级**（响应 `rerank.model=null`、`rerank_score=null`，日志 `timed out`）。
4. **两套限批别混用**：`MAX_BATCH`（`/embed`，默认 16，与 `EMBEDDING_BATCH_SIZE` 对齐）与 `RERANK_MAX_BATCH`（精排，多语言档必须 4）。
5. **索引读写走别名** `paper_chunks_current`；换分词器/embedding 模型必须新建索引再原子切别名，旧索引保留回滚（`scripts/create_index.py --migrate-from`）。
   给**活索引加新字段**是允许的（`PUT _mapping`，见 `app/search/opensearch.py::update_mapping` 与
   `scripts/refresh_index_metadata.py`），但**必须赶在第一个带该字段的文档之前**——否则 `dynamic: true`
   会先把它映成 `text`（`pages`、`paper_type` 这类想按 keyword 过滤的字段就废了）；改已有字段类型仍然只能新建索引。
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

