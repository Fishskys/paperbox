# 评测闭环与运维部署

| 项 | 内容 |
|---|---|
| 状态 | 依据 commit `54048a3` 的工作树实测（2026-09-22） |
| 关键文件 | `app/eval/metrics.py`、`scripts/eval.py`、`scripts/build_eval_set.py`、`scripts/build_arxiv_ids.py`、`scripts/healthcheck.py`、`scripts/acceptance.py`、`scripts/reindex.py`、`scripts/purge_deleted.py`、`scripts/bulk_ingest.py`、`scripts/bulk_ingest_dir.py`、`scripts/create_index.py`、`evals/`、`infra/docker-compose.yml`、`docker-compose.yml`、`Dockerfile`、`.env.example`、`logs/` |
| 相关文档 | `AGENTS.md` §3.1/§3.2/§3.3/§5/§6、`README.md` §3.1/§6/§7、`docs/progress/project.md` §10–§14、`evals/README.md`、`docs/architecture/00-overview.md` |

## 1. 职责边界（做什么 / 不做什么）

**做**

- 检索质量的**量化闭环**：纯函数指标 → 定标集 → 对**运行中的服务**跑评测 → JSON/报告落盘（`app/eval/metrics.py`、`scripts/eval.py`）。
- 定标集的**可复现构建**：人工 spec → 解析到真实 `paper_id`（`scripts/build_eval_set.py`）；语料清单生成（`scripts/build_arxiv_ids.py`）。
- **真机运维脚本**：健康检查、端到端验收、批量导入、重建索引、清理已删论文残留、索引迁移（`scripts/*.py`，见 §2 与 §5 总表）。
- **部署两形态**的配置与陷阱：本机（WSL Docker + 宿主 app）与 Linux 服务器（宿主直跑或容器化），见 `AGENTS.md` §3.1。
- **目录纪律**：运行日志一律进 `logs/{codex,app,eval}/`，仓库根不写 `*.log`（`AGENTS.md:327`、`.gitignore:23-24`）。

**不做**

- 评测**不跑在应用进程里**：`scripts/eval.py` 是独立客户端，只发 `POST /api/search`（`scripts/eval.py:2-23`）。
- 单元测试**不碰真机**：pytest 全程不连 PostgreSQL/OpenSearch/MinIO，真机验证只放 `scripts/`（`AGENTS.md:329-331`）。
- 根 `docker-compose.yml` 只打包**应用**，四个依赖在 `infra/docker-compose.yml`（两份 compose 明确分离，`docker-compose.yml:1`、`infra/docker-compose.yml:2`）。
- 不自动删数据：`create_index.py` 只切别名、旧索引保留供回滚（`scripts/create_index.py:23-28`）；`purge_deleted.py` 默认**只**清索引文档与对象、保留 PostgreSQL 行，只有显式 `--hard` 才删 PG 行（`scripts/purge_deleted.py:81`）。

## 2. 关键文件与函数（文件 → 函数/类 → 作用，带行号）

| 文件（行数） | 函数/类 | 行号 | 作用 |
|---|---|---|---|
| `app/eval/metrics.py`（174） | `METRIC_NAMES` | 38 | 报告顺序 `("hit_rate","recall","mrr","ndcg")` |
| | `_check_k` / `_relevant` / `_top_k` | 41-46 / 49-61 / 64-65 | `k` 必须为正 `int`（`bool` 也拒）；把 `{id: grade}` 归一为 `grade > 0` 子集（值转 `int`，失败跳过）；取前 `k` 项并 `str()` 化 |
| | `hit_rate_at_k` | 68-80 | 命中窗口内任一相关论文 → `1.0`，否则 `0.0` |
| | `recall_at_k` | 83-97 | 命中数 / 标注相关论文数（分母不是 `k`） |
| | `mrr` | 100-111 | 第一个相关论文的倒数排名，**看全量排名** |
| | `ndcg_at_k`（内部 `_dcg` 114-121） | 124-144 | 实际 DCG（截断到 `k`）/ 理想 DCG（标注按 grade 降序取 `k`）；`gain = 2**grade-1`、`discount = log2(rank+1)`；IDCG 为 0 时返回 `0.0` |
| | `aggregate` | 147-174 | 跨查询求均值，返回 `{metric: {"mean","n"}}` |
| `scripts/eval.py`（499） | `load_env` / `load_jsonl` | 64 / 78 | 极简 `KEY=VALUE` 读取；`.jsonl` 容错读取（空行跳过、非法 JSON 报行号） |
| | `load_queries` / `load_labels` | 95 / 103 | 校验 `id`/`query`；labels 按 `query_id` 聚成 `{paper_id: grade}` |
| | `parse_k` / `parse_modes` / `rerank_variants` | 119 / 138 / 155 | 命令行解析；`both` → `[False, True]` |
| | `ranked_paper_ids` / `search_once` | 171-189 / 192-213 | 一次 `POST /api/search`（返回 ids / 原始结果 / `took_ms`）；把论文级结果折叠成 best-first **去重** id 列表 |
| | `score_query` | 216-225 | 产出 `mrr`、`hit_rate@k`、`recall@k`、`ndcg@k` |
| | `group_summary` | 231-257 | 按 `mode\|rerank` 分组求均值，**跳过 `error` 行** |
| | `format_markdown` / `format_console` | 260 / 338 | Markdown 报告（汇总表 + 前 5 明细 + 失败清单）与控制台摘要 |
| | `build_parser` / `main` | 358 / 374 | CLI 与主流程；退出码 `0` 全成功 / `1` 有失败查询（495） |
| `scripts/build_eval_set.py`（140） | `_docker_argv` / `psql` | 25-43 / 46-60 | 前缀按 `PAPERBOX_DOCKER_PREFIX` > PATH 上的 `docker` > 回退 `wsl -e docker`；`docker exec … psql -t -A -F\| -c` 只读查询，失败抛 `RuntimeError` |
| | `load_corpus` / `resolve` | 63 / 78 | 取存活 `INDEXED` 论文；按 `arxiv` 或 `title_like` 前缀解析成 `paper_id` |
| | `main` | 87-140 | 读 `queries-spec.json`，写 `queries.jsonl` + `labels.jsonl`，打印 UNRESOLVED |
| `scripts/build_arxiv_ids.py`（50） | 模块级流程 | 7-50 | 5 个主题各 12 条，去重后写 `evals/arxiv_ids.txt`（44-48） |
| `scripts/healthcheck.py`（107） | `host_port` | 36-42 | 从 URL 文本解析主机与端口（缺省 80） |
| | `check_tcp` / `check_http` | 45 / 58 | TCP 连接与 HTTP 探测，各自打印 `ok`/`FAIL` 行 |
| | `main` | 74-103 | 依赖逐项检查 + 索引文档计数，`RESULT:` 汇总 |
| `scripts/acceptance.py`（231） | `Checker` | 49-61 | 逐条 `PASS/FAIL/SKIP` 记录与汇总 |
| | `main` | 64-227 | 跑 plan §38 的 8 条（外加 `0 dependencies healthy`，共 9 行输出） |
| `scripts/reindex.py`（93） | `targets` | 33-46 | 未删论文；`--missing` 时排除已有 chunks 的论文 |
| | `main` | 49-89 | 逐篇 `tasks.reindex_paper`，统计 chunks 与 stage |
| `scripts/purge_deleted.py`（183） | `deleted_papers` | 63-71 | 全部软删论文，按 `deleted_at` 升序 |
| | `count_index_docs` | 43-49 | 指定论文在别名索引里的 chunk 文档数 |
| | `hard_delete_paper` | 81-108 | `--hard`：逐表删该论文的 chunks/files/identifiers/sources/provenance/authors/tags/jobs + `papers` 行，返回各表计数 |
| | `main` | 109-183 | 逐篇删 OpenSearch 文档 + MinIO 前缀；`--dry-run` 只预览；`--hard` 追加删 PG 行 |
| `scripts/check_consistency.py`（111） | `main` | 84-111 | 真机三端对账；有漂移返回 1，`--no-fail` 恒 0 |
| `scripts/refresh_index_metadata.py`（141） | `paper_ids_statement` / `build_updates` / `main` | 41-58 / 81-93 / 94-141 | 先 `update_mapping`（除非 `--no-mapping`）再批量 partial update；`--dry-run` / `--paper-id` / `--limit` |
| `scripts/bulk_ingest.py`（397） | `parse_arxiv_list` | 72-98 | 解析 `id<TAB>topic<TAB>title`，忽略 `#`/空行，id 去重 |
| | `fetch_existing_arxiv_ids` / `start_ingest` / `poll_job` | 114 / 137 / 151 | 分页 `GET /api/papers` 收集已有 `arxiv_id`（`--resume`）；`POST /api/papers/ingest` → 轮询 `GET /api/jobs/{id}` 到终态或超时 |
| | `main` | 258-393 | 串行导入，写 `evals/ingest-report.json` |
| `scripts/bulk_ingest_dir.py`（599） | `is_candidate` / `matches_glob` | 77 / 87 | `.pdf` + 非隐藏/临时文件；`**` 语义的 glob |
| | `collect_files` / `build_manifest` | 104 / 132 | 遍历（不跟随符号链接）+ 大小预筛，产出 candidates/skipped |
| | `retry_delay` / `parse_retry_after` / `post_with_backoff` | 180 / 200 / 291 | `Retry-After` 优先，否则指数退避 + 抖动，封顶 60s；429 时按此等待重试（默认 6 次） |
| | `summarize` / `completed_paths` | 211 / 224 | 计数（accepted/duplicate/rejected/completed/failed/skipped）；`--resume` 已完成集合 |
| | `run_server_side` / `run_client_side` | 331 / 384 | 两条路径：`/ingest/dir` 零传输 或 `/ingest/files` 每请求 1 个文件 |
| | `main` | 480-591 | CLI + 报告落盘 |
| `scripts/create_index.py`（269） | `verify` / `migrate` | 91 / 145 | 幂等建索引与别名；`--migrate-from` 服务端 `_reindex` 后计数相等才原子切别名 |

## 3. 数据结构（表/字段/索引，或内存结构）

**指标的内存契约**（`app/eval/metrics.py:6-11`）

| 名称 | 形状 | 语义 |
|---|---|---|
| `relevance` | `{paper_id: grade}` | `2` 高度相关 / `1` 相关 / `0` 不相关；**未出现的论文视为不相关** |
| `ranked_ids` | `Sequence[str]` | 检索顺序，最好在前；chunk 命中通常由调用方折叠成论文级并保留最佳名次 |

**定标集文件**（`evals/`）

| 文件 | 结构 | 实测规模（本工作树） |
|---|---|---|
| `queries-spec.json` | 顶层 `{note, queries[]}`；每条含 `id`/`query`/`language`/`note`/`labels[{arxiv\|title_like, grade}]` | 50 条查询 |
| `queries.jsonl` | `{"id","query","language","note"}`（`scripts/eval.py:12`） | 50 行（`en` 40 / `zh` 10） |
| `labels.jsonl` | `{"query_id","paper_id","grade"}`（`scripts/eval.py:13`） | 77 行（`grade=2` 64 条 / `grade=1` 13 条），覆盖全部 50 个查询 |
| `queries-zh-en.jsonl` | 与 `queries.jsonl` 同形，10 条中文查询的英文改写 | 10 行 |
| `arxiv_ids.txt` | `id<TAB>主题<TAB>标题`，`#` 注释（`scripts/bulk_ingest.py:72-98`） | 62 行（含 2 行注释） |
| `arxiv_ids_retry.txt` | 同上，首批失败后的重导清单 | 9 行 |

**评测报告 JSON**（`scripts/eval.py:464-483`）

| 键 | 内容 |
|---|---|
| `generated_at` / `base_url` / `top_k` | 运行元信息 |
| `total` / `success` / `failed` | 总行数 = 查询 × mode × rerank；成功/失败数 |
| `params` | `k`、`modes`、`rerank`、`rerank_variants`、`queries`、`labels`、`metric_names`、`labelled_queries` |
| `summary` | `{"mode\|on": {metric: {"mean","n"}}}`（`scripts/eval.py:234`） |
| `per_query` | 每行 `query_id/query/mode/rerank/group/ranked_ids/ranked_papers/metrics/took_ms/error/labels/relevance`（`scripts/eval.py:420-433`） |

**批量导入报告**

| 脚本 | 报告 | 顶层键 |
|---|---|---|
| `scripts/bulk_ingest.py` | `evals/ingest-report.json` | `started_at/finished_at/base_url/total/completed/failed/skipped/items[]`（`bulk_ingest.py:372-381`） |
| `scripts/bulk_ingest_dir.py` | `evals/ingest-dir-report.json`（`bulk_ingest_dir.py:47`） | `started_at/finished_at/mode/root/glob/base_url/counts/error/items[]`（`bulk_ingest_dir.py:574-584`） |

## 4. 调用链（从入口到落地，逐跳，带函数名）

**评测一轮**

```
uv run python scripts/eval.py [--modes ...] [--rerank both] [--k 1,3,5,10]
 └─ main (eval.py:374)
     ├─ load_env → base_url = --base-url > PAPER_API_BASE > PAPER_API_URL > http://127.0.0.1:8077 (:378-383)
     ├─ load_queries / load_labels (:393-394)
     ├─ httpx.Client(base_url, Bearer PAPER_API_KEY) (:405-413)
     └─ 每个 query × mode × rerank：
         search_once → POST /api/search (:192-213)
         → ranked_paper_ids（去重）(:171-189)
         → score_query → hit_rate_at_k / recall_at_k / mrr / ndcg_at_k (:216-225)
     ├─ group_summary (:231-257)
     └─ 写 report-*.json (:486) + report-*.md (:487) → 退出码 0/1 (:495)
```

**定标集重建**

```
scripts/build_eval_set.py → main (:87)
 └─ _docker_argv → docker|wsl -e docker exec <PAPERBOX_PG_CONTAINER> psql -t -A -F| -c … (:25-60)
     ├─ load_corpus: select arxiv_id,id,title from papers where deleted_at is null and status='INDEXED' (:65-68)
     ├─ resolve: arxiv 全等匹配 / title_like 前缀匹配 (:78-84)
     └─ 写 evals/queries.jsonl + evals/labels.jsonl (:127-128)，未解析项打印 “UNRESOLVED” (:131-134)
```

**批量导入**

```
bulk_ingest.py: load_arxiv_list →(--resume) fetch_existing_arxiv_ids → start_ingest
  → poll_job(COMPLETED/FAILED) → chunk_count → ingest-report.json
bulk_ingest_dir.py: collect_files → build_manifest
  → server-side POST /api/papers/ingest/dir ／ --via-http 每文件 POST /api/papers/ingest/files
  → poll_job（429 走 post_with_backoff）→ summarize → ingest-dir-report.json
```

**运维**

```
healthcheck.py: check_tcp(postgres) → check_http(opensearch /_cluster/health, minio /minio/health/live,
                embedding /health, api /health) → GET <alias>/_count
acceptance.py : /health → /api/papers/ingest → /api/jobs/{id} → /api/papers/{id}[/file|/chunks]
                → OpenSearch _count(embedding:*) → /api/search(keyword|semantic|hybrid) → 无鉴权 401
reindex.py    : targets → tasks.reindex_paper → 统计 PaperChunk 数
purge_deleted.py: deleted_papers → count_index_docs → opensearch.delete_by_paper_id + object_storage.delete_prefix
create_index.py: ensure_index → _reindex(wait_for_completion=false) → wait_for_task → 计数相等 → update_aliases
```

## 5. 不变量与踩过的坑

| 不变量/坑 | 说明 | 出处 |
|---|---|---|
| `k` 必须为正整数 | `bool` 也被拒（`isinstance(k, bool)` 先判），坏 CLI 参数**报错而不是静默记 0** | `metrics.py:41-46` |
| Hit Rate 分母是**查询**、Recall 分母是**标注数** | 无标注的查询命中不了（`0.0`）；Recall 的分母不是 `k`，重复 id 只计一次 | `metrics.py:71-74, 86-97` |
| MRR **不受 `k` 限制** | 看全量排名，`0.0` 表示没有命中 | `metrics.py:100-111` |
| NDCG 的 0/0 保护 | `ideal <= 0` 时返回 `0.0` | `metrics.py:142-143` |
| `aggregate` 跳过脏值 | `bool` 与非数值不计入，`n` 反映实际观测数 | `metrics.py:163-166` |
| 单个查询失败不中断整轮 | 记 `error` 字符串、指标置全 0，末尾打印 `success/failed` | `scripts/eval.py:442-445, 492` |
| 报告默认带 UTC 时间戳名 | `report-<YYYYmmddTHHMMSSZ>.json`（Markdown 同名换后缀）；历史报告的 `params.queries` 仍写旧路径 `D:\hermes\...`，不回填 | `scripts/eval.py:164-165, 396-398` |
| **日志不写仓库根 + 单测不碰真机** | 日志 → `logs/{codex,app,eval}/`（已 gitignore）；pytest 只跑纯函数与内存 SQLite，真机验证放 `scripts/` 且自带清理 | `AGENTS.md:327-331`、`.gitignore:23-24`、`tests/conftest.py:62` |
| `extra_hosts` 陷阱 | Linux 的 Docker 引擎**不自带** `host.docker.internal`，根 compose 显式声明 `host-gateway`；不加则容器内四依赖全不可达 | `docker-compose.yml:5-6, 24-26`、`AGENTS.md:90` |
| 容器内 `127.0.0.1` 是容器自己 | 应用容器化时五个依赖地址必须走 `host.docker.internal` 或同网络服务名 | `.env.example:129-136` |
| 数据目录变量只认 `infra/.env` | `docker compose` 只读 compose 同目录的 `.env`，写仓库根那份无效 | `AGENTS.md:126`、`.env.example:118-119` |
| `Dockerfile` 的监听参数 | CMD 用 `sh -c exec` 展开 `PAPER_API_HOST/PORT`，此前写死导致 compose 传参**被静默忽略** | `Dockerfile:31-33`、`docs/progress/project.md:509` |
| 评测脚本不硬编码 WSL | `_docker_argv` 按 `PAPERBOX_DOCKER_PREFIX` → `docker` → `wsl -e docker` 选择，Linux 上可用 | `build_eval_set.py:26-43`、`docs/progress/project.md:510` |
| `--resume` 的键是**相对路径** | 报告里 `relative`（或退回 `path`）；`status=skipped` 的行不算已完成 | `bulk_ingest_dir.py:224-238` |
| 429 以服务端 `Retry-After` 为准 | 有该头就用它（封顶 60s），没有才指数退避 + 25% 抖动 | `bulk_ingest_dir.py:180-208` |
| 精排超时会**静默降级** | `RERANK_TIMEOUT` 默认 10 秒撑不住多语言精排，表现为“能搜到但没重排”（`rerank.model=null`） | `docs/progress/project.md:468-472`、`.env.example:49-54` |
| `purge_deleted` 只清索引与对象 | `paper_chunks` 行**故意保留**（软删语义），且只处理 `deleted_at` 非空的行 | `purge_deleted.py:13-14, 37-39` |

## 6. 配置项（键 → 默认值 → 作用 → 出处文件:行）

**应用侧（仓库根 `.env`）**

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `PAPER_API_BASE` / `PAPER_API_URL` / `PAPER_API_KEY` | `http://127.0.0.1:8077` / 空 | 评测与批量脚本的基址与 Bearer | `scripts/eval.py:52, 378-384` |
| `PAPER_API_HOST` / `PAPER_API_PORT` | `0.0.0.0` / `8077` | 监听地址与端口（容器 CMD 也读） | `.env.example:36-37`、`Dockerfile:33` |
| `POSTGRES_DSN` | `postgresql+psycopg://…@127.0.0.1:5432/paperbox` | 权威 DSN，覆盖 `POSTGRES_*` 分项 | `.env.example:12-13` |
| `OPENSEARCH_URL` / `OPENSEARCH_INDEX` / `OPENSEARCH_ALIAS` | `http://127.0.0.1:9200` / `paper_chunks_v2` / `paper_chunks_current` | 索引与别名（读写走别名） | `.env.example:16-18` |
| `MINIO_ENDPOINT` / `MINIO_BUCKET` | `127.0.0.1:9000` / `paperbox` | 原件存储 | `.env.example:21-25` |
| `EMBEDDING_URL` / `EMBEDDING_BATCH_SIZE` | `http://127.0.0.1:8090` / `16` | 向量服务；批量必须与容器 `MAX_BATCH` 一致 | `.env.example:28-32` |
| `RERANK_ENABLED` / `RERANK_MODEL` / `RERANK_MAX_BATCH` / `RERANK_TIMEOUT` / `RERANK_CANDIDATES` | `true` / `Xenova/ms-marco-MiniLM-L-6-v2` / `16` / `10` / `5` | 精排开关、模型、限批、超时、候选倍数 | `.env.example:41-55` |
| `RRF_KEYWORD_WEIGHT` / `RRF_SEMANTIC_WEIGHT` | `1.0` / `1.0` | RRF 融合权重（sweep 结论：保留等权） | `.env.example:58-59`、`docs/progress/project.md:347-349` |
| `QUERY_REWRITE_*` | `false` / 空 / `512` / `300` / `10` | 服务端查询改写（含 CJK 才改写） | `.env.example:63-73` |
| `SEARCH_LOG_ENABLED` / `SEARCH_LOG_RESULTS_LIMIT` | `true` / `20` | 检索日志（评测调用同样落库） | `.env.example:80-81`、`docs/progress/project.md:283` |
| `INGEST_MAX_FILE_MB` / `INGEST_LOCAL_ROOTS` | `100` / 空（**`/ingest/dir` 关闭，404**） | 上传大小上限（`bulk_ingest_dir.py` 客户端预筛也读它）；目录导入白名单，仅 PDF 与 app 同机时可用 | `.env.example:85, 99-102` |

**容器侧（`infra/.env`，只被 compose 插值读取）**

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `POSTGRES_DATA_DIR` | `./data/postgres` | PG 数据卷挂载 | `infra/docker-compose.yml:27` |
| `OPENSEARCH_DATA_DIR` | `./data/opensearch` | OpenSearch 数据卷 | `infra/docker-compose.yml:54` |
| `MINIO_DATA_DIR` | `./data/minio` | MinIO 数据卷 | `infra/docker-compose.yml:73` |
| `EMBEDDING_MODELS_DIR` | `./data/embedding-models` | 模型缓存卷（`FASTEMBED_CACHE_PATH=/models`） | `infra/docker-compose.yml:104, 90` |
| `PAPERBOX_BIND_IP` | `0.0.0.0` | 依赖端口绑定地址；服务器可设 `127.0.0.1` | `infra/docker-compose.yml:25, 52, 70-71, 102` |
| `POSTGRES_PASSWORD` / `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | 必填 | 凭据 | `infra/.env.example:7-9` |
| `EMBEDDING_MODEL` / `RERANK_MODEL` / `RERANK_MAX_BATCH` / `ORT_THREADS` / `MAX_BATCH` | 见文件 | 模型与资源（本机 9GB WSL 实测安全值） | `infra/.env.example:24-33` |
| `OPENSEARCH_ADMIN_PASSWORD` | `ChangeMe-Initial-Admin-2026!` | 安全插件关闭时**不参与校验**，仅防明文进仓库 | `infra/docker-compose.yml:46` |

**脚本级环境变量**

| 键 | 默认 | 作用 | 出处 |
|---|---|---|---|
| `PAPERBOX_DOCKER_PREFIX` / `PAPERBOX_PG_CONTAINER` | 自动探测 / `paperbox-postgres` | 覆盖 docker 调用前缀（如 `wsl -e`、`docker -H tcp://…`）；`psql` 运行所在容器名 | `scripts/build_eval_set.py:36-43, 48` |

**运维脚本总表**

| 脚本 | 作用 | 主要参数 | 幂等 | 碰真机数据 | 退出码 |
|---|---|---|---|---|---|
| `healthcheck.py` | 四依赖 + API 健康检查与文档数 | 无 | 是（只读） | 只读 | `0` 全通 / `1` 有失败（`healthcheck.py:103`） |
| `acceptance.py` | plan §38 端到端验收 9 项 | `--api`、`--url` | 否（会真导入一篇 arXiv PDF，判重则复用） | **写**：导入论文 | `0` 全 PASS / `1` 有 FAIL（`acceptance.py:61, 118`） |
| `reindex.py` | 重建 chunks/向量/索引 | `<paper_id…>`、`--missing` | 是（重建同输入同输出） | **写**：删旧 chunks 重写索引 | `0` 无失败 / `1` 有失败（`reindex.py:60`） |
| `purge_deleted.py` | 清已删论文的索引文档与 MinIO 对象 | `--dry-run` | 是 | **写**：删索引文档 + 对象（PG 行保留） | 无残留 `0`；有失败 `1`；`--dry-run` 恒 `0`（`purge_deleted.py:126-128, 96, 101`） |
| `check_consistency.py` | 三端（PG/MinIO/OpenSearch）只读对账，列出缺失/孤儿/删除残留 | `--no-fail` | 是（只读） | 只读 | 有漂移 `1` / 无漂移 `0` / `--no-fail` 恒 `0`（`check_consistency.py:89-116`） |
| `refresh_index_metadata.py` | 批量改写已索引文档的元数据快照（不重算向量） | `--dry-run`、`--paper-id`、`--limit`、`--no-mapping` | 是（同元数据同输出） | **写**：`PUT _mapping` + 每个 chunk 一条 partial update | 全成功 `0` / 有失败 `1`（`refresh_index_metadata.py:94-141`） |
| `bulk_ingest.py` | 按 arXiv 清单串行导入 | `--file`、`--limit`、`--resume`、`--dry-run`、`--out`、`--timeout`、`--poll-interval`、`--base-url`、`--api-key` | 否（`--resume` 靠 `arxiv_id` 跳过） | **写**：批量导入 | 无失败 `0`；有失败 `1`；`--dry-run` `0`；`--limit<=0` `2`（`bulk_ingest.py:260-262, 289, 393`） |
| `bulk_ingest_dir.py` | 导入一个文件夹 | `--root`(必需)、`--glob`、`--no-recursive`、`--limit`、`--via-http`、`--resume`、`--dry-run`、`--max-file-mb`、`--out`、`--timeout`、`--poll-interval` | 否（`--resume` 按相对路径跳过） | **写**：批量导入 | 无 error 且 `failed=0` 且 `rejected=0` → `0`，否则 `1`；root 非目录 / `--limit<=0` → `2`（`bulk_ingest_dir.py:482-484, 503-505, 591`） |
| `create_index.py` | 幂等建索引/别名；`--migrate-from` 迁移并切别名 | `--index`、`--alias`、`--migrate-from`、`--poll-interval`、`--task-timeout` | 是 | **写**：建索引/切别名（**不删旧索引**） | `verify` `0/1`（`create_index.py:117`）；`migrate` `0/1/2`（`:156-160, 246`） |
| `build_eval_set.py` | 由 spec 生成定标集 | `--spec` | 是（同输入逐字节同输出） | 只读（`psql` 只发 select） | 正常 `0`；`psql` 失败抛 `RuntimeError`（`:58-59`） |
| `build_arxiv_ids.py` | 从 arXiv API 生成语料清单 | 无 | 否（话题/条数为常量，结果随 arXiv 变化） | 否（只打外网 + 写文件） | 正常 `0` |

## 7. 测试位置与覆盖（tests/xxx.py → 覆盖什么）

| 文件 | 覆盖 |
|---|---|
| `tests/test_metrics.py` | 四个指标的正反例、手算 NDCG（`gain=2**grade-1`，`discount=log2(rank+1)`）、`k` 越过结果长度、空排名、`k` 必须为正（`test_metrics.py:179-203`）、`aggregate` 的均值/扁平行/空输入/跳过失败查询（`:214-243`） |
| `tests/test_bulk_ingest_dir.py` | 客户端纯函数：`.pdf` 预筛（含隐藏/临时后缀）、`**` glob、遍历与 `--limit`、清单拆分与相对/绝对路径、`Retry-After` 优先与封顶、退避增长与抖动、`--resume` 往返、计数、控制台按 `error_code` 分组、`--dry-run` 不发请求、root/limit 用法错误（`test_bulk_ingest_dir.py:45-274`） |
| `tests/conftest.py` | 内存 SQLite 会话（`sqlite+pysqlite:///:memory:`），PostgreSQL 专有部分唯一索引被跳过（`conftest.py:11, 62`）——这是“单测不碰真机”的落地方式 |
| `tests/test_consistency.py` | 三端对账：缺失对象/孤儿对象/缺失索引/缺失 chunk/孤儿文档/删除残留/坏 store（一个挂了另两个照样答）/未传 client |
| `tests/test_index_snapshot.py` | 索引映射与 chunk 文档形状：新字段类型、tag 按 kind 分列、`identifiers` 形状、`embedding` 仍是 1024 维 knn |
| `tests/test_refresh_index_metadata.py` | 快照刷新：每 chunk 一条 partial update（不含 embedding/text）、先 mapping 后文档、跳过软删与无 chunk 论文、`--dry-run`/`--paper-id`/`--no-mapping`、SQL 选择列覆盖 ORDER BY 列 |
| `tests/test_paper_list_filters.py` | `GET /api/papers` 过滤（venue/年份区间/paper_type/tag）+ `PaperOut` 新列序列化 |
| `tests/test_purge_deleted.py` | `purge_deleted.py`：默认只对账不删 PG 行、`--hard` 逐表删净 |

**无单元测试的真机脚本**：`scripts/eval.py`、`scripts/build_eval_set.py`、`scripts/build_arxiv_ids.py`、`scripts/healthcheck.py`、`scripts/acceptance.py`、`scripts/reindex.py`、`scripts/purge_deleted.py`、`scripts/bulk_ingest.py`、`scripts/create_index.py` —— 它们按 `AGENTS.md:331` 的设计全部依赖真实服务，验收靠实跑（`logs/app/*.log`、`logs/eval/`）。

## 8. 未做 / 已知缺口

- **报告缺语言维度**：`format_markdown` / `group_summary` 只按 `mode|rerank` 分组（`scripts/eval.py:231-295`），`docs/progress/project.md` §10/§11 里按 EN/ZH 拆的数字是在报告之外另行聚合的，脚本本身不产出。
- **无指标阈值门禁**：评测退出码只反映“有没有 HTTP 失败”，不反映指标是否达标（`scripts/eval.py:495`），回归需要人读报告。
- **评测串行执行**：查询 × mode × rerank 逐次调用（`scripts/eval.py:414-419`），基线 300 次调用、开启精排时单次可达 ≈20–31s（`docs/progress/project.md:473, 488`）。
- **`report-placeholder.*` 仅留档**：那是 6 篇语料时的占位跑，不可与基线直接比较（`evals/README.md:18`）。
- **日志无轮转**：`logs/app/paperbox-api.log` 已 61 万字节级，仓库没有轮转/清理策略；`logs/` 整体 gitignore（`.gitignore:24`）。
- **数据目录口径不一致（未确认）**：`infra/.env` 实测 `POSTGRES_DATA_DIR=/var/lib/paperbox-data/pg`，而 `AGENTS.md:121` 写“四个都在 `<仓库同级>/paperbox-data\{pg,…}`”（开发者本机绝对路径）；`infra/.env` 里其余三个确实是该目录。差异原因未核实（未跑 `docker compose config` 验证解析结果）。
- **`healthcheck.py` 的 DSN 解析未确认**：读到的是 `host_port(env.get("POSTGRES_DSN", …)[-1])`（`scripts/healthcheck.py:79`），`[-1]` 的语义无法自文本解释——疑似读取工具对凭据片段做了脱敏遮盖，未在真机验证其实际行为。
- **服务器侧动作未执行**（`docs/progress/project.md:531-532`）：`PAPERBOX_BIND_IP=127.0.0.1` 收敛依赖端口、按服务器内存放大 `ORT_THREADS`/`MAX_BATCH`/`RERANK_MAX_BATCH`/`OPENSEARCH_JAVA_OPTS`、三处数据搬迁（`pg_dump` / `mc mirror` / OpenSearch `_reindex`，**勿重算向量**）。
- **`purge_deleted.py` 的边界 + 标注质量无工具**：前者只处理 `deleted_at` 非空的论文，报告里 `0 chunk doc(s)` 不代表异常；后者靠 `queries-spec.json` 的 `note` 人工把关，仓库没有标注审计工具（`evals/README.md:12`）。
