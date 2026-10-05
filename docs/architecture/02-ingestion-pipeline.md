# 导入流水线与任务调度

| 项 | 内容 |
|---|---|
| 状态 | 依据 commit 3911a6b 的工作树实测（2026-09-22） |
| 关键文件 | `app/workers/tasks.py`（1106 行）、`app/workers/queue.py`（427）、`app/workers/housekeeping.py`（341）、`app/services/ingestion_service.py`（552）、`app/api/jobs.py`（91）、`app/api/ingestion.py`（750）、`app/core/errors.py`（199）、`app/core/config.py`（274）、`app/main.py`（96） |
| 相关文档 | `docs/architecture/09-upload-queue.md`（上传入口 HTTP 契约，分界见 §1）、`AGENTS.md` §3.8（L204-232）、`README.md` §3.2（L131-149）、`docs/progress/project.md` §15（L536-584）/§16（L588 起） |

行号均为上述文件在当前工作树中的行号。背景文档与代码冲突处一律以代码为准，冲突点集中列在 §5。

## 1. 职责边界（做什么 / 不做什么）

**本模块做什么**

- 作业状态机的定义、合法转换与落库（`ingestion_jobs.stage` / `progress` / `error_code` / `finished_at`）。
- 入队到执行：并发上限、优先级、幂等入队、重启恢复（`app/workers/queue.py`）。
- 一次作业从 `DOWNLOADING` 到 `COMPLETED` / `FAILED` 的全部阶段：取源、存原文、解析、切分、embedding、写索引（`app/workers/tasks.py`）。
- 重试（`POST /api/jobs/{id}/retry`）与重建索引（`POST /api/papers/{id}/reindex`）的路由与语义。
- 失败归因：`error_code` 取值与分类规则（`app/core/errors.py`）。
- 清理：`STORED` 检查点删 staging 对象/解包出的本地文件，以及兜底 GC（`app/workers/housekeeping.py`）。

**本模块不做什么**

- 上传入口的 HTTP 细节：multipart 逐 part 落盘、边写边算 sha256、`429 + Retry-After` 准入、`413/415/422` 契约、zip 解包与 zip-slip/zip-bomb 防护、`/ingest/dir` 白名单收敛（`app/api/ingestion.py`、`app/services/upload_admission.py`、`archive_service.py`、`local_scan.py`）——归 09 号文档（`docs/architecture/09-upload-queue.md`）。`housekeeping.py` 在 09 号文档里从"入口残留清理"视角写，本文只写它作为调度侧兜底 GC 的行为（§5、§8）。
- 检索、元数据合并引擎、删除对账（`DELETE /api/papers/{id}` 的顺序约束）不在本文范围；只在决定流水线顺序时引用。

**分界点**：HTTP 响应返回 `202` 的那一刻是本文起点——`ingestion_jobs` 行已存在、`stage=RECEIVED`（或已 `QUEUED`）、并已交给进程内队列。唯一的例外是优先级判定（`1 文件=交互 / ≥2=批`）写在入口函数里，本文必须给出其代码位置（§6）。

## 2. 关键文件与函数（文件 → 函数/类 → 作用，带行号）

| 文件 | 函数 / 类 | 作用 | 行号 |
|---|---|---|---|
| `services/ingestion_service.py` | 阶段与进度常量 `STAGE_*` / `PROGRESS_*` | 状态机的字典：`RECEIVED`/`QUEUED`/`DOWNLOADING`/`STORED`/`COMPLETED`/`FAILED` | L33-64 |
| 同上 | `IN_FLIGHT_STAGES` | 判定"重启时正在跑"的阶段元组（6 个） | L48-55 |
| 同上 | `create_job` | 插入 `stage=RECEIVED, progress=0` 的作业行（调用方 commit），创建前跑 `validate_source_payload` | L213-256 |
| 同上 | `validate_source_payload` | 仅对 `local_path` 校验：非空 + 绝对路径 | L179-194 |
| 同上 | `find_existing_paper` | 入队前判重（`paper_files.sha256` → `sha256:` 指纹） | L197-210 |
| 同上 | `mark_queued` | `UPDATE ... WHERE stage IN ('RECEIVED','QUEUED')` 置 `QUEUED`，守卫式、自己 commit | L403-423 |
| 同上 | `mark_failed` | 写 `stage=FAILED` + `error_code` + 截断到 2000 字的 `error_message` + `finished_at` | L351-372 |
| 同上 | `prepare_retry` | `UPDATE ... WHERE stage='FAILED'` 重置为 `RECEIVED`，`rowcount==1` 是并发闸门 | L375-400 |
| 同上 | `recover_jobs` | 启动时分类：`RECEIVED`/`QUEUED` → 重入队；`IN_FLIGHT_STAGES` → `FAILED` + `INTERRUPTED` | L426-462 |
| 同上 | `resolve_duplicate` | 把作业收成 `COMPLETED` 的 no-op：`payload.duplicate=True` + 指向既有论文 | L328-348 |
| 同上 | `serialize_job` | `JobOut` 的字段形状（含 `error_code` / `finished_at`） | L294-307 |
| `workers/tasks.py` | `stage_commit_hook` | 每次阶段 commit 后触发的观测钩子（生产为 `None`） | L47 |
| 同上 | `run_ingestion_job` | 队列 runner：开自己的 session，`_process_job` 失败则 `_record_failure` 且**不重抛** | L138-164 |
| 同上 | `run_retry_job` | 按 `job.paper_id` 是否为空决定走 reindex 语义还是整体重跑 | L167-188 |
| 同上 | `run_reindex_job` / `reindex_paper` | reindex 的 runner 与主体，`_run_pipeline(..., dedupe=False)` | L118-135 / L79-115 |
| 同上 | `_advance_stage` | 置 `stage`/`progress` 并 **commit**，再触发钩子 | L191-207 |
| 同上 | `_process_job` | 取源 → 判重 → 建论文行 → 存原文 → `STORED` commit → 清理源 → `_run_pipeline` | L210-291 |
| 同上 | `_load_source` / `_load_local_source` | 三种 `source_type` 的分支与 SHA256 计算（本地文件流式哈希，不缓冲） | L310-343 / L346-372 |
| 同上 | `_store_source` | 写入 `papers/<paper_id>/original.pdf`（本地源流式上传） | L375-397 |
| 同上 | `_cleanup_source` / `remove_local_file` | 删 staging 对象；`cleanup_after=true` 时删本地文件并剪空目录；均 best-effort | L400-418 / L421-440 |
| 同上 | `_run_pipeline` | `PARSING → CHUNKING → EMBEDDING → INDEXING → COMPLETED`，`dedupe` 与 `file_record` 决定是否做目标论文解析 | L443-536 |
| 同上 | `_upgrade_fingerprint` | 解析后按 DOI > arXiv > 标题+首作者+年 > sha256 升级指纹；冲突时按 `discard_on_conflict` 决定弃单或保旧 | L539-621 |
| 同上 | `_discard_duplicate_paper` | 竞态输家：清 chunk、删索引文档、删对象、软删论文行，作业收成重复 | L665-700 |
| 同上 | `_resolve_target_paper` / `_finish_non_primary` | 复用壳论文或已索引论文；非主版本不解析不索引直接完成 | L714-760 / L806-834 |
| 同上 | `_backfill_metadata` | 两层元数据：`pdf_embedded`（置信度 1.0）→ `pdf_heuristic`（0.5，只填空） | L869-932 |
| 同上 | `_replace_chunks` / `_write_embeddings` / `_index_rows` / `_mark_indexed` | 重建 chunk 行、写 embedding 溯源、构造 OpenSearch 文档、盖 `indexed_at` | L962-1078 |
| `app/services/degradation_service.py` | `record` / `resolve_stage` / `Recorder` | **T7.3 降级账本**：各阶段「能用但更薄」的结果落 `paper_degradations`（`(stage, code, detail)`）；`Recorder` 是绑定 `(session, paper, job)` 的 sink | L81 / L133 / L226 |
| 同上 | `_record_failure` | 回滚后用新事务做 `FAILED` 记账，并把论文状态置 `FAILED` | L1045-1064 |
| `workers/queue.py` | `PRIORITY_INTERACTIVE` / `PRIORITY_BATCH` | 优先级类，数值小者先出队（0 / 1） | L57-59 |
| 同上 | `kind_for_payload` | 恢复时按 `payload.source_type == "reindex"` 选 runner | L82-85 |
| 同上 | `IngestQueue.enqueue` | 幂等入队；队列未启动则内联执行并返回 `False` | L165-208 |
| 同上 | `IngestQueue.submit` | 先 `mark_queued`（提交 `QUEUED`）再入队；不可排队时返回 `None` | L218-235 |
| 同上 | `IngestQueue.recover` | 调 `recover_jobs`，把 `requeue` 列表按 kind 重新入队 | L240-259 |
| 同上 | `depth` / `stats` | 等待中作业数（不含在跑）/ 队列快照（供 `GET /api/jobs/queue`） | L264-272 / L274-291 |
| 同上 | `_worker` | 取 `(priority, seq, item)`，`asyncio.to_thread` 跑阻塞流水线，异常只记日志 | L296-328 |
| 同上 | `stop` | 取消 worker 协程；线程内已开跑的流水线继续到结束，其作业行留待下次 `recover` 标 `INTERRUPTED` | L139-154 |
| `api/jobs.py` | `get_queue` / `get_job` / `retry_job` | 队列快照 / 轮询单作业 / 重试（`202`，body 为重置后的作业） | L21-30 / L33-41 / L44-72 |
| `api/ingestion.py` | `stage_and_queue` | 单文件：校验 → 暂存 → 入队前判重 → `create_job` → `job_queue.submit(..., priority)` | L133-213 |
| 同上 | `ingest_url` / `ingest_files` / `ingest_dir` / `ingest_compressed` | 四个入口的 `submit` 调用点与优先级判定点 | L253 / L311-314、L207 / L475-481 / L657-661 |
| `core/errors.py` | `FAILURE_CODES` / `classify_failure` | 错误码全集与异常 → `(code, message)` 映射（顺序敏感） | L39-52 / L116-173 |
| `workers/housekeeping.py` | `run_gc` / `Housekeeping._loop` | 一次收集（可注入依赖）与周期循环（启动立即跑一次，之后每 `INGEST_GC_INTERVAL_S`） | L136-212 / L276-287 |
| `main.py` | `lifespan` | 启动顺序：`job_queue.start()` → `job_queue.recover()` → `housekeeping.start()`；关闭顺序相反 | L40-55 |

## 3. 数据结构（表/字段/索引，或内存结构）

**表 `ingestion_jobs`**（`app/db/models.py` L411-446）——状态机的唯一落地处：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `UUID` 主键 | `new_uuid()` 生成，出现在 API 路径里 |
| `paper_id` | `UUID` FK `papers.id ON DELETE SET NULL` | `STORED` 检查点写入；也是重试路由的依据 |
| `kind` | `String(32)`，默认 `'ingest'` | 仅审计用；队列实际用内存里的 `KIND_*` |
| `stage` | `String(32)`，默认 `'received'` | 状态机本体，无 DB 级 CHECK 约束 |
| `progress` | `Float`，默认 0 | 0/10/30/45/60/80/95/100 |
| `error_code` | `String(32)`，可空 | `app/core/errors.py` 的码；未失败为 `NULL` |
| `error_message` | `Text`，可空 | 截断到 `MAX_ERROR_LENGTH=2000`（`ingestion_service.py` L70、L366） |
| `payload` | `JSONB` | 见下 |
| `started_at` / `finished_at` | `timestamptz` | `finished_at IS NULL` 是"活作业"的定义 |
| `created_at` / `updated_at` | `TimestampMixin`（L59） | 排序用 |

索引：`ix_ingestion_jobs_paper_id`、`ix_ingestion_jobs_stage`、`ix_ingestion_jobs_created_at`（L413-417）。
注意：**没有独立的 `status` 列**，状态机就是 `stage`；API 层的 `RECEIVED`/`DUPLICATE`（`ingestion_service.py` L57-58）只是响应体的 `status` 字段值。

**`payload`（JSONB）的键**（散布于 `tasks.py` 与各入口）：

| 键 | 写入位置 | 含义 |
|---|---|---|
| `source_type` | 各入口 | `url` / `file` / `local_path` / `reindex` |
| `source` | URL 入口、reindex | 原始 URL（`ensure_valid_url` 校验） |
| `object_key` | `/ingest/files`（`api/ingestion.py` L199） | staging 对象键，`STORED` 后删除 |
| `local_path` / `cleanup_after` | `/ingest/dir`、`/ingest/compressed`（L444、L731） | 服务端本地文件；`cleanup_after=true` 才删 |
| `filename` / `content_type` / `size_bytes` / `file_kind` | 入口 | 标题与原文行元数据 |
| `duplicate` / `indexed` / `reason` | `resolve_duplicate`、`_finish_non_primary`（L814-816） | 结果标注；`indexed=false` 仅非主版本 |
| `reused_paper_id` / `match_method` | `_resolve_target_paper`（L748-749） | 复用了哪篇论文、靠什么匹配上的 |

**队列的内存结构**（`queue.py`，进程内，重启即丢）：

| 结构 | 类型 | 说明 |
|---|---|---|
| `_queue` | `asyncio.PriorityQueue[tuple[int, int, _Item]]` | 元组是 `(priority, seq, item)`，`seq` 保证同类 FIFO | 
| `_pending` | `dict[job_id, (kind, priority)]` | 等待中，插入序（`stats.queued_job_ids` 用它） |
| `_running` | `dict[job_id, kind]` | 正在跑 |
| `_seq` | `int` | 单调计数器，递增即 FIFO 次序（`enqueue` L192-193） |
| `_workers` | `list[asyncio.Task]` | 数量 = `self.concurrency = max(1, INGEST_CONCURRENCY)`（L96-97、L130-133） |

**GC 的数据视图**：`GcReport`（`housekeeping.py` L45-72）与 `JobRef`（L75-83，字段 `job_id/live/object_key/local_path`）。

## 4. 调用链（从入口到落地，逐跳，带函数名）

**A. 新建作业（以 `/ingest/files` 单文件为例）**

1. `api/ingestion.py:262 ingest_files` → `stage_and_queue`（L133）。
2. `stage_and_queue`：`is_pdf`/`ensure_size`（L154-157）→ `_stage_upload` 落 staging 并算 sha256（L161-165）→ `find_existing_paper` 判重（L171，命中则 `_discard_staging` 立即删 staging 并 `resolve_duplicate` 返回 `duplicate`）→ `create_job(source_type="file", payload={"object_key": ...})`（L193-200）→ `session.commit()`（L201）。
3. `job_queue.submit(session, job.id, KIND_INGEST, priority)`（L207）。
4. `queue.py:218 submit` → `ingest.mark_queued`（L230）：`UPDATE ... WHERE stage IN ('RECEIVED','QUEUED')` + `commit`（`ingestion_service.py` L411-422）→ `enqueue`（L234）。
5. `queue.py:165 enqueue`：去重检查（L186-188）→ 取 `seq`（L192-193）→ 同线程直接 `_hand_off`，跨线程 `loop.call_soon_threadsafe`（L202-207）→ `PriorityQueue.put_nowait`（L338）。
6. `queue.py:296 _worker` 取到元组 → `asyncio.to_thread(runner, job_id)`（L318），runner 来自 `DEFAULT_RUNNERS`（L63-67）。

**B. 流水线本体（`tasks.py:141 run_ingestion_job` → `210 _process_job` → `443 _run_pipeline`）**

> INDEXING 阶段的 chunk 文档由 `_index_rows`（`tasks.py:1141`）构造；其中**元数据部分**来自
> `app/search/snapshot.py::paper_metadata_snapshot`（单一来源，`scripts/refresh_index_metadata.py` 复用同一函数）。

1. `run_ingestion_job`：`SessionLocal()` → `ingest.get_job` → `_process_job`；异常路径 `session.rollback()` → `_record_failure`（L146-152）。
2. `_process_job`：`_advance_stage(DOWNLOADING, 10)`（L226-228，**commit**）→ `_load_source`（L230）→ 判重（L233-245：命中则 `resolve_duplicate` + commit + `_cleanup_source` + 返回 `PayloadOutcome(duplicate=True)`）→ `create_paper(status=PENDING)`（L250-257）→ `_store_source`（L259）→ `register_original_file`（L260-271）→ 写 `job.paper_id` / `stage=STORED` / `progress=30` + `commit`（L273-280）→ `_cleanup_source`（L284）→ `_run_pipeline`（L287）。
3. `_run_pipeline`：`paper.status=PROCESSING`（L468）→ 造降级 sink `Recorder`（L475-478，T7.3）→ `_advance_stage(PARSING, 45)`（L479）→ `download_bytes` + `extract_pages` + `detect_sections` + `merge_short_sections`（L472-474）→ 若 `dedupe and file_record is not None` 走 `_resolve_target_paper`（L477-486，非主版本在此 `_finish_non_primary` 并 return）→ `_reset_placeholder_title` / `_backfill_metadata` / `_restore_placeholder_title`（L488-490）→ `_upgrade_fingerprint`（L496-501，冲突则 `_discard_duplicate_paper` 并 return）→ `_advance_stage(CHUNKING, 60)`（L512）→ `chunk_document(..., on_degrade=degradations)` + `_replace_chunks` + `degradations.resolve(CHUNKING)`（L522-535，T7.3）→ `_advance_stage(EMBEDDING, 80)`（L537）→ `embedding_service.embed_texts` + `_write_embeddings`（L539-545）→ `_advance_stage(INDEXING, 95)`（L547）→ `ensure_index` + `delete_by_paper_id` + `bulk_index_chunks`（L549-555）→ `_mark_indexed`（L557）→ 写 `job.stage=COMPLETED` / `progress=100` / `finished_at` / `paper.status=INDEXED` + `commit`（L532-536）。

**C. 重试**：`api/jobs.py:44 retry_job` → `prepare_retry`（`FAILED → RECEIVED`，`rowcount` 闸门）→ `job_queue.submit(..., KIND_RETRY)`（L71）→ `tasks.run_retry_job`（L167）：读 `job.paper_id` 有值 → `run_reindex_job`；无值 → `run_ingestion_job`。
**D. 重建索引**：`api/papers.py:296` → `create_job(source_type="reindex")` + `job.paper_id = paper.id`（L290-299）→ `submit(..., KIND_REINDEX)`（L300）→ `run_reindex_job` → `reindex_paper` → `_run_pipeline(..., dedupe=False)`（`tasks.py` L111）。
**E. 重启恢复**：`main.py:48-52` → `job_queue.start()` → `job_queue.recover()`（`queue.py` L240）→ `ingest.recover_jobs`（L426-462）→ 重入队 / 标 `INTERRUPTED` → `housekeeping.start()`（启动即跑一次）。

## 5. 不变量与踩过的坑

**状态机全图**（左列 = 写该值的代码位置）：

```
RECEIVED(0) --create_job--> ingestion_service.py:246-253
   |
   | mark_queued（唯一允许 RECEIVED/QUEUED 之间转换的守卫）   ingestion_service.py:403-423
   v
QUEUED(0) --------------------------------------------------------+
   | _worker 取走，run_ingestion_job -> _process_job             |
   v                                                              |
DOWNLOADING(10)  _advance_stage，commit   tasks.py:229-231         |
   |                                                              |
   v                                                              |
STORED(30)   直接赋值 + commit（不经过 _advance_stage） tasks.py:276-283
   |
   v
PARSING(45)   _advance_stage     tasks.py:482
   v
CHUNKING(60)  _advance_stage     tasks.py:518
   v
EMBEDDING(80) _advance_stage     tasks.py:561
   v
INDEXING(95)  _advance_stage     tasks.py:543
   v
COMPLETED(100) 直接赋值 + commit tasks.py:550-555 → 轮询终止
```

旁路终态（均为 `COMPLETED`，不经过后半段）：判重命中（`ingestion_service.py:328-348`）、解析后指纹冲突弃单（`tasks.py:690-725`）、非主版本（`tasks.py:831-859`）。唯一真失败的终态是 `FAILED`（`ingestion_service.py:364`），另外两条写入路径是 `recover_jobs` 的 `INTERRUPTED`（L452-458）和 `run_reindex_job` 的"无 paper_id"（`tasks.py:130`）。

| 不变量 / 坑 | 代码证据 |
|---|---|
| **每个阶段边界必须 `commit`，不能只 `flush`**。否则轮询（另一个 session/连接）在整个流水线期间只能看到 `RECEIVED` 和最终值 | `_advance_stage` 的 docstring 与实现 L191-207；回归测试 `tests/test_job_progress.py:259`（独立 session 读到的中间态）、`:333`（引擎日志里每阶段一个 `COMMIT`）、`:392` |
| **但钩子覆盖不到 `DOWNLOADING` 之外的两处**：`STORED` 和 `COMPLETED` 是"直接赋值 + `session.commit()`"，不经 `_advance_stage`，因此 `stage_commit_hook` 只看到 4 个阶段（PARSING/CHUNKING/EMBEDDING/INDEXING） | `tasks.py:276-283`、`532-536` 对比 `226-228`；断言见 `tests/test_job_progress.py:318-323` |
| **`STORED` 是唯一分水岭**：之前失败 → `run_ingestion_job` 回滚，`paper_id` 为空、论文行与 `paper_files` 行都不落库，staging 对象**必须保留**（重试唯一的输入）；之后失败 → 论文行、原文对象已提交，staging 已删，重试走 reindex 语义 | `tasks.py:151-155`（回滚）、`273-280`（提交点）、`284`（提交后才删源）；测试 `tests/test_stored_cleanup.py:206-222`（STORAGE_FAILED 时 `deleted == []`）与 `:228-246`（STORED 后失败时 staging 已删、`paper_id` 仍在）；**兜底 GC 也遵守这条**：`STORED` 之前失败的行（`paper_id IS NULL`）其 staging 保留 `STAGING_RETRY_GRACE_HOURS=72` 小时（2026-09-22 修，见 §5） |
| **删源一律在 `commit` 之后**。顺序颠倒会让失败的作业失去唯一输入 | `tasks.py:283`（commit）→ `284`（`_cleanup_source`） |
| **判重命中立即删**，三处：流水线内判重、`/ingest/files` 暂存后判重、压缩包解包后判重。暂存副本唯一的用途（算 sha256）已经用完 | `tasks.py:247`；`api/ingestion.py:175-176`；`api/ingestion.py:716-717` |
| `_cleanup_source` 只删两类东西：`payload["object_key"]`（staging）与 `cleanup_after=true` 的本地文件；其余本地文件（`/ingest/dir` 导入的原始 PDF）**永不删** | `tasks.py:403-421`、`346-372`；测试 `tests/test_local_source.py:195` |
| **`mark_failed` 的 docstring 与代码不符**：docstring（L359-363）称"`stage`/`progress` 保留失败发生的阶段"，代码 L364 实际把 `stage` 写成 `FAILED`，只有 `progress` 保留 | 代码 `ingestion_service.py:364`；测试 `tests/test_job_progress.py:447-449`（`stage == "FAILED"` 且 `progress == PROGRESS_EMBEDDING`）。`README.md:299` 的同一说法同样不准 → 以代码为准 |
| **`mark_queued` 是单向闸门**：只接受 `RECEIVED`/`QUEUED`，运行中的作业绝不会被倒回 `QUEUED` | `ingestion_service.py:411-418`；测试 `tests/test_ingest_queue.py:214-227` |
| **`prepare_retry` 的 `rowcount == 1` 是重试并发闸门**：双击/并发调用只有一个能把作业翻出 `FAILED`，不会起两个 worker | `ingestion_service.py:384-400`；测试 `tests/test_job_retry.py:82-90` |
| **`enqueue` 幂等**：已在 `_pending` 或 `_running` 中则返回 `False` 且不重复入队 | `queue.py:186-188`；测试 `tests/test_ingest_queue.py:167-189` |
| **队列未启动时内联执行**（一次性脚本、单测），返回值 `False`；"绝不静默丢活" | `queue.py:195-200`、`340-346`；测试 `tests/test_ingest_queue.py:149-157` |
| **重试路由看 `paper_id`，不看 `stage`**。`STORED` 之后失败必有 `paper_id`，故能复用 MinIO 原文与论文行 | `tasks.py:182-191`；测试 `tests/test_job_retry.py:114-175` |
| **`dedupe=False` 只用于 reindex**。若 reindex 也做指纹弃单，会把自己（已索引的存活论文）连 chunk 带索引文档一起清掉 | `tasks.py:114` 与 `_run_pipeline` docstring L452-466；`_upgrade_fingerprint` 的 `discard_on_conflict` L572-588、L601-608；测试 `tests/test_fingerprint_priority.py:338` |
| `dedupe=False` 还顺带跳过 `_resolve_target_paper`（条件是 `dedupe and file_record is not None`，reindex 两者都不满足）→ reindex 不会走非主版本分支 | `tasks.py:482-482`；`reindex_paper` 未传 `file_record`（L111） |
| **`stage` 无 DB 级约束**，是 `String(32)`；写错值不会报错 | `app/db/models.py:453-455` |
| **同名常量两处定义**：`tasks.py:56-63` 定义了自己的 `STAGE_DOWNLOADING`/`STAGE_STORED`/`PROGRESS_DOWNLOADING`/`PROGRESS_STORED`，但这两阶段实际用的是 `ingest.*`（L227、L274-275）；两处值相同，暂无行为差异，但改一处会漏另一处 | `tasks.py:56-63` vs `226-228`、`273-275` |
| 队列是**进程内**的：只有 `--workers 1` 的前提下成立 | `queue.py:20-24`；`README.md:135-137` |
| `stop()` 不会杀线程里的流水线；该作业行停在中间态，下次启动被 `recover` 标 `INTERRUPTED` | `queue.py:139-154` |

## 6. 配置项（键 → 默认值 → 作用 → 出处文件:行）

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `INGEST_CONCURRENCY` | 2 | 同时运行的流水线数 = worker 协程数；`<=0` 启动即报错 | `core/config.py:144`（校验 L190-195）→ `queue.py:96-97`、`130-133` |
| `INGEST_DOWNLOAD_TIMEOUT` | 120.0 | URL 下载超时（httpx） | `config.py:133` → `ingestion_service.py:476` |
| `INGEST_MAX_FILE_MB` | 100 | 单文件上限；`max_file_bytes()` 换算 | `config.py:134` → `ingestion_service.py:115-117`、`138-144` |
| `INGEST_UPLOAD_CONCURRENCY` | 2 | 在途**上传请求**数上限（`429 + Retry-After: 2`） | `config.py:146` → `upload_admission.py:71-79` |
| `INGEST_QUEUE_HIGH_WATERMARK` | 50 | 处理积压达到该深度时**只拒多文件**请求；0 关闭 | `config.py:150-152` → `upload_admission.py:128-136` |
| `INGEST_MAX_FILES_PER_REQUEST` | 20 | 单请求文件数（超出 `422`） | `config.py:154-156` → `api/ingestion.py:286-294` |
| `INGEST_MAX_REQUEST_MB` | 200 | 单请求总字节（超出 `413`） | `config.py:158` → `api/ingestion.py:296-309` |
| `INGEST_LOCAL_ROOTS` | `""` | `/ingest/dir` 白名单；空 = 端点 404 | `config.py:164` |
| `INGEST_ARCHIVE_*` | `500 MB` / `2000` / `5000 MB` / `100` / `""` / `24h` | zip 大小、条目数、解压总量、压缩比、解包目录、TTL | `config.py:168-180` |
| `INGEST_GC_INTERVAL_S` | 300 | housekeeping 周期；启动另跑一次 | `config.py:184` → `housekeeping.py:324`、`:353-364`、`main.py:52` |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` | `BAAI/bge-m3` / 1024 | 写入 chunk 行、论文行与索引文档 | `config.py:76-77` → `tasks.py:279-280`、`959-960`、`1025-1026` |

优先级阈值（`1 文件=交互、≥2=批`）**没有配置项**，是入口函数里的硬编码判断：`api/ingestion.py:311-314`（files）、`475-479`（dir）、`657-661`（compressed）、`539`（单文件固定交互）。重试与 reindex 共用同一个 `INGEST_CONCURRENCY` 上限，重试走 `KIND_RETRY`（`api/jobs.py:71`），reindex 走 `KIND_REINDEX`，优先级取默认值 `PRIORITY_INTERACTIVE`（`queue.py:59`）。

## 7. 测试位置与覆盖（tests/xxx.py → 覆盖什么）

| 测试文件 | 覆盖什么 |
|---|---|
| `tests/test_ingest_queue.py` | 并发上限（L74、L84）、FIFO（L91）、`stats` 的 running/queued（L98）、失败作业不杀 worker（L132）、未启动时内联（L149）、未知 kind 报错（L160）、幂等入队（L167）、`kind_for_payload` 路由（L192）、`mark_queued` 的接受/拒绝（L201、L214）、`recover_jobs` 分类（L230）、recover 入队（L284）、`INTERRUPTED` 是已知码（L305） |
| `tests/test_queue_priority.py` | 优先级常量次序（L62）、交互作业插队到等待的批作业之前（L67）、同类 FIFO（L81）、默认交互（L95）、`queued_high`/`queued_low` 分类（L110）、`depth` 只算等待中（L137、L151） |
| `tests/test_job_progress.py` | 运行中能被独立 session 观测到中间阶段（L251）、每阶段一个 `COMMIT`（L329）、每次转换都可见（L388）、**失败保留失败阶段的进度**（L420）、`serialize_job`/`JobOut` 字段（L453） |
| `tests/test_job_retry.py` | `prepare_retry` 重置字段（L67）、只能被认领一次（L82）、非 `FAILED` 拒绝（L93）、未知 id 拒绝（L107）、STORED 后失败 → 续跑成功（L114）、重试再失败 → 新错误码（L133）、STORED 前失败 → 整体重跑（L155）、作业消失是无操作（L178） |
| `tests/test_stored_cleanup.py` | STORED 时删 staging（L110 前后）、永久副本字节一致（L118）、URL 作业无可删（L133）、判重竞态也删 staging（L161）、**存储失败保留 staging + `STORAGE_FAILED`**（L206）、STORED 后失败不再需要 staging（L225）、`cleanup_after` 删解包文件（L246） |
| `tests/test_failure_classification.py` | 错误码集合精确匹配（L50）、每种码一个用例（L58-145）、消息保留原始异常与 cause 链（L148、L154）、空文本 PDF → `NO_TEXT_LAYER`（L191、L201） |
| `tests/test_deletion.py` | 删除顺序：先清索引、再删对象、最后软删（L70）；任一步失败则不标记删除且可重试（L90、L103、L117）。与流水线的交集是"论文行/对象/索引三者一致"这一不变量 |

相关但不在必读清单里的对照：`tests/test_local_source.py`（`local_path` 各失败码、`cleanup_after`）、`tests/test_ingest_files.py` / `test_ingest_file.py` / `test_ingest_dir.py` / `test_ingest_compressed.py`（入口与优先级）、`tests/test_upload_admission.py`、`tests/test_upload_gc.py`、`tests/test_fingerprint_priority.py:338`（reindex 保住旧指纹）。

## 8. 未做 / 已知缺口

- **无外部队列**：队列在进程内，横向扩 worker/多副本必须先换外部队列（当前明确不做）。`queue.py:20-24`；`README.md:135-137`；`docs/progress/project.md:583`。
- **重启恢复只处理 `ingestion_jobs` 行，不做 MinIO/OpenSearch 对账**：`recover_jobs` 不检查"论文行有、对象缺失"这类不一致；对账仍是 `scripts/purge_deleted.py`。`ingestion_service.py:426-462`；`docs/progress/project.md:584`。
- **`INTERRUPTED` 不会自动重跑**：恢复只是把作业标成 `FAILED`，重新驱动必须人工 `POST /api/jobs/{id}/retry`。`ingestion_service.py:452-458`、`api/jobs.py:53-59`。
- **`run_reindex_job` 的"无 paper_id"失败不带错误码**：`mark_failed` 未传 `code`，于是 `stage=FAILED` 而 `error_code=NULL`，与其它失败不一致。`tasks.py:129-132`。
- **没有取消接口**：`QUEUED` 作业无法取消，只能在跑完后删除论文；本文未发现相关端点或测试（未确认是否有意为之）。
- **`progress` 的粒度**：`STORED → PARSING` 之间（下载+解析）没有中间反馈，大 PDF 会长时间停在 30/45；`PARSING` 内部无进度。
- **`stage` 无枚举约束**：DB 层 `String(32)` 无 CHECK，写错值不会被拦住。`app/db/models.py:453-455`。
- **阶段常量重复定义**：`tasks.py:56-63` 与 `ingestion_service.py:33-64` 各有一套 `DOWNLOADING`/`STORED` 常量（值相同、互不引用），后续改动有漏改风险。
- **`payload.indexed` / `payload.reason` 不上 API**：非主版本作业只在 `payload` 里留痕，`serialize_job` 不暴露，客户端只能看到 `COMPLETED`。`tasks.py:839-842`、`ingestion_service.py:294-307`。
- **队列统计不做持久化**：`stats()` 是内存快照，重启后归零，没有历史/告警。`queue.py:274-291`。
- **housekeeping 只删文件、不改作业状态**（明确的设计约束）：孤儿与终态作业的 staging 都删，`live` 的定义是 `finished_at IS NULL 且 stage 不在 (COMPLETED, FAILED)`。**例外（2026-09-22 修）**：`STORED` 之前失败（`stage=FAILED` 且 `paper_id IS NULL`）的行，其 staging 字节自 `finished_at` 起保留 `STAGING_RETRY_GRACE_HOURS=72` 小时——重试要重读 `payload["object_key"]`，原先 GC 在下一轮（默认 300s）就删掉，使上一行的"必须保留"落空（重启后留下的 `INTERRUPTED` 作业正是这种状态）。`housekeeping.py:193-277`（`run_gc`）、`:145`（`live`）、`:148`（`retryable`）、`:178-191`（`_keeps_staging`）；测试 `tests/test_upload_gc.py`（保留 / 超期回收 / 只有 FAILED 享受宽限三条）。
