# 上传入口与文件处理（多文件 / 目录 / zip）

| 项 | 内容 |
|---|---|
| 状态 | 依据 commit 54048a3 的工作树实测（2026-09-22） |
| 关键文件 | `app/api/ingestion.py`（750 行）、`app/services/upload_admission.py`（173）、`app/services/local_scan.py`（357）、`app/services/archive_service.py`（462）、`app/services/object_storage.py`（498）、`app/workers/housekeeping.py`（341）、`app/schemas/ingestion.py`（194）、`app/core/config.py` §ingestion 段 |
| 相关文档 | `docs/architecture/02-ingestion-pipeline.md`（流水线状态机与队列调度）、`AGENTS.md` §3.8、`README.md` §3.3、`docs/progress/project.md` §16 |

## 1. 职责边界（做什么 / 不做什么）

**做**：把用户给的一批文件（multipart 多文件 / 服务端目录 / zip 压缩包 / 单个 URL）变成 `ingestion_jobs` 里一条条 `QUEUED` 的作业行，并把字节安全地落到 MinIO staging 或临时解包目录。本文覆盖 **HTTP 入口契约、staging 写入与边写边算 sha256、目录扫描与安全、zip 解包与安全、上传准入、清理 GC**。

**不做**：不执行流水线（不解析、不 embedding、不建索引）；不新增表 / 迁移 / 阶段 / 错误码——五个入口共用同一状态机与同一条队列（`app/api/ingestion.py:1-19`）。作业被 `submit` 之后的调度、状态推进、`STORED` 后的解析与索引属于 `02-ingestion-pipeline.md`。

- 端点不自己跑管道：`POST` 只把作业停在 `QUEUED` 后交给 `app.workers.queue`（`app/api/ingestion.py:16-18`）。
- `/ingest/dir` 与 `/ingest/compressed` 的读取方是**服务端自己**；只有 `/ingest/files`（与 `/ingest/file`、`/ingest/compressed` 的压缩包本体）承载客户端字节（`app/services/upload_admission.py:1-21`）。
- 本仓库唯一新增的文件系统访问面是 `/ingest/dir`，其安全规则集中在 `local_scan.py`（`app/services/local_scan.py:1-23`）。

## 2. 关键文件与函数（文件 → 函数/类 → 作用，带行号）

| 文件 | 函数 / 类 | 作用 |
|---|---|---|
| `app/api/ingestion.py` | `router` | `/api/papers` 前缀 + `require_api_key` 依赖（59-63） |
| | `_busy` | 构造 `429` + `Retry-After` 头（66-72） |
| | `_rejected` | 把单文件异常转成 `rejected` 结果行，经 `classify_failure` 定 `error_code`（75-92） |
| | `_stage_upload` | 单 part 流式写 staging 并哈希（95-130） |
| | `stage_and_queue` | 校验 → staging → 判重 → 建作业 → 入队，单 part 全流程（133-213） |
| | `_discard_staging` | best-effort 删除 staging 对象（216-221） |
| | `summarize` | 汇总 `accepted`/`duplicate`/`rejected` 计数（224-235） |
| | `ingest_url` / `ingest_files` / `ingest_dir` / `ingest_file` / `ingest_compressed` | 五个入口（238-254 / 257-353 / 356-510 / 513-564 / 567-689） |
| | `_register_extracted` | zip 条目判重、建 `local_path` 作业或丢弃（692-747） |
| `app/services/object_storage.py` | `build_staging_key` | staging 键命名 `uploads/<request_id>/<index>-<name>.pdf`（262-273） |
| | `safe_filename` | 只留 basename、清洗特殊字符、截断 120（245-259） |
| | `_HashingReader` | 包装二进制流，边读边 `sha256`，只拦 `read`/`readinto`（72-106） |
| | `upload_stream_hashed` | `put_object(length=...)` 直传并在返回时带 `sha256`（276-326） |
| `app/services/upload_admission.py` | `UploadAdmission` | 在途计数 + 积压水位判定，`threading.Lock` 保证计数精确（62-147） |
| | `should_throttle_batch` | 水位判断，仅由多文件请求调用（128-136） |
| | `get_admission` / `reset` | 进程级单例（153-164） |
| `app/services/local_scan.py` | `ensure_allowed` | 白名单 + `realpath` 收敛 + 目录存在性（124-145） |
| | `is_within` / `_norm` | `realpath`+`normcase` 形式的包含判断（110-121） |
| | `_walk` | 遍历，剪符号链接 / junction / 隐藏 / 临时文件（201-235） |
| | `is_link` | 同时判 symlink 与 Windows junction（186-198） |
| | `scan` | 先 stat 限大小，再流式哈希，超限计入 `skipped`（238-332） |
| `app/services/archive_service.py` | `save_stream` | 压缩包本体落临时文件并限 `INGEST_ARCHIVE_MAX_MB`（186-211） |
| | `unsafe_reason` | zip-slip / 符号链接 / 设备文件逐条判据（217-234） |
| | `extract_archive` | 中央目录三重上限 → 逐条解包（250-343） |
| | `_write_entry` | 单条目边写边查每文件上限（346-370） |
| | `remove_file`/`prune_dir`/`prune_tree`/`cleanup_dir` | 删除与空目录剪枝（376-431） |
| `app/workers/housekeeping.py` | `run_gc` | 一次收集：孤儿/终态 staging、过期解包目录、残留压缩包（183-260；`STORED` 之前失败的行按 `STAGING_RETRY_GRACE_HOURS=72h` 保留其 staging，2026-09-22） |
| | `load_job_refs` | 读作业行，定义 `live = finished_at is None and stage not in (COMPLETED, FAILED)`（103-145；`retryable = stage == "FAILED" and paper_id is None` 在 `:138`） |
| | `Housekeeping` | 启动跑一次 + 每 `INGEST_GC_INTERVAL_S` 循环（280-347） |

## 3. 数据结构（表/字段/索引，或内存结构）

响应体（`app/schemas/ingestion.py`）：

| 模型 | 关键字段 | 行号 |
|---|---|---|
| `IngestRequest` | `source_type`（只允许 `url`）、`source`（必须 http(s)），`extra="forbid"` | 15-37 |
| `IngestAccepted` | `job_id`、`paper_id`、`status`、`duplicate`、`stage` | 40-51 |
| `IngestFileResult` | `filename`、`entry`（压缩包内名）、`status`、`job_id`、`paper_id`、`error_code`、`error_message`、`size_bytes` | 54-72 |
| `IngestFilesAccepted` | `request_id` + `accepted`/`duplicate`/`rejected` + `results` | 75-90 |
| `IngestDirRequest` | `root`、`glob`（默认 `**/*.pdf`）、`recursive`、`limit`（≥1）、`dry_run` | 93-110 |
| `IngestDirJob` | 同 `IngestFileResult` 外加 `relative`、`path` | 113-132 |
| `IngestDirAccepted` | `matched`/`accepted`/`duplicate`/`rejected`/`skipped` + `jobs` | 135-155 |
| `IngestCompressedAccepted` | `entries_total`/`entries_ignored`/`entries_rejected` + 逐条 `results` | 158-179 |

`status` 三个常量 `accepted`/`duplicate`/`rejected` 见 `app/schemas/ingestion.py:9-12`；不变量 `accepted + duplicate + rejected == len(results)`（同文件 76-90）。

内存结构：

| 结构 | 内容 | 行号 |
|---|---|---|
| `ScannedFile` | `path`/`relative`/`size_bytes`/`sha256`/`reason`/`error_code`（判据失败时 `sha256=None`） | `local_scan.py:74-87` |
| `ScanResult` | `files` + `skipped`，`matched = len(files) + skipped` | `local_scan.py:90-104` |
| `ArchiveLimits` | `max_files`/`max_uncompressed_bytes`/`max_ratio`/`max_file_bytes`，由 settings 组装 | `archive_service.py:77-97` |
| `ExtractedEntry` / `ExtractResult` | 解出的 PDF 与 `ignored`/`rejected`/`reasons` 计数 | `archive_service.py:100-121` |
| `UploadAdmission` | `limit`、`high_watermark`、`_in_flight`、`_lock`、`_depth_provider` | `upload_admission.py:62-84` |
| `GcReport` / `JobRef` | 收集结果四桶 + `errors`；作业引用含 `object_key`/`local_path` | `housekeeping.py:59-102` |

作业 `payload`（由 `create_job` 合并，`ingestion_service.py:213-256`）：`source_type`（`url`/`file`/`local_path`）、`object_key`（staging 键）、`local_path` + `cleanup_after`（服务端本地文件）、`filename`/`content_type`/`size_bytes`。`local_path` 必须是绝对路径，否则 `UnsupportedSource` → 422（`ingestion_service.py:179-194`）。

## 4. 调用链（从入口到落地，逐跳，带函数名）

| 入口 | 请求契约 | 成功 | 失败码（触发处行号） |
|---|---|---|---|
| `POST /api/papers/ingest` | JSON `IngestRequest`（URL） | 202 `IngestAccepted`（238-254） | 422 非 http(s) URL（246-249）；422 字段非法（schema 层） |
| `POST /api/papers/ingest/file` | multipart `file` | 202 `IngestAccepted`（513-564） | 429 并发/水位（530-542）、422 单文件被拒（544-548）、503 作业行消失（550-555） |
| `POST /api/papers/ingest/files` | multipart `files` 可重复 | 202 `IngestFilesAccepted`，逐 part 结果（257-353） | 422 文件数超限（286-294）、413 请求总量超限（296-309）、429 积压（316-317）/在途（332-333） |
| `POST /api/papers/ingest/dir` | JSON `IngestDirRequest` | 202 `IngestDirAccepted`（356-510） | 404 未启用（379-380）、403 越界（381-382）、404 目录不存在（383-384） |
| `POST /api/papers/ingest/compressed` | multipart `file`（zip） | 202 `IngestCompressedAccepted`（567-689） | 429 积压（589-591）、415 非 zip（593-602）、422 解包上限（629-634）、500 其他解包故障（635-641） |

`/ingest/files`（`ingest_files` 257-353）：

1. 文件数 / 总量闸门：`len(uploads) > INGEST_MAX_FILES_PER_REQUEST` → 422（286-294）；把各 part **已声明**的 `upload.size` 求和与 `INGEST_MAX_REQUEST_MB` 比较 → 413（296-309，发生在任何 staging 写入之前）。
2. 批/交互分类：`batch = len(uploads) > 1`，据此选 `PRIORITY_BATCH` 或 `PRIORITY_INTERACTIVE`（311-314）。
3. 积压闸门：`batch and admission.should_throttle_batch()` → 429（316-317）。
4. `with admission.slot():`（321-331）——在途槽位覆盖整批 part 的循环；`AdmissionRejected` → 429（332-333）。
5. 每个 part 走 `stage_and_queue`（133-213）：校验空文件 / `is_pdf` / `ensure_size`（151-159）→ `build_staging_key`（161）→ 线程池 `_stage_upload`（163-165）→ `find_existing_paper`（171）→ 命中即 `_discard_staging` + `resolve_duplicate`（172-191）→ 否则建 `source_type="file"` 作业、`payload={"object_key": staging_key}`、commit（193-201）→ `job_queue.submit(..., KIND_INGEST, priority)`（207）。
6. 任一 part 失败只返回该行的 `rejected`（166-168、202-205），其余 part 继续；最后 `summarize`（345-353）。

`/ingest/file`（`ingest_file` 513-564）：`index=1`、`PRIORITY_INTERACTIVE`，同样是 `stage_and_queue`；唯一差别是契约——单文件被拒时把 `error_message` 提升为请求级 422（544-548），而不是逐文件结果行。

`/ingest/dir`（`ingest_dir` 361-510）：`local_scan.ensure_allowed(payload.root, settings.local_roots)`（378）→ 三种异常映射 404/403/404（379-384）→ `local_scan.scan(root, glob, recursive, limit, max_bytes=ingest.max_file_bytes())`（386-392）→ 逐条：有 `reason` 记 `rejected`（397-409）；`find_existing_paper(item.sha256)` 命中记 `duplicate`（411-423）；`dry_run` 记 `accepted` 但不建作业（425-435）；否则 `create_job(source_type="local_path", payload={"local_path": ...})`（437-446）→ 全部建成后按数量选优先级逐个 `submit`（474-481）。

`/ingest/compressed`（`ingest_compressed` 572-689）：水位闸门（589-591）→ 读 8 字节判魔数（593-595）→ `save_stream` 落临时文件（613-615）→ `extract_archive`（617-622）→ `finally` 删压缩包本体（623-625）→ 对 `extracted.entries` 逐条 `_register_extracted`（645-651）→ `prune_tree(dest)` 收掉空骨架（655）→ 提交 `PRIORITY_BATCH`（657-661）。

staging 键与哈希（`stage_and_queue` → `_stage_upload` → `object_storage`）：

- 键：`uploads/<safe_request_id>/<index>-<safe-filename>.pdf`（`object_storage.py:262-273`，`UPLOAD_PREFIX="uploads"` 见 40）；同名两文件靠 `index` 区分，客户端文件名经 `safe_filename` 洗净（245-259）。
- 有声明大小时：`upload_stream_hashed(staging_key, upload.file, length=declared_size, ...)`（119-125）。MinIO 从 `_HashingReader`（72-106）拉数据，`read`/`readinto` 每块顺手 `digest.update`，`put_object` 返回时 `hexdigest` 已就绪（295-307）——**不整块读内存、不二次读**。
- 无声明大小时退化为整份 `upload.file.read()` 后 `upload_bytes` + `compute_sha256`（106-117），仍受 `INGEST_MAX_FILE_MB` 约束。
- 空文件在两端都被拒（107-109、126-129）。

## 5. 不变量与踩过的坑

1. **一个坏 part 不失败整请求**：每个 part 独立 try/except（`ingestion.py:166-168`、`202-205`）、zip 每条目独立（`692-747`）、目录每个文件独立（`447-461`）。
2. **准入在写字节之前**：429/413/422 都在 `admission.slot()` 或 staging 之前判定（296-317）；测试断言此时 `storage.uploads == []`（`tests/test_ingest_files.py:322`、`334`）。
3. **积压水位只拒多文件**：`should_throttle_batch()` 只在 `batch=True` 时被调用（316），单文件豁免（`upload_admission.py:128-136` 注释明说“一个人等一个答案”）；在途并发槽则不分单/多文件（321、533、612）。
4. **判重靠内容哈希**：`/files` 必须把重复字节整份传完才能知道重复，随后立即删除已建的 staging 对象（172-175）；`/ingest/dir` 有传输前预哈希（`scan` 300-319）。
5. **`dry_run` 零副作用**：不建作业、不入队（425-435、474 的 `and not payload.dry_run`）。
6. **必须用 `local_scan.is_link()` 判链接**：`os.walk(followlinks=False)` 仍会进入 Windows 目录 junction，`Path.is_symlink()` 对 junction 返回 False；junction 是非特权用户（`mklink /J`）唯一能造的 reparse point，因此是最现实的逃逸口（`local_scan.py:186-198`、`AGENTS.md:227-230`）。本机实测：`os.path.isjunction` 在项目解释器 3.12.10 上存在（`pyproject.toml:5` 要求 `>=3.12,<3.13`）。
7. **zip bomb 在建条目前判定**：三上限全部取自 `archive.infolist()` 的中央目录（`archive_service.py:271-293`），超限时 `dest` 里一个字节都没写；条目级上限在写入过程中再兜一层（346-370）。
8. **zip-slip 双保险**：`unsafe_reason` 逐条拒（217-234：空名、绝对路径、盘符路径、`..`、符号链接、设备文件），写出前再用 `is_within(target, dest)` 复核一次（315-320）。
9. **嵌套压缩包不递归**（304-306，计入 `entries_ignored`）；`.pdf` 后缀只是候选，最终靠 `%PDF` 魔数（前 1 KiB，`PDF_MAGIC_WINDOW=1024`，173-180），无魔数即 `UNSUPPORTED_TYPE` 拒收并删文件（697-707）。
10. **GC 只删文件、不碰作业行**：`run_gc` 只对作业表做 `select`（85-115），测试用真实作业行验证 stage 与 `error_code` 不变（`tests/test_upload_gc.py:270-287`）；失败只进 `GcReport.errors`，绝不抛（156-159、215-222）。
11. **staging 没有宽限期**：GC 判据只有 liveness（174-181），没有年龄检查；孤立即刻被删。历史事故：staging 只写不删，13 个作业留下 9 个对象 49.8 MB（`housekeeping.py:5-10`，该数字来自代码注释，本会话未复测）。
12. **`STORED` 是删除的唯一检查点**：`STORED` 之前失败必须保留 staging 才能重试（`AGENTS.md:224`），存储成功后才 `_cleanup_source`（`tasks.py:273-283`）。

## 6. 配置项（键 → 默认值 → 作用 → 出处文件:行）

| 键 | 默认 | 作用 | 出处 |
|---|---|---|---|
| `INGEST_MAX_FILE_MB` | 100 | 单文件上限，超限 `OVERSIZED` / 目录扫描 `OVERSIZED` | `config.py:121`；`ingestion_service.py:115-117` |
| `INGEST_UPLOAD_CONCURRENCY` | 2 | 在途上传请求上限，超出 429 | `config.py:133`；`upload_admission.py:71-79` |
| `INGEST_QUEUE_HIGH_WATERMARK` | 50 | 积压水位；只拒多文件请求，0 = 关闭 | `config.py:137-139`；`upload_admission.py:80-81`、`134-136` |
| `INGEST_MAX_FILES_PER_REQUEST` | 20 | 单请求文件数上限 → 422 | `config.py:141-143`；`ingestion.py:286-294` |
| `INGEST_MAX_REQUEST_MB` | 200 | 单请求总字节上限 → 413 | `config.py:145`；`ingestion.py:296-309` |
| `INGEST_LOCAL_ROOTS` | 空 | 目录导入白名单（`;`/`,`/`os.pathsep` 分隔，realpath 归一化去重）；空 = 端点 404 | `config.py:151`、`241-265`；`local_scan.py:131-134` |
| `INGEST_ARCHIVE_MAX_MB` | 500 | 压缩包本体上限 → 422 | `config.py:155`；`archive_service.py:186-211` |
| `INGEST_ARCHIVE_MAX_FILES` | 2000 | 解包条目数上限（zip bomb #1） | `config.py:157`；`archive_service.py:276-280` |
| `INGEST_ARCHIVE_MAX_UNCOMPRESSED_MB` | 5000 | 解压总量上限（#2） | `config.py:159-161`；`archive_service.py:281-286` |
| `INGEST_ARCHIVE_MAX_RATIO` | 100 | 压缩比上限（#3），0 关闭 | `config.py:163`；`archive_service.py:287-293` |
| `INGEST_ARCHIVE_TMP_DIR` | 空 | 解包目录（空 = 系统 temp） | `config.py:165`；`archive_service.py:127-135` |
| `INGEST_ARCHIVE_TTL_HOURS` | 24 | 解包目录与残留压缩包保留上限 | `config.py:167`；`housekeeping.py:198`、`:234-258` |
| `INGEST_GC_INTERVAL_S` | 300 | GC 间隔（启动必跑一次） | `config.py:171`；`housekeeping.py:307`、`323-334` |
| `INGEST_CONCURRENCY` | 2 | 并发流水线数（处理腿，本文只引用） | `config.py:127`；见 `02-ingestion-pipeline.md` |

`Retry-After` 固定 2 秒：`upload_admission.RETRY_AFTER_SECONDS = 2`（`upload_admission.py:36`）。

## 7. 测试位置与覆盖（tests/xxx.py → 覆盖什么）

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_ingest_files.py`（402） | 单/多 part 入队与优先级（142-193）、同名不冲突的 staging 键（194-207）、非 PDF 与空 part 逐行拒（208-242）、存储失败只影响该 part（243-257）、重复内容不建新论文与重复作业已 COMPLETED（258-309）、422/413（310-336）、单文件超限逐 part 拒（337-354）、429 与槽位释放、水位只拒多文件（359-402） |
| `tests/test_ingest_file.py`（162） | 旧端点响应形状回归（54-73）、staging 键含 request_id（74-84）、payload 指向 staging（85-99）、交互优先级（100-109）、非 PDF/空/超限 422（110-135）、饱和 429（136-148）、判重（149-161） |
| `tests/test_ingest_dir.py`（375） | 目录导入建 `local_path` 作业（152-174）、隐藏/临时/非 PDF 跳过（175-184）、`recursive`/`glob`/大小写/`limit`（185-213）、优先级（214-222）、判重（223-238）、超限与不可读逐文件拒（239-268）、`dry_run`（269-288）、404/403/`..` 逃逸（289-316）、junction/symlink 逃逸与根内链接不遍历、链接 PDF 跳过（317-356）、多白名单（363-374）；链接构造在无权限时 `pytest.skip`（112-132） |
| `tests/test_bulk_ingest_dir.py`（278） | 客户端脚本 `scripts/bulk_ingest_dir.py` 的纯函数：候选判定、glob、遍历与 manifest（45-123）、`Retry-After` 优先于退避且封顶（124-159）、续跑（160-204）、汇总与失败分组（205-254）、`dry_run` 不发请求（255-266） |
| `tests/test_ingest_compressed.py`（356） | 解包入队与临时目录消失（126-164）、空 zip（165-173）、判重（174-188）、无 `%PDF` 魔数拒（189-203）、`..`/绝对路径拒（204-216）、symlink/设备条目拒（217-238）、嵌套包不递归（239-256）、压缩比/条目数/解压总量/包本体超限 422（257-298）、单条目超限（299-313）、7z 与纯文本 415、空上传 415（314-334）、积压 429 与槽位释放（340-356） |
| `tests/test_upload_admission.py`（193） | 槽位额度与释放、不越界为负（31-59）、上下文管理器异常路径（60-87）、多线程不超限（88-142）、水位与 0 关闭、`snapshot`、默认值取自 settings、单例复用（143-193） |
| `tests/test_upload_stream.py`（235） | `_HashingReader` 哈希正确、增量读、`readinto`、属性透传（112-163）、`upload_stream_hashed` 摘要与大小、**不缓冲整包**（`GuardedStream` 拒绝无界 `read`，28-51、178-192）、content_type/metadata 透传、存储异常包装、与独立哈希一致（164-234） |
| `tests/test_upload_gc.py`（438） | 孤儿/终态 staging 删除、live 作业保留、半写对象删除、**`STORED` 之前失败的作业其 staging 保留 / 超过 72h 回收 / 非 FAILED 终态不享受宽限（158-214，2026-09-22）**、解包目录按 TTL 与 liveness、残留压缩包过期删除、二次 pass 幂等、删除/列举失败只记录不抛、作业行不被改动、报告序列化、循环立即跑一次且可停、异常不杀循环、`start` 幂等、单例 |
| `tests/test_local_source.py`（393） | `local_path` 作业落地与 `cleanup_after` 删文件/剪空目录、判重不建新论文（175-266）、缺文件/是目录/超限/非 PDF/空文件失败码（267-342）、`create_job` 的 local payload 校验（343-392） |

## 8. 未做 / 已知缺口

- **`UploadAdmission.snapshot()` 没有接到任何端点**：全仓库只有定义处（`upload_admission.py:138-147`）与测试引用，`GET /api/jobs/queue` 只回 `job_queue.stats()`（`app/api/jobs.py:30`，字段见 `app/schemas/job.py:38-52`），看不到 `in_flight` 与水位状态。
- **`/ingest/dir` 完全不受准入约束**：端点内没有 `get_admission` 调用（356-510），一次请求可建最多 `limit`（默认 2000）个作业，不受 `INGEST_QUEUE_HIGH_WATERMARK` 限制。
- **413 只统计“已声明大小”的 part**：`declared_total` 只累加 `upload.size` 为正整数的部分（297-301）；若解析器拿不到大小，回落路径把该 part 整份读进内存（107-110），最坏情况 20 × `INGEST_MAX_FILE_MB` 同时在内存。
- ~~**`FAILED` 不算 live**~~ **已修（2026-09-22）**：`STORED` 之前失败（`paper_id IS NULL`）的作业，其 staging 对象现在保留 `STAGING_RETRY_GRACE_HOURS=72` 小时供重试（原先下一轮 GC 就删，重试必然取不到字节）；覆盖 `tests/test_upload_gc.py::test_staging_bytes_of_a_pre_stored_failure_are_kept_for_the_retry` 等三条。
- **解包前的磁盘空间守卫未做**（`docs/progress/project.md` §16 已知遗留）：压缩包路径峰值 ≈ 压缩包 + 解压总量 + 正式副本，三重上限只约束后两者。
- **`/ingest/compressed` 也吃上传准入**（占在途槽位 + 受水位限制），比计划更严，属有意选择（`docs/progress/project.md` §16，代码见 `ingestion.py:589-591`、`612`）。
- **队列是进程内的**：`--workers 1`；多副本需外部队列（`docs/progress/project.md` §16，本文不展开，见 `02-ingestion-pipeline.md`）。
- 本会话未运行 pytest / docker，§7 的测试清单来自源码逐文件通读，行号与用例名以工作树为准；未实测任何端点（因此所有 HTTP 状态码结论均来自代码路径与测试断言，而非本机请求）。
