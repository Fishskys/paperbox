# paperbox 用户手册

本手册覆盖**部署、配置、接口与排障**四件事，对应版本 **0.2.0**。
只想先跑起来看效果，读 [README](README.md) 的「快速开始」即可；本文是它的展开版。

- 第 1 章 详细部署教程：应用配置 + 四个容器的配置（全部以表格给出：变量名 / 作用 / 默认值 / 可选值）
- 第 2 章 API：全部接口的说明、端点与参数
- 第 3 章 常见问题：各种状态码与错误符号的含义，以及使用过程中会遇到的问题

> 约定：文中的「默认值」指**仓库自带示例配置**（`.env.example` / `infra/.env.example`）里的值。
> 仓库里有两份配置文件，**它们的作用域完全不同**：
> - 根 `.env` —— 应用进程读的配置（复制自根 `.env.example`）
> - `infra/.env` —— Docker 容器读的配置（复制自 `infra/.env.example`，docker compose 只读 compose 文件同目录的 `.env`）
>
> 两份都要复制，且**凭据必须成对一致**（PostgreSQL 口令、MinIO 用户名/口令在两边各出现一次）。

---

## 1. 详细部署教程

### 1.1 部署前准备

| 项 | 要求 | 说明 |
|---|---|---|
| Docker + Docker Compose | 必需 | 跑 PostgreSQL / OpenSearch / MinIO / Embedding 四个容器 |
| [uv](https://docs.astral.sh/uv/) | 必需 | 管理 Python 环境（`uv sync` 按锁文件建 `.venv`），不要用 `pip install` |
| 系统内存 | ≥ 8 GB（推荐 16 GB） | 四个容器常驻约 6–7 GB（embedding 冷启动后 ~4 GB、OpenSearch ~1.3 GB） |
| 磁盘 | ≥ 10 GB | 含容器镜像（OpenSearch ~1 GB、embedding 镜像 ~1 GB）与数据目录 |
| 可选：docling-serve | 非必需 | 第二解析后端，用来更好地处理双栏/表格论文；不部署则自动降级为内置 pypdf 解析 |

### 1.2 部署步骤（默认配置，无需改任何配置项）

```bash
# 1) 取代码
git clone https://github.com/Fishskys/paperbox.git
cd paperbox

# 2) 复制两份配置模板（模板值即可直接使用）
cp infra/.env.example infra/.env     # 容器配置
cp .env.example .env                 # 应用配置

# 3) 起四个依赖容器
cd infra && docker compose up -d && cd ..

# 4) Python 环境（按 uv.lock 建 .venv）
uv sync

# 5) 建数据库表结构与检索索引（两步都幂等，可重复执行）
uv run alembic upgrade head
uv run python scripts/create_index.py

# 6) 启动 API
uv run uvicorn app.main:app --host 0.0.0.0 --port 8077
```

每步的验证方式：

| 步骤 | 如何确认成功 |
|---|---|
| 3) 容器 | `docker compose ps` 四个服务都 `healthy`；`curl http://127.0.0.1:9200`、`curl http://127.0.0.1:8090/health` 有响应 |
| 5) 建表/索引 | `uv run python scripts/create_index.py` 回显索引名、字段与分词器；重复执行不报错 |
| 6) 启动 | 日志出现 `paperbox 0.2.0 starting`；`curl http://127.0.0.1:8077/health` 四个依赖都是 `ok` |

一键自检（推荐）：

```bash
uv run python scripts/healthcheck.py
```

它会逐项检查四个依赖、应用健康接口、索引文档数与检索管道，最后打印 `RESULT: all dependencies reachable`。

> **Windows 开发机**：依赖容器跑在 WSL2 里，第 3 步换成
> `wsl -e bash -lc "cd /mnt/<盘>/.../paperbox/infra && docker compose up -d"`；
> 若从 Windows 访问容器端口不通，需要在 WSL 里放行端口（`wsl -e -u root bash -lc "ufw allow 9200/tcp"`，9000/8090/5432 同理）。
> 地址一律写 `127.0.0.1`，不要写 `localhost`（见 §3.5 Q8）。

### 1.3 应用配置（根 `.env`）

#### 1.3.1 数据库 / 检索库 / 对象存储

| 变量名 | 作用 | 默认值 | 可选值 / 说明 |
|---|---|---|---|
| `POSTGRES_HOST` | PostgreSQL 主机 | `127.0.0.1` | 任意主机名/IP；容器化时用 `host.docker.internal` 或服务名 |
| `POSTGRES_PORT` | PostgreSQL 端口 | `5432` | 1–65535 |
| `POSTGRES_USER` | 数据库用户 | `postgres` | 需与容器初始化时的用户一致 |
| `POSTGRES_PASSWORD` | 数据库口令 | `change-me-postgres` | **必须与 `infra/.env` 的 `POSTGRES_PASSWORD` 相同** |
| `POSTGRES_DB` | 数据库名 | `paperbox` | |
| `POSTGRES_DSN` | 完整 SQLAlchemy 连接串 | `postgresql+psycopg://postgres:change-me-postgres@127.0.0.1:5432/paperbox` | **权威值**，设置后覆盖上面 5 项（Alembic 也用它） |
| `OPENSEARCH_URL` | 检索库地址 | `http://127.0.0.1:9200` | 带 scheme；本机部署无鉴权、无 TLS |
| `OPENSEARCH_INDEX` | 物理索引名 | `paper_chunks_v3` | 换分词器/向量维度时必须新建索引再切别名 |
| `OPENSEARCH_ALIAS` | 读写别名 | `paper_chunks_current` | 应用只读写别名，索引重建对调用方无感 |
| `MINIO_ENDPOINT` | 对象存储地址 | `127.0.0.1:9000` | `host:port`，**不带** `http://` |
| `MINIO_ACCESS_KEY` | 对象存储用户名 | `paperbox` | **必须与 `infra/.env` 的 `MINIO_ROOT_USER` 相同** |
| `MINIO_SECRET_KEY` | 对象存储口令 | `change-me-minio` | **必须与 `infra/.env` 的 `MINIO_ROOT_PASSWORD` 相同** |
| `MINIO_SECURE` | 是否走 HTTPS | `false` | `true` / `false` |
| `MINIO_BUCKET` | 存储桶名 | `paperbox` | 不存在时会自动创建 |

#### 1.3.2 向量与精排

| 变量名 | 作用 | 默认值 | 可选值 / 说明 |
|---|---|---|---|
| `EMBEDDING_URL` | 向量/精排推理服务地址 | `http://127.0.0.1:8090` | 与容器 `embedding` 的端口一致 |
| `EMBEDDING_MODEL` | 向量模型名 | `intfloat/multilingual-e5-large` | 只作为记录与溯源（真正加载的模型由容器侧决定） |
| `EMBEDDING_DIMENSION` | 向量维度 | `1024` | 必须与索引 `knn_vector.dimension` 一致 |
| `EMBEDDING_BATCH_SIZE` | 单次 `/embed` 的文本数 | `16` | **必须与容器 `MAX_BATCH` 相同**（超批 422） |
| `EMBEDDING_TIMEOUT` | 单批 HTTP 超时（秒） | `300` | 必须大于最坏排队时间；容器把推理串在一条 FIFO 队列后 |
| `EMBEDDING_MAX_RETRIES` | 每批额外重试次数 | `2` | 退避 `0.5 × 2^n` 秒 |
| `RERANK_ENABLED` | 是否具备精排能力 | `true` | 设为 `false` 时精排请求直接跳过（`rerank=true` 也无效） |
| `RERANK_MODEL` | 精排（交叉编码器）模型名 | `temsa/mmarco-mMiniLMv2-L12-H384-v1-onnx-cpu-qint8` | 可换内置档：`jinaai/jina-reranker-v2-base-multilingual`（中文优先、更重）、`Xenova/ms-marco-MiniLM-L-6-v2`（英文轻量） |
| `RERANK_MODEL_FILE` | 非内置模型的仓库内 ONNX 文件 | `model.onnx` | 内置档请注释掉；多数导出在 `onnx/model.onnx`，量化导出常在仓库根 |
| `RERANK_MAX_BATCH` | 单次精排的候选上限 | `4` | 交叉编码器内存随「token × 候选数」增长：多语言/int8 档用 `4`，轻量英文档可回 `16` |
| `RERANK_URL` | 精排服务地址 | `http://127.0.0.1:8090` | 与 `EMBEDDING_URL` 同服务 |
| `RERANK_TIMEOUT` | 精排请求超时（秒） | `10` | 候选数 = `top_k × RERANK_CANDIDATES`；**设小了会让精排静默降级**（多语言档建议 `60`） |
| `RERANK_CANDIDATES` | 精排候选过取倍数 | `5` | 合法值 ≥1；候选池 = `top_k × 此值`，同时决定 native 路径每篇进精排的块数上限 |

#### 1.3.3 检索行为

| 变量名 | 作用 | 默认值 | 可选值 / 说明 |
|---|---|---|---|
| `SEARCH_BACKEND` | `hybrid` 模式的融合后端 | `native` | `native`（引擎侧融合 + 按论文折叠）/ `python`（应用侧双路融合，基线与降级路径）；单次请求可用请求体 `backend` 覆盖，只影响 `mode=hybrid` |
| `RRF_KEYWORD_WEIGHT` | 关键词腿在融合中的权重 | `1.0` | `0` 即废掉该腿；实验结论是保留等权 |
| `RRF_SEMANTIC_WEIGHT` | 向量腿在融合中的权重 | `1.0` | 同上 |
| `QUERY_REWRITE_ENABLED` | 查询改写总开关 | `false` | `true` 时必须同时配好 `URL` / `MODEL` / `API_KEY`，否则**启动即报错** |
| `QUERY_REWRITE_URL` | LLM 服务基址（OpenAI 兼容） | 空 | 例 `https://api.example.com/v1` |
| `QUERY_REWRITE_API_KEY` | LLM 凭证 | 空 | 只放 `.env`，不要提交 |
| `QUERY_REWRITE_MODEL` | 请求体 `model` | 空 | 例 `deepseek-flash` |
| `QUERY_REWRITE_TIMEOUT` | 单次改写超时（秒） | `10` | 超时自动退回原查询（仍 200） |
| `QUERY_REWRITE_MAX_CHARS` | 触发改写的输入长度上限 / 输出截断上限 | `300` | 必须为正 |
| `QUERY_REWRITE_MAX_TOKENS` | 单次改写 token 预算 | `512` | **别调小**：推理模型先耗 token 在隐藏推理上，`64` 会得到空答案 |
| `QUERY_REWRITE_TARGET_LANGUAGE` | 目标语言 | `en` | 当前无代码读取（预留） |
| `SEARCH_LOG_ENABLED` | 是否记录检索日志 | `true` | 关掉即不写检索日志表 |
| `SEARCH_LOG_RESULTS_LIMIT` | 每条日志最多记多少篇结果 | `20` | |

#### 1.3.4 导入与上传

| 变量名 | 作用 | 默认值 | 可选值 / 说明 |
|---|---|---|---|
| `INGEST_DOWNLOAD_TIMEOUT` | URL 导入的下载超时（秒） | `120` | |
| `INGEST_MAX_FILE_MB` | 单个 PDF 大小上限 | `100` | 超限 → 该文件 `rejected` / 单文件请求 422 |
| `INGEST_CONCURRENCY` | 同时运行的导入流水线数 | `2` | `>0`；多出来的上传停在 `QUEUED`；embedding 是瓶颈，本机别调大 |
| `INGEST_UPLOAD_CONCURRENCY` | 在途上传请求上限 | `2` | 超出返回 `429 + Retry-After: 2` |
| `INGEST_QUEUE_HIGH_WATERMARK` | 处理队列深度阈值 | `50` | 达到后**只拒多文件**请求（429）；`0` 关闭该限制 |
| `INGEST_MAX_FILES_PER_REQUEST` | 单请求文件数上限 | `20` | 超出 422 |
| `INGEST_MAX_REQUEST_MB` | 单请求总字节上限 | `200` | 超出 413（此时尚未写 staging） |
| `INGEST_LOCAL_ROOTS` | 目录导入白名单根，`;` 或 `,` 分隔 | 空 | **空 = `/api/papers/ingest/dir` 关闭（404）**；越界 403 |
| `INGEST_ARCHIVE_MAX_MB` | zip 压缩包本体上限 | `500` | 超出 422 |
| `INGEST_ARCHIVE_MAX_FILES` | 解包条目数上限 | `2000` | zip bomb 防护 1/3 |
| `INGEST_ARCHIVE_MAX_UNCOMPRESSED_MB` | 解压总量上限 | `5000` | 防护 2/3 |
| `INGEST_ARCHIVE_MAX_RATIO` | 压缩比上限 | `100` | 防护 3/3；`0` 关闭该限制 |
| `INGEST_ARCHIVE_TMP_DIR` | 临时解包目录 | 空（系统 temp） | |
| `INGEST_ARCHIVE_TTL_HOURS` | 解包目录保留时长（小时） | `24` | 由清理任务回收 |
| `INGEST_GC_INTERVAL_S` | 清理任务间隔（秒） | `300` | 启动时必跑一次 |

#### 1.3.5 解析

| 变量名 | 作用 | 默认值 | 可选值 / 说明 |
|---|---|---|---|
| `PARSER_BACKEND` | 解析后端 | `docling` | `docling` / `pypdf`；docling 不可达时**自动降级**为 pypdf 并写降级账本 |
| `PARSER_CONCURRENCY` | 并发解析数 | `1` | `>0`；docling 是 CPU 密集型，保持 1 |
| `PARSER_CACHE` | 解析产物是否缓存到对象存储 | `true` | `true`/`false`；命中即重放（不重复调用解析服务） |
| `PARSER_MAX_PAGES` | 只解析前 N 页 | `0`（整篇） | `>0` 时仅作用于 docling，且该次解析**不进缓存**，降级账本记 `pagination_truncated` |
| `DOCLING_URL` | docling-serve 地址 | 空 | **空 = 关闭 docling 后端**（立即降级 pypdf，不会等待超时） |
| `DOCLING_TIMEOUT` | 客户端读超时（秒） | `660` | **必须大于** `DOCLING_DOCUMENT_TIMEOUT` |
| `DOCLING_DOCUMENT_TIMEOUT` | 服务端单文档超时（秒） | `600` | 服务端默认无期限，会让一篇论文长时间占满 CPU |
| `DOCLING_MAX_RETRIES` | 瞬时失败重试次数 | `1` | 只对 5xx/超时/空 body 重试；4xx 不重试 |
| `DOCLING_OCR` | 是否开启 OCR | `false` | 电子版 PDF 无需 OCR；开启后吞吐显著下降 |
| `DOCLING_TABLE_MODE` | 表格识别模式 | `accurate` | `accurate` / `fast` |
| `DOCLING_FORMULA_ENRICHMENT` | 公式转 LaTeX | `false` | 最贵的一项（数倍耗时）；开启需同时配 preset 且镜像含公式模型；**该值属于解析缓存身份** |
| `DOCLING_FORMULA_PRESET` | 公式模型预设名 | `codeformulav2` | 公式开启时必填，否则服务端 404 |
| `DOCLING_PAGE_BREAK` | 页分隔标记字面量 | `<!-- page-break -->` | 两侧使用的同一份方言，数量恒为「页数 − 1」 |
| `DOCLING_IMAGE_TAG` | 镜像 tag 兜底版本 | `paperbox-docling-cpu:v1.35.0-formula` | 取不到服务端版本时写进产物元数据 |

#### 1.3.6 分块

| 变量名 | 作用 | 默认值 | 可选值 / 说明 |
|---|---|---|---|
| `CHUNK_MODE` | 切块策略 | `length` | `length`（按 token 预算）/ `semantic`（按句子相似度低谷切） |
| `CHUNK_SEMANTIC_THRESHOLD` | 语义模式的低谷阈值（余弦） | `0.80` | 仅 `CHUNK_MODE=semantic` 生效 |
| `CHUNK_SEMANTIC_MIN_TOKENS` | 低谷处允许切块的最小累积 token | `200` | 仅语义模式生效，避免切出碎片 |

#### 1.3.7 服务与日志

| 变量名 | 作用 | 默认值 | 可选值 / 说明 |
|---|---|---|---|
| `PAPER_API_KEY` | API 鉴权密钥（Bearer） | `change-me` | **部署务必改掉**；改了要重启应用 |
| `PAPER_API_HOST` | 监听地址 | `0.0.0.0` | 只对本机开放可写 `127.0.0.1` |
| `PAPER_API_PORT` | 监听端口 | `8077` | |
| `LOG_LEVEL` | 日志级别 | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

> **改完根 `.env` 必须重启应用**：配置在启动时读入，`--reload` 不会重读。

### 1.4 容器配置（`infra/.env`）

#### 1.4.1 四个容器总览

| 容器名 | 镜像 / 构建 | 端口（宿主→容器） | 数据目录变量 | 健康检查 | 起不来的典型原因 |
|---|---|---|---|---|---|
| `paperbox-postgres` | `postgres:15.2-alpine` | `5432` | `POSTGRES_DATA_DIR` | `pg_isready` | `POSTGRES_PASSWORD` 未设（必填） |
| `paperbox-opensearch` | `opensearchproject/opensearch:3.6.0` | `9200` | `OPENSEARCH_DATA_DIR` + `OPENSEARCH_BACKUP_DIR` | `_cluster/health` | 内存不足；改了 `path.repo` 未重建容器 |
| `paperbox-minio` | `minio/minio:RELEASE.2025-07-23T15-54-02Z-cpuv1` | `9000`（API）、`9001`（控制台） | `MINIO_DATA_DIR` | `/minio/health/live` | `MINIO_ROOT_USER/PASSWORD` 未设（必填） |
| `paperbox-embedding` | 本地构建 `infra/embedding`（`paperbox/embedding-server:0.1`） | `8090` | `EMBEDDING_MODELS_DIR` | `/health` | 首次启动要下载模型（慢）；批次/线程配置过大会 OOM |
| `paperbox-docling`（可选） | 本地构建 `infra/docling`（`paperbox-docling-cpu:v1.35.0-formula`） | `8091→5001` | `DOCLING_DATA_DIR` | `/health` | 只带 `--profile local-docling` 才启动；内存上限给小了会被 OOM-kill |

#### 1.4.2 凭据与端口

| 变量名 | 作用 | 默认值 | 可选值 / 说明 |
|---|---|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL 口令 | `change-me-postgres` | **必填**；要与根 `.env` 的 `POSTGRES_PASSWORD` / `POSTGRES_DSN` 一致 |
| `MINIO_ROOT_USER` | MinIO 用户名 | `paperbox` | **必填**；要与根 `.env` 的 `MINIO_ACCESS_KEY` 一致 |
| `MINIO_ROOT_PASSWORD` | MinIO 口令 | `change-me-minio` | **必填**；要与根 `.env` 的 `MINIO_SECRET_KEY` 一致 |
| `PAPERBOX_BIND_IP` | 依赖容器端口绑定地址 | `0.0.0.0` | 服务器上可设 `127.0.0.1`，只让本机应用访问依赖 |
| `OPENSEARCH_ADMIN_PASSWORD` | OpenSearch 初始管理员口令 | `ChangeMe-Initial-Admin-2026!` | 当前安全插件被禁用，**不参与校验**；将来启用时必须改成强口令 |

#### 1.4.3 数据目录（改这里才能搬家）

| 变量名 | 作用 | 默认值 | 可选值 / 说明 |
|---|---|---|---|
| `POSTGRES_DATA_DIR` | 数据库数据目录 | `./data/postgres` | 服务器建议绝对路径，如 `/srv/paperbox/data/postgres` |
| `OPENSEARCH_DATA_DIR` | 检索库数据目录 | `./data/opensearch` | |
| `OPENSEARCH_BACKUP_DIR` | 快照仓库落点 | `./data/opensearch-backups` | 挂到容器 `/mnt/backups`，与 `-Epath.repo` 一致 |
| `MINIO_DATA_DIR` | 对象存储数据目录 | `./data/minio` | |
| `EMBEDDING_MODELS_DIR` | 向量/精排模型缓存目录 | `./data/embedding-models` | 换机时可整体搬运，避免重新下载 |
| `DOCLING_DATA_DIR` | docling 缓存目录（可选容器） | `./data/docling` | 模型在镜像里，这里只放缓存 |

> 改数据目录后必须 `docker compose up -d --force-recreate <服务>`（**只 `restart` 不生效**，挂载在创建时就固定了），
> 且换目录前请先停容器并把旧目录整体复制过去。

#### 1.4.4 模型与资源（embedding 容器）

| 变量名 | 作用 | 默认值 | 可选值 / 说明 |
|---|---|---|---|
| `EMBEDDING_MODEL` | 向量模型 | `intfloat/multilingual-e5-large` | 1024 维、512 token 上限；换模型要新建索引 |
| `RERANK_MODEL` | 精排模型 | `temsa/mmarco-mMiniLMv2-L12-H384-v1-onnx-cpu-qint8` | 与根 `.env` 的 `RERANK_MODEL` 保持一致（否则响应里报的模型名与实际加载的不符） |
| `RERANK_MODEL_FILE` | 非内置模型的 ONNX 文件 | `model.onnx` | 内置档（jina / ms-marco）请注释掉 |
| `RERANK_MAX_BATCH` | 精排单次候选上限 | `4` | 内存随候选数增长；轻量英文档可回 `16` |
| `MAX_BATCH` | `/embed` 单次文本数上限 | `16` | **必须与根 `.env` 的 `EMBEDDING_BATCH_SIZE` 相同** |
| `ORT_THREADS` | 推理线程数 | `4` | 调大会和同机其他服务抢 CPU，并可能触发 OOM |
| `INFERENCE_WORKERS` | 推理队列工作线程数 | `1` | 保持 1：并发不再抢内存，削峰交给队列 |
| `INFERENCE_QUEUE_DEPTH` | 推理队列深度 | `512` | 队满**立即** `503 + Retry-After`；定值规矩见 §3.4 |

#### 1.4.5 可选：docling 解析容器

| 变量名 | 作用 | 默认值 | 可选值 / 说明 |
|---|---|---|---|
| `DOCLING_MEM_LIMIT` | 容器内存上限 | `3000m` | 同时作为 swap 上限（禁止用 swap，防止拖垮整机） |
| `DOCLING_CPUS` | CPU 配额 | `4` | 给太多会让同机其他容器饿死 |
| `BUILD_HTTP_PROXY` / `BUILD_HTTPS_PROXY` | 构建期下载公式模型用的代理 | 空 | 仅 `docker compose build docling` 时生效；能直连外网则留空 |

启动本机 docling（默认**不启动**，只在需要时按 profile 起）：

```bash
cd infra && docker compose --profile local-docling up -d docling
# 然后在根 .env 里把 DOCLING_URL 指过去，并重启应用
```

### 1.5 部署到服务器

两种形态，按需要选：

| 形态 | 做法 | 适用 |
|---|---|---|
| A. 应用直跑宿主（推荐） | 四个依赖容器跑 docker，应用用 `uv run uvicorn` 或 systemd 托管，依赖地址填 `127.0.0.1` | 单机部署、最省事 |
| B. 应用也容器化 | `docker compose -f docker-compose.yml up -d --build`（根目录那份 compose 只打包应用） | 要统一编排时 |

形态 B 的两个要点：依赖地址要么指向宿主网关 `host.docker.internal`（根 compose 已配 `extra_hosts`），
要么把两份 compose 加进同一张 external 网络、地址直接写服务名（`postgres:5432` / `opensearch:9200` / `minio:9000` / `embedding:8090`）。

**安全建议**：`infra/.env` 设 `PAPERBOX_BIND_IP=127.0.0.1`，只暴露应用端口（8077，或经反代走 443）；
`PAPER_API_KEY` 换成强口令；依赖服务不要直接暴露公网。

**备份与迁移**（三处要一起搬，别重算向量）：

| 数据 | 备份/迁移方式 |
|---|---|
| 元数据（PostgreSQL） | `pg_dump` / `pg_restore` |
| 原件与解析产物（MinIO） | `mc mirror`，或直接搬运 `MINIO_DATA_DIR` 目录 |
| 检索索引（OpenSearch） | 快照仓库（`uv run python scripts/setup_snapshots.py`）或 `scripts/create_index.py --migrate-from` 服务端复制 |

### 1.6 常用运维命令

| 命令 | 用途 |
|---|---|
| `uv run python scripts/healthcheck.py` | 依赖 + 应用 + 索引 + 检索管道一键自检 |
| `uv run python scripts/create_index.py` | 幂等创建索引与别名；`--migrate-from <旧索引>` 服务端复制文档 |
| `uv run python scripts/refresh_index_metadata.py` | 只刷新索引里的元数据快照（秒级，不重算向量） |
| `POST /api/papers/{paper_id}/reindex` | 重建某篇论文的切块与向量（较慢） |
| `uv run python scripts/check_consistency.py` | 三端（数据库/对象存储/索引）只读对账 |
| `uv run python scripts/setup_snapshots.py` | 建/核对快照仓库与定时快照策略；`--restore-check` 做恢复演练 |
| `uv run python scripts/purge_deleted.py --hard` | 清理已删论文的索引文档、对象与数据库行（不可逆） |
| `uv run python scripts/eval.py` | 对运行中的服务跑定标集评测（检索质量） |
| `uv run python scripts/bulk_ingest_dir.py <目录>` | 批量导入一个目录（同机零传输） |

---

## 2. API

### 2.1 通用约定

| 项 | 约定 |
|---|---|
| 基址 | `http://<host>:8077`（默认端口 8077） |
| 鉴权 | 除 `/`、`/health`、`/docs` 外，所有接口都要 `Authorization: Bearer <PAPER_API_KEY>` |
| 请求体 | JSON（`Content-Type: application/json`），上传类接口用 `multipart/form-data` |
| 交互式文档 | `GET /docs`（OpenAPI 3.1，可直接试调） |
| 错误体 | `{"detail": "..."}`（422 校验错误为 FastAPI 默认结构，含 `loc`/`msg`/`type`） |
| 幂等性 | 建索引、建快照、清理解包目录等运维接口可重复执行 |
| 长任务 | 导入类接口返回 `202` + `job_id`，进度用作业接口轮询 |

### 2.2 服务与自检

| 方法 | 端点 | 说明 | 参数 |
|---|---|---|---|
| GET | `/` | 服务名、版本、文档地址 | 无 |
| GET | `/health` | 应用与四个依赖（PG / OpenSearch / MinIO / Embedding）健康状态，免鉴权 | 无 |
| GET | `/api/consistency` | 三端只读对账：逐篇核对「文件记录 ↔ 对象存储对象」「切块记录 ↔ 索引文档」 | `limit`（默认 200，1–1000）、`parser_papers`（默认 `false`，为 `true` 时附上每个解析后端下的存活论文清单） |

### 2.3 导入（五个入口）

| 方法 | 端点 | 说明 | 参数 |
|---|---|---|---|
| POST | `/api/papers/ingest` | 按 URL 导入 PDF | body `source`（**必填**，HTTP(S) 的 PDF 地址）、`source_type`（默认 `url`） |
| POST | `/api/papers/ingest/file` | 上传单个 PDF（`/files` 的封装） | form `file`（**必填**） |
| POST | `/api/papers/ingest/files` | 一次上传多个 PDF（1–20 个） | form `files`（**必填**，可重复） |
| POST | `/api/papers/ingest/dir` | 导入服务器本机目录（**零传输**） | body `root`（**必填**，绝对路径，必须落在 `INGEST_LOCAL_ROOTS` 内）、`glob`（默认 `**/*.pdf`）、`recursive`（默认 `true`）、`limit`（默认 2000）、`dry_run`（默认 `false`，只回清单不建作业） |
| POST | `/api/papers/ingest/compressed` | 上传 zip 压缩包批量导入（**仅 zip**） | form `file`（**必填**） |

五个入口都返回 `202` + 作业信息；`/ingest/files` 逐文件返回 `accepted` / `duplicate` / `rejected`，互不影响。

```bash
# URL 导入
curl -X POST http://127.0.0.1:8077/api/papers/ingest \
  -H "Authorization: Bearer $PAPER_API_KEY" -H 'Content-Type: application/json' \
  -d '{"source": "https://arxiv.org/pdf/1706.03762"}'

# 多文件上传
curl -X POST http://127.0.0.1:8077/api/papers/ingest/files \
  -H "Authorization: Bearer $PAPER_API_KEY" \
  -F "files=@a.pdf" -F "files=@b.pdf"
```

### 2.4 论文

| 方法 | 端点 | 说明 | 参数 |
|---|---|---|---|
| GET | `/api/papers` | 论文列表（读数据库当前值，支持过滤与分页） | `limit`（默认 20）、`offset`（默认 0）、`status`、`q`（标题子串，不区分大小写）、`venue`（可重复）、`year_from`、`year_to`、`paper_type`（可重复）、`tag`（可重复） |
| GET | `/api/papers/{paper_id}` | 论文详情（含文件列表） | `paper_id`（路径，**必填**） |
| DELETE | `/api/papers/{paper_id}` | 删除论文（软删，返回 204） | `paper_id`（路径，**必填**） |
| GET | `/api/papers/{paper_id}/chunks` | 该论文的切块列表 | `paper_id`（**必填**）、`limit`（默认 50）、`offset`（默认 0） |
| GET | `/api/papers/{paper_id}/file` | 下载 PDF 原件（需 API key） | `paper_id`（**必填**） |
| GET | `/api/downloads/{paper_id}` | 下载 PDF 原件（**短期签名链接，无需 API key**；由 MCP 的 `paper_get_file` 发放） | `paper_id`（**必填**）、`exp`（Unix 过期时间）、`sig`（HMAC 签名） |
| GET | `/api/papers/{paper_id}/degradations` | 该论文的解析降级记录 | `paper_id`（**必填**）、`include_resolved`（默认 `false`） |
| POST | `/api/papers/{paper_id}/reindex` | 重建该论文的切块与向量（返回 202） | `paper_id`（**必填**） |

### 2.5 检索

| 方法 | 端点 | 说明 | 参数 |
|---|---|---|---|
| POST | `/api/search` | 检索：关键词 / 语义 / 混合，可选精排与过滤面 | 请求体见下表 |
| GET | `/api/search-logs` | 历史检索日志（复盘 Bad Case） | `limit`（默认 50）、`since`（时间下界）、`mode` |

`POST /api/search` 请求体：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `query` | string | — | **必填**，自由文本（中英文均可） |
| `mode` | string | `hybrid` | `keyword`（BM25）/ `semantic`（向量）/ `hybrid`（双路融合） |
| `top_k` | int | `10` | 期望返回的论文数（1–50） |
| `filters` | object | 无 | 见下表 |
| `rerank` | bool | `false` | `true` 时用交叉编码器对候选重排（更准，更慢） |
| `backend` | string | 跟随 `SEARCH_BACKEND` | `native` / `python`，只影响 `mode=hybrid` |
| `facets` | bool | `false` | 同时返回各过滤键在当前过滤条件下的可选值与论文数 |

`filters` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `year_from` / `year_to` | int | 发表年份区间 |
| `authors` | string[] | 作者名 |
| `venue` | string[] | 会议/期刊名 |
| `venue_year` | string[] | 会议/期刊**那一届**的年份（与论文自身年份是两回事） |
| `paper_type` | string[] | 论文类型（如 journal / conference） |
| `doi` / `arxiv_id` | string | 精确标识符 |
| `identifier` | string[] | `<scheme>:<值>`，如 `doi:10.1109/...`；未知 scheme 直接 422 |
| `tag` | string[] | 标签（四个来源的并集） |
| `ieee_terms` / `author_terms` / `dynamic_index_terms` / `source_tags` | string[] | 按来源分类的索引词，可分别过滤 |

`POST /api/search` 响应体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `query` | string | 原始查询（恒为请求值） |
| `rewritten_query` | string \| null | 实际用于检索的改写后查询（改写未生效时为 `null`） |
| `mode` | string | 实际使用的检索模式 |
| `backend` | string | 实际使用的融合后端（单腿模式恒为 `python`） |
| `total` | int | 当前过滤条件下命中的**论文**总数（真值） |
| `candidates` | int | 实际喂进聚合的**切块**候选数（解释「为什么返回的论文比候选少」） |
| `took_ms` | number | 本次检索耗时（毫秒） |
| `rerank` | object | `{enabled, applied, model, took_ms}`；降级时 `model`/`took_ms` 为 `null` |
| `rewrite` | object | `{enabled, applied, model, took_ms}` |
| `facets` | object \| null | `facets=true` 时给出各过滤键的取值与论文数；失败为 `null` |
| `results` | array | 论文级结果，见下表 |

`results[]` 每条：

| 字段 | 说明 |
|---|---|
| `paper_id` / `title` / `authors` / `year` | 论文基本信息 |
| `doi` / `arxiv_id` / `venue` / `venue_year` / `paper_type` / `volume` / `issue` / `pages` / `publication_date` | 元数据（缺失为 `null`） |
| `score` | 最终排序分（0–1 相对分） |
| `relevance` | 分档：`high`（≥0.9）/ `medium`（≥0.6）/ `low` |
| `retrieval_score` | 一阶段（召回）分；未精排时为 `null` |
| `rerank_score` | 精排原始分；未精排或精排降级时为 `null` |
| `evidence` | 证据块数组：`chunk_id`、`page`、`section`、`text`（默认最多 3 条/篇） |

```bash
# 混合检索 + 精排 + 过滤面
curl -X POST http://127.0.0.1:8077/api/search \
  -H "Authorization: Bearer $PAPER_API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"low power SRAM leakage reduction","mode":"hybrid","top_k":5,
       "rerank":true,"facets":true,
       "filters":{"year_from":2020,"paper_type":["journal"]}}'
```

### 2.6 作业

| 方法 | 端点 | 说明 | 参数 |
|---|---|---|---|
| GET | `/api/jobs` | 作业列表 | `limit`（默认 20）、`offset`（默认 0）、`stage`、`paper_id` |
| GET | `/api/jobs/queue` | 队列状态：在跑数、高/低优先级排队数、并发上限 | 无 |
| GET | `/api/jobs/{job_id}` | 单个作业的阶段、进度与失败归因 | `job_id`（路径，**必填**） |
| POST | `/api/jobs/{job_id}/retry` | 重试失败作业（返回 202） | `job_id`（路径，**必填**） |

作业阶段（`stage`）按顺序为：`RECEIVED` → `QUEUED` → `DOWNLOADING` → `STORED` → `PARSING` → `CHUNKING` → `EMBEDDING` → `INDEXING` → `COMPLETED`，任一步失败变 `FAILED` 并带 `error_code` / `error_message`。

### 2.7 元数据

| 方法 | 端点 | 说明 | 参数 |
|---|---|---|---|
| GET | `/api/papers/{paper_id}/metadata` | 元数据 + 每个字段的来源账本（谁在何时写入） | `paper_id`（**必填**） |
| PATCH | `/api/papers/{paper_id}/metadata` | 手动修正元数据 | `paper_id`（**必填**）；body 可含 `title`、`abstract`、`language`、`year`、`venue`、`venue_year`、`volume`、`issue`、`pages`、`paper_type`、`publication_date`、`doi`、`arxiv_id`、`authors`、`tags`、`url`（只提交要改的字段） |
| POST | `/api/papers/{paper_id}/metadata/rollback` | 回滚某字段到历史版本 | `paper_id`（**必填**）；body `field`（**必填**）、`provenance_id`（**必填**，来自 GET metadata 的账本） |
| GET | `/api/metadata/review` | 待复核清单（冲突 / 歧义） | `status`（可重复）、`limit`（默认 50） |
| POST | `/api/metadata/import` | 导入外部题录（IEEE raw / CSL-JSON / 通用 JSON） | `source_type`（默认 `import_file`）、`dry_run`（默认 `true`，只看不写）、`apply`、`limit` |
| POST | `/api/metadata/apply` | 提交复核决定 | body `entries`（复核条目）、`mode`（`fill` 默认 / `overwrite`）、`fields`（限定字段） |
| POST | `/api/metadata/sources/{source_id}/attach` | 把某条来源记录挂到指定论文 | `source_id`（路径，**必填**）；body `paper_id`（**必填**） |

> 元数据改动**不会自动改变检索过滤结果**：过滤读的是索引里的元数据快照。
> 只改元数据用 `uv run python scripts/refresh_index_metadata.py`（秒级）；要连向量一起重算才用 `reindex`。

---

## 3. 常见问题

### 3.1 HTTP 状态码

| 状态码 | 含义 | 常见触发与处理 |
|---|---|---|
| 200 | 成功 | — |
| 202 | 已受理 | 导入/重试/重建索引类接口：返回的是作业，用 `/api/jobs/{job_id}` 轮询进度 |
| 204 | 成功且无响应体 | `DELETE /api/papers/{paper_id}` |
| 401 Unauthorized | 没带或带了格式不对的凭证 | 请求头缺 `Authorization: Bearer <key>`；响应带 `WWW-Authenticate: Bearer` |
| 403 Forbidden | 凭证不对 | `PAPER_API_KEY` 不匹配；改了 `.env` 后忘了重启应用 |
| 404 Not Found | 路径不存在或功能未开启 | `/api/papers/ingest/dir` 在 `INGEST_LOCAL_ROOTS` 为空时**就是 404**（功能关闭，不是路径写错） |
| 409 Conflict | 状态冲突 | 目前只有一处：重试一个**不是** `FAILED` 的作业（`only FAILED jobs can be retried`） |
| 413 Request Entity Too Large | 单请求总字节超限 | 超 `INGEST_MAX_REQUEST_MB`（默认 200 MB）；拆分请求或调大该值 |
| 415 Unsupported Media Type | 文件类型不支持 | 非 PDF 上传；或压缩包不是 zip（7z / rar / tar 一律 415，会把支持格式写在响应里） |
| 422 Unprocessable Entity | 参数校验失败 | 字段类型/取值非法；响应体含 `loc`/`msg`/`type`，对照定位。常见：单文件超 `INGEST_MAX_FILE_MB`、文件数超 `INGEST_MAX_FILES_PER_REQUEST`、`identifier` 的 scheme 不认识 |
| 429 Too Many Requests | 被准入限流 | 在途上传请求超 `INGEST_UPLOAD_CONCURRENCY`，或多文件请求遇上队列水位；**按响应头 `Retry-After` 退避重试**（秒） |
| 500 Internal Server Error | 服务端异常 | 看应用日志；多为外部依赖返回了非预期响应 |
| 503 Service Unavailable | 依赖暂不可用 | 检索后端不可用（查询向量化失败、索引异常）、对象存储不可用、删除论文时的清理失败；推理容器队满也会以 503 体现（`inference queue full`）。退避重试，必要时调 `INFERENCE_QUEUE_DEPTH` |

### 3.2 作业错误码（`error_code`）

出现在 `GET /api/jobs/{job_id}` 与作业列表里，表示**这次导入为什么失败**。

| 错误码 | 含义 | 处理 |
|---|---|---|
| `NO_TEXT_LAYER` | PDF 没有文本层（扫描件） | 当前不支持 OCR；换成有文本层的 PDF，或先自行 OCR |
| `ENCRYPTED_PDF` | PDF 有密码/加密 | 去掉密码后重新导入 |
| `CORRUPT_PDF` | PDF 损坏或不是有效文档 | 重新下载/换源 |
| `DOWNLOAD_FAILED` | URL 下载失败或超时 | 检查 URL 可公开访问、网络可达；必要时调大 `INGEST_DOWNLOAD_TIMEOUT` |
| `OVERSIZED` | 文件超 `INGEST_MAX_FILE_MB` | 拆分或调大上限 |
| `UNSUPPORTED_TYPE` | 类型不支持或内容为空 | 只支持 PDF（压缩包只支持 zip） |
| `DUPLICATE_FINGERPRINT` | 同一篇论文已存在（指纹重复） | 不是错误：库里去重生效。要强制重导先删除原论文 |
| `PARSE_BACKEND_UNAVAILABLE` | 指定的解析后端不可达且**不允许降级** | 只出现在「明确要求某后端且禁止回退」的调用（如验收脚本）；正常流水线会降级并记账 |
| `PARSE_FAILED` | 解析失败 | 看 `error_message` 细节；换解析后端（`PARSER_BACKEND`）再试 |
| `EMBEDDING_FAILED` | 向量化失败 | 检查 `paperbox-embedding` 容器与 `EMBEDDING_TIMEOUT`（排队过久会表现为批次失败） |
| `INDEX_FAILED` | 写索引失败 | 检查 OpenSearch 是否健康、索引映射是否匹配（未声明字段会被 `strict` 拒绝） |
| `STORAGE_FAILED` | 对象存储读写失败 | 检查 MinIO 容器与凭据 |
| `INTERRUPTED` | 进程重启打断了作业 | 用 `POST /api/jobs/{job_id}/retry` 重试 |
| `INTERNAL` | 未归类的内部错误 | 看应用日志与 `error_message` |

### 3.3 解析降级码（`degraded_reason` / `GET .../degradations`）

表示**这次导入成功了，但某个环节打了折扣**——不阻塞使用，但要知道结果可能不完整。

| 降级码 | 含义 | 影响 |
|---|---|---|
| `docling_unavailable` | 指定的 docling 后端不可达，已用内置 pypdf 解析 | 双栏/表格论文的阅读顺序与结构可能不如 docling |
| `formulas_as_text` | 公式未转 LaTeX（开关关闭，或服务端在公式上失败后自动去掉公式重试） | 正文里的公式以纯文本形式出现 |
| `table_structure_lost` | 表格未能保持结构 | 表格内容仍在正文，但行列表格结构丢失 |
| `reading_order_unverified` | 阅读顺序未经校验（降级解析侧） | 双栏论文可能出现段落顺序错乱 |
| `pagination_truncated` | 只解析了前 N 页（`PARSER_MAX_PAGES > 0`） | 后续页未进入索引；该次解析不进缓存 |
| `section_title_too_long` | 章节标题过长，已降级为正文 | 文本不丢，但不再作为章节标题参与切块与检索 |
| `semantic_fallback` | 语义切块失败，该章节退回定长切块 | 切块边界不如语义模式理想 |
| `parse_degraded` | 解析层其它降级（兜底标记） | 结合 `degraded_reason` 文本判断 |

### 3.4 响应里的「空值」符号

| 现象 | 含义 |
|---|---|
| `rerank.model = null` 且 `rerank_score = null` | **精排静默降级**了：超时（`RERANK_TIMEOUT` 太小）或服务不可用；结果仍是「能搜到但没重排」 |
| `rewrite.enabled = false` | 查询改写开关关闭（`QUERY_REWRITE_ENABLED=false`） |
| `rewrite.applied = false` | 开关开着但本次没改写（纯英文查询，或重写失败降级） |
| `keyword_score` / `semantic_score` 为 `null` | 正常：引擎侧融合只给出一个融合分（`backend=native` 时恒如此） |
| `retrieval_score = null` | 该论文没经过精排（一阶段分的回填只在精排时发生） |
| `facets = null` | 聚合失败只记 warning、不返回 503；`total` 同一纪律：计数失败退回候选池大小 |
| `total >> len(results)` | `total` 是命中的**论文总数**（真值），`results` 只是本次返回的前 `top_k` 篇 |
| `evidence` 少于 3 条 | 该论文命中的块少于 3，或命中的块被判为噪声（页眉/参考文献等）后不足 |

### 3.5 使用中会遇到的问题

**Q1 检索结果顺序没变化、也没有 `rerank_score`？**
多半是精排降级（见 §3.4）。候选数 = `top_k × RERANK_CANDIDATES`，多语言档每候选约 0.06–0.3 秒，
`top_k=10` 就可能是几十秒；把 `RERANK_TIMEOUT` 调到 `60` 再看日志里有没有 `rerank request failed`。

**Q2 检索报 503？**
两处来源：① 推理队列满（日志 `inference queue full (depth=..., workers=1)`）—— 降低并发、稍后重试，
或调大容器 `INFERENCE_QUEUE_DEPTH`（定值规矩：`depth × 单次推理耗时 ≤ EMBEDDING_TIMEOUT / 2`）；
② 向量/精排容器没起来 —— `docker compose ps` 看 `paperbox-embedding` 是否 healthy。

**Q3 导入一直停在 `QUEUED`？**
`INGEST_CONCURRENCY` 是同时跑的流水线数（默认 2），排队是正常行为；用 `GET /api/jobs/queue` 看队列深度。
若前面有大压缩包在跑，等它结束即可。

**Q4 上传报 429 / 413 / 415 / 422？**
分别对应：准入限流（按 `Retry-After` 退避）、单请求总字节超限、文件类型不支持（非 PDF，或压缩包不是 zip）、
参数校验失败（单文件超限/文件数超限/非法过滤器）。对照 §3.1 与 `.env` 里的 `INGEST_*` 上限。

**Q5 刚导入的论文搜不到？**
① 先确认作业已完成（`stage=COMPLETED`）；② `GET /api/consistency` 对账，看是「有行无文档」还是索引没建；
③ 确认索引别名指向正确（`uv run python scripts/create_index.py` 会回显当前索引与别名）。

**Q6 改了元数据（PATCH / 导入 / 合并），按新值过滤却查不到？**
过滤读的是索引里的**快照**，不会实时改变。只改元数据用
`uv run python scripts/refresh_index_metadata.py`（秒级）；连向量一起重算才用 `reindex`（较慢）。

**Q7 `facets` 里没有某个 venue，但它确实存在？**
取值的 top-N 上限是 50 —— **上限之外是「没列」不是「没有」**。收窄过滤条件后再看。

**Q8 服务是好的，但每个请求都像卡了几秒？**
地址写成了 `localhost`。Windows 上 `localhost` 优先解析到 IPv6 回环，而 WSL2 的镜像网络不转发 IPv6 回环，
每次连接先撞约 8 秒超时才退回 IPv4。**所有地址一律写 `127.0.0.1`**。

**Q9 Windows 上 `curl` 容器端口不通（连接超时）？**
WSL2 里的依赖端口要在 WSL 的防火墙里放行：`wsl -e -u root bash -lc "ufw allow 9200/tcp"`（9000/8090/5432 同理）。
放行后 `uv run python scripts/healthcheck.py` 应全绿。

**Q10 解析一篇论文很慢（几十秒到几分钟）？**
先看是不是开着公式转 LaTeX（`DOCLING_FORMULA_ENRICHMENT=true` 会数倍增加耗时）；
再确认 `DOCLING_URL` 是否指向一个真实运行的 docling 服务 —— 指向了不存在的主机会等待超时后才降级，
把地址留空则**立即**降级为 pypdf。

**Q11 中文检索效果不好？**
中文最大的瓶颈在检索前的查询改写。开启 `QUERY_REWRITE_ENABLED=true`（需配好 `QUERY_REWRITE_URL` / `MODEL` / `API_KEY`）
可以让中文查询先改写成英文检索式，实测中文定标集 Hit Rate@1 从 0.30 提到 0.90。
另外确认索引用的是 CJK 分词（`paper_chunks_v3` 及之后的索引）。

**Q12 应用启动直接报错退出？**
看日志首行。最常见是 `QUERY_REWRITE_ENABLED=true` 但 `QUERY_REWRITE_URL` / `MODEL` / `API_KEY` 有缺（这是刻意的
「不许静默失效」）；其次是 `PARSER_BACKEND` / `SEARCH_BACKEND` 填了非法值（只接受 `docling|pypdf` 与 `native|python`）。

**Q13 想换精排模型或向量模型？**
精排：改 `RERANK_MODEL`（非内置档还要给 `RERANK_MODEL_FILE`）—— **根 `.env` 与 `infra/.env` 两处都要改**，
然后重建容器并重启应用；只改一边会出现「容器跑新模型、响应报旧模型名」。
向量：换模型必须**新建索引**并切换别名（向量维度/空间不可原地替换），然后重算向量（`reindex`）。

**Q14 机器内存吃紧 / 容器被 OOM-kill？**
按影响顺序调：`RERANK_MAX_BATCH`（精排激活内存≈候选数 × 文本长度）、`ORT_THREADS`、`MAX_BATCH`、
`INFERENCE_QUEUE_DEPTH`；docling 容器另设 `DOCLING_MEM_LIMIT` / `DOCLING_CPUS`（宁可它被杀，应用会降级 pypdf）。
