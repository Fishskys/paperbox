# 存储层（PostgreSQL / MinIO / OpenSearch）

| 项 | 内容 |
|---|---|
| 状态 | 依据 commit 54048a3 的工作树实测（2026-09-22） |
| 关键文件 | `app/db/models.py`、`app/db/session.py`、`migrations/versions/`（6 个脚本）、`migrations/env.py`、`alembic.ini`、`app/search/mappings.py`、`app/search/opensearch.py`、`app/services/object_storage.py`、`app/services/paper_service.py`、`app/services/archive_service.py`、`app/workers/housekeeping.py`、`scripts/create_index.py`、`scripts/purge_deleted.py`、`infra/docker-compose.yml`、`.env.example` |
| 相关文档 | `AGENTS.md`（:66 记 head `7a2f4c9d51be`）、`README.md`（:60 端口/bucket、:373 v2 索引、:379 create_index、:492 "9 张表"）、`docs/architecture/metadata-architecture.md` |

## 1. 职责边界（做什么 / 不做什么）

做：

- PostgreSQL 是元数据的事实源（`app/db/session.py:3-5`），承载 14 张表（`app/db/models.py`），软删除只改 `deleted_at`；
- MinIO 存原文 PDF 与上传暂存对象，键布局由 `app/services/object_storage.py` 统一构造，API 只经 `GET /api/papers/{id}/file` 转发（`app/services/object_storage.py:11-12`）；
- OpenSearch 存 chunk 级文档（含 1024 维向量），全部读写走别名 `paper_chunks_current`（`app/search/opensearch.py:4-5`、`:24-27`）；
- **三端一致性对账（只读）**：`GET /api/consistency` + `scripts/check_consistency.py` 逐篇核对「`paper_files.object_key` ↔ MinIO 对象」与「chunk 行 ↔ 索引文档」，报出缺失/孤儿/删除残留；**解析产物（`papers/<id>/extracted/...`，即 `PARSER_CACHE`）单独计成 `cache_objects`/`cache_objects_total`，永不算 problem**（它从不登记 `paper_files`，2026-09-30 前被误报成 `orphan_object`，30 篇 = `problems=30`）；不写任何一端，某个 store 连不上只记进 `errors` 并继续回答另外两端（`app/services/consistency_service.py:485`，2026-09-22）。加 `?parser_papers=true`（或 `check_consistency.py --parser-papers`）时另外按解析戳给出**存活论文 id 清单**（`:354` `_census_ids`，上限 `:93` `PARSER_PAPER_ID_LIMIT`）——那是 `scripts/reindex.py --parser-backend <name>` 的工作清单，默认报告仍是摘要（2026-09-30）。
- 清理职责：`DELETE /api/papers/{id}` 先删索引文档与对象再置 `deleted_at`（`app/api/papers.py:302-331`）；`app/workers/housekeeping.py` 回收 `uploads/` 残留与解包目录；`scripts/purge_deleted.py` 补历史遗留。

不做：

- 不做跨存储事务：PG、OpenSearch、MinIO 三步顺序执行、各自幂等，任一步失败就返回 503 让调用方整体重试（`app/api/papers.py:306-328`）；
- 不用 DB 触发器维护时间戳：`updated_at` 靠 SQLAlchemy `onupdate`，裸 SQL 更新不会刷新（`app/db/models.py:22-23`、`:65-70`）；
- 不存 extracted/figures 产物：`build_extracted_key`/`build_figure_key` 已定义但本仓库无调用方（`app/services/object_storage.py:114-119`，grep 全仓无引用）；
- 不让客户端直连 MinIO（无 presign 对外，`presigned_get_url` 注释为内部调试，`app/services/object_storage.py:453-462`）。

## 2. 关键文件与函数（文件 → 函数/类 → 作用，带行号）

| 文件 | 符号 | 作用 |
|---|---|---|
| `app/db/models.py` | `Base` / `TimestampMixin` | 声明式基类；`created_at`/`updated_at` 服务器默认 `now()`（:50-70） |
| | `Paper` …`SearchQuery` 14 个类 | 14 张表定义，见第 3 节 |
| `app/db/session.py` | `get_engine` | 懒建引擎，`pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=1800`（:22-34） |
| | `session_scope` / `get_db` | 脚本事务上下文（:56-67）/ FastAPI 请求级依赖（:70-76） |
| `migrations/env.py` | `get_url` / `include_object` | DSN 只来自 `settings.database_url`（:31）；`include_object` 恒返回 True（:43-45） |
| `app/search/mappings.py` | `build_mapping` | 返回 chunk 索引的 settings+mappings（:104-174），`dynamic: "strict"` |
| `app/search/opensearch.py` | `ensure_index` | 幂等建索引并把别名指过去（:69-110） |
| | `build_alias_swap_body` / `alias_swap_is_safe` | 原子换别名（带 `is_write_index`，:122-133）；只在两侧文档数相等时允许切换（:136-138） |
| | `build_chunk_document` / `bulk_index_chunks` | 行→文档（:141-173）；批量写 200/批（:176-235） |
| | `delete_by_paper_id` | 按 `paper_id` term 删文档（:238-263） |
| `app/services/object_storage.py` | `build_object_key` / `build_staging_key` | `papers/<id>/original.pdf`（:109-111）/ `uploads/<req>/<i>-<name>.pdf`（:262-273） |
| | `upload_stream_hashed` / `_HashingReader` | 边传边算 SHA256，不二次读流（:276-326、:72-106） |
| | `delete_prefix` / `move_object` / `safe_filename` | 按前缀删（:426-434）/ 重定位对象（:389-423）/ 净化文件名（:245-259） |
| `app/services/paper_service.py` | `soft_delete_paper` | 标记论文与文件删除，并物理删除 `paper_identifiers`（:670-692） |
| `app/services/archive_service.py` | `extraction_root` / `archive_path` | `<tmp>/paperbox-<request_id>/` 与 `<tmp>/paperbox-<request_id>.zip`（:132-140） |
| `app/workers/tasks.py` | `_store_source` / `_cleanup_source` / `remove_local_file` | 落到 `papers/<id>/original.pdf`（:396-418）；STORED 后删 staging 与解包文件（:421-439）；删文件并修剪空目录（:442-460） |
| `app/workers/housekeeping.py` | `run_gc` | 回收孤儿/过期 staging 与超过 TTL 的解包目录（:155-231） |
| `scripts/create_index.py` | `verify` / `wait_for_task` / `migrate` | 幂等建索引并报告（:91-118）／轮询 `GET _tasks/<id>`（:120-142）／`_reindex` 后原子切别名（:145-247） |

## 3. 数据结构（表/字段/索引）

`models.py` 文档字符串写的是"9 + 4 = 13 张"（`app/db/models.py:3-14`），实际定义 **14** 张：第 14 张是后加的 `search_queries`（`app/db/models.py:776-817`）。下表按 14 张全列。

| 表 | 列（类型 / 可空 / 默认） | 索引与约束 |
|---|---|---|
| `papers` | `id` UUID PK（`default=new_uuid`）；`external_id` varchar255 空；`fingerprint` varchar255 非空；`title` text 非空；`abstract` text 空；`language` varchar32 空；`year` int 空；`doi` varchar255 空；`arxiv_id` varchar64 空；`url` text 空；`venue_id` UUID FK→`venues` SET NULL 空；`volume` varchar32 空；`issue` varchar32 空；`pages` varchar64 空；`publication_date` date 空；`paper_type` varchar32 空；`venue_edition_id` UUID FK→`venue_editions` SET NULL 空；`venue_year` int 空；`status` varchar32 非空 默认 `'pending'`；`embedding_model` varchar128 空；`embedding_dimension` int 空；**`parser_backend` varchar16 空（索引）**、**`parser_version` varchar64 空**（迁移 `81a04251abfa_paper_parser_stamp.py`，`models.py:137-138`）；`deleted_at` timestamptz 空；`created_at`/`updated_at` | `ix_papers_doi`/`_arxiv_id`/`_year`/`_status`/`_created_at`/`_venue_year`；**部分唯一** `uq_papers_fingerprint_live` |
| `authors` | `id` PK；`name` varchar512 非空；`normalized_name` varchar512 非空；`orcid` varchar64 空；`affiliation` text 空；`created_at`/`updated_at` | `ix_authors_name` |
| `venues` | `id` PK；`name` varchar512 非空；`normalized_name` varchar512 非空 **唯一**；`kind` varchar32 空；`publisher` varchar255 空；`issn` varchar64 空；时间戳 | `UNIQUE(normalized_name)` |
| `paper_authors` | `id` PK；`paper_id` FK CASCADE 非空；`author_id` FK CASCADE 非空；`author_order` int 非空 默认 0；`is_corresponding` bool 非空 默认 false；`created_at`（无 `updated_at`） | `UNIQUE(paper_id,author_id)`、`UNIQUE(paper_id,author_order)`、`ix_paper_authors_author_id` |
| `paper_tags` | `id` PK；`name` varchar128 非空；`normalized_name` varchar128 非空 **唯一**；时间戳 | `UNIQUE(normalized_name)` |
| `papers_tags` | `id` PK；`paper_id` FK CASCADE 非空；`tag_id` FK→`paper_tags` CASCADE 非空；`kind` varchar32 非空 默认 `'source_tag'`；`created_at` | `UNIQUE(paper_id,tag_id)`、`ix_papers_tags_tag_id` |
| `paper_files` | `id` PK；`paper_id` FK CASCADE 非空；`source_id` FK→`paper_sources` SET NULL 空；`kind` varchar32 非空 默认 `'original'`；`object_key` varchar1024 非空；`bucket` varchar255 非空；`filename` varchar512 空；`content_type` varchar128 空；`size_bytes` int 空；`sha256` varchar64 空；`is_primary` bool 非空 默认 false；`deleted_at`；时间戳 | `ix_paper_files_paper_id`、`ix_paper_files_sha256`；**部分唯一** `uq_paper_files_primary` |
| `paper_chunks` | `id` PK；`paper_id` FK CASCADE 非空；`chunk_index` int 非空；`page_start`/`page_end` int 空；`section`/`subsection` varchar255 空；`text` text 非空；`token_count`/`char_count` int 空；`embedding_model` varchar128 空；`embedding_dimension` int 空；`embedded_at`/`indexed_at`/`deleted_at` 空；`doc_metadata` JSONB 空；时间戳 | `UNIQUE(paper_id,chunk_index)`、`CHECK(page_end>=page_start)`、`ix_paper_chunks_paper_id`、`ix_paper_chunks_paper_section` |
| `ingestion_jobs` | `id` PK；`paper_id` FK→`papers` **SET NULL** 空；`kind` varchar32 非空 默认 `'ingest'`；`stage` varchar32 非空 默认 `'received'`；`progress` float 非空 默认 0；`error_code` varchar32 空；`error_message` text 空；`payload` JSONB 空；`started_at`/`finished_at` 空；时间戳 | `ix_ingestion_jobs_paper_id`/`_stage`/`_created_at` |
| `paper_sources` | `id` PK；`paper_id` FK CASCADE 空；`source_type` varchar32 非空；`source_ref` varchar512 非空；`content_type` varchar32 空；`raw` JSONB 非空 默认 `'{}'`；`match_status` varchar16 非空 默认 `'pending'`；`match_method` varchar32 空；`match_confidence` float 空；`fetched_at` 空；`imported_at` 非空 默认 now()；`importer` varchar128 空（无 `updated_at`） | `UNIQUE(source_type,source_ref)`、`ix_paper_sources_paper_id`、`ix_paper_sources_match_status` |
| `paper_identifiers` | `id` PK；`paper_id` FK CASCADE **非空**；`scheme` varchar32 非空；`value` text 非空；`normalized_value` text 非空；`first_source_id` FK→`paper_sources` SET NULL 空；`is_primary` bool 非空 默认 false；`created_at` | `UNIQUE(paper_id,scheme,normalized_value)`、**部分唯一** `uq_paper_identifiers_scheme_value`、`ix_paper_identifiers_paper_id` |
| `paper_field_provenance` | `id` PK；`paper_id` FK CASCADE 非空；`source_id` FK SET NULL 空；`field` varchar64 非空；`value` JSONB 非空；`confidence` float 空；`is_current` bool 非空 默认 false；`decided_by` varchar32 非空 默认 `'initial'`；`decided_at` 非空 默认 now()；`identifier_id` FK→`paper_identifiers` SET NULL 空 | **部分唯一** `uq_paper_field_provenance_current`、`ix_paper_field_provenance_paper_field` |
| `venue_editions` | `id` PK；`venue_id` FK CASCADE 非空；`year` int 非空；`location`/`dates` text 空；`publication_number` varchar64 空；`is_number` varchar64 空（无时间戳列） | `UNIQUE(venue_id,year)` |
| `search_queries` | `id` PK；`request_id` varchar64 空；`query` text 非空；`rewritten_query` text 空；`mode` varchar16 非空；`top_k` int 非空；`rerank` bool 非空 默认 false；`filters` JSONB 空；`candidates` int 空；`returned` int 非空；`took_ms` int 空；`results` JSONB 空；`created_at` 非空 默认 now() | `ix_search_queries_created_at`、`ix_search_queries_mode` |

### 4 个部分唯一索引表达的不变量

| 索引（定义处） | 谓词 | 不变量 |
|---|---|---|
| `uq_papers_fingerprint_live`（`app/db/models.py:88-93`；迁移 `7359b44a3938:24-25`） | `WHERE deleted_at IS NULL` | 只有"活"论文占用指纹；删除后释放，同一文档可重新导入（`app/services/paper_service.py:670-684` 只置 `deleted_at`，行保留供审计） |
| `uq_paper_identifiers_scheme_value`（`models.py:557-563`；迁移 `7a2f4c9d51be:107-113`） | `WHERE paper_id IS NOT NULL` | 一个 `(scheme, normalized_value)` 至多属于一篇论文，两个来源引用同一 DOI 不会落成两行。**注意谓词实际恒真**：`paper_id` 列本身 `NOT NULL`（`models.py:570-572`、迁移 `:80`），所以它等价于全表唯一；删除论文时 `paper_identifiers` 行被物理删除以释放 DOI（`paper_service.py:688-690`） |
| `uq_paper_field_provenance_current`（`models.py:611-617`；迁移 `7a2f4c9d51be:152-158`） | `WHERE is_current` | 每个 `(paper_id, field)` 只有一条 current 记录；历史行 `is_current=false` 可无限追加，这是回滚能力的基础（`models.py:602-607`） |
| `uq_paper_files_primary`（`models.py:341-346`；迁移 `7a2f4c9d51be:214-220`） | `WHERE is_primary AND deleted_at IS NULL` | 每篇活论文至多一个主版本文件（唯一键只有 `paper_id`，谓词已含 `is_primary`）；"至少一个"不受约束，实际允许 0 个 |

### ER 关系图（FK 与删除行为）

```
venues ──CASCADE──> venue_editions
   │  ▲ SET NULL                │  ▲ SET NULL
   │  └──────────────┐          │  │
   └──(papers.venue_id)         └──(papers.venue_edition_id)
                      papers
   ┌──────┬───────────┬──────────┬───────────┬───────────┬────────────┐
 CASCADE CASCADE    CASCADE    CASCADE     SET NULL    (无FK)
 paper_authors paper_chunks paper_files papers_tags ingestion_jobs search_queries
   │                                │  ▲ SET NULL
 authors                        (source_id) ──> paper_sources
                                                  ▲ SET NULL ↑
   papers ──CASCADE── paper_sources ──CASCADE── paper_identifiers
                ▲ SET NULL (source_id)              ▲ SET NULL
   papers ──CASCADE── paper_field_provenance ──SET NULL──┘
                        └─(identifier_id → paper_identifiers, SET NULL)
```

- 论文之下**级联删除**（DB `ON DELETE CASCADE`，ORM 亦 `cascade="all, delete-orphan"`）：`paper_authors`、`papers_tags`、`paper_files`、`paper_chunks`、`paper_sources`、`paper_identifiers`、`paper_field_provenance`（`models.py:144-172`）。
- `Paper.ingestion_jobs` 同样声明 `cascade="all, delete-orphan"`（`models.py:172-172`），但 FK 是 `ON DELETE SET NULL`（`models.py:451-453`）——两者语义不同：ORM 删除论文会删掉作业行，直接 SQL 删论文只会把 `ingestion_jobs.paper_id` 置空。
- 同理 `paper_files.source_id`、`paper_identifiers.first_source_id`、`paper_field_provenance.source_id/identifier_id` 都是 SET NULL：来源行消失不会连带删证据行。

## 4. 调用链（从入口到落地，逐跳，带函数名）

写入（上传一个 PDF）：

1. `POST /api/papers/ingest/files` → `app/api/ingestion.py:161` `object_storage.build_staging_key(request_id, index, filename)` → `uploads/<req>/<i>-<name>.pdf`；
2. `object_storage.upload_stream_hashed`（`:295-345`）经 `_HashingReader`（`:72-106`）落盘并得到 `sha256`，用于内容去重；
3. worker `_store_source`（`app/workers/tasks.py:396-418`）→ `object_storage.build_object_key(paper_id)` → `papers/<paper_id>/original.pdf`；
4. `paper_service.register_original_file`（`app/services/paper_service.py:627-667`）写 `paper_files` 行（`bucket`、`object_key`、`sha256`、`is_primary`）；
5. `_cleanup_source`（`tasks.py:421-439`）删 staging 对象（失败交给 housekeeping），有 `cleanup_after` 时 `remove_local_file`（`:442-460`）删解包文件并修剪空目录；
6. 解析/embedding 后 `opensearch.bulk_index_chunks`（`app/search/opensearch.py:302-361`）写别名 `paper_chunks_current`，文档 `_id = chunk_id`。

删除（`DELETE /api/papers/{id}`，`app/api/papers.py:302-342`）：

1. `_load_paper` 取活论文（软删后 404）；
2. `opensearch.delete_by_paper_id`（`opensearch.py:364-389`）删索引文档；失败 → `SearchIndexError` → 503，论文保持可见；
3. `object_storage.delete_prefix(paper.id)`（`object_storage.py:426-434`）删 `papers/<id>/` 下全部对象；失败 → 503；
4. `paper_service.soft_delete_paper`（`:547-569`）置 `papers.deleted_at = now()`、`status = "DELETED"`（常量 `:34`）、把未删的 `paper_files` 也置 `deleted_at`，并物理删除该论文的 `paper_identifiers`；
5. `session.commit()`。

补漏：`scripts/purge_deleted.py` 遍历 `deleted_at IS NOT NULL` 的论文（`:63-69`），对每篇调 `opensearch.delete_by_paper_id` + `object_storage.delete_prefix`（`:149-150`），`--dry-run` 只统计不写；`paper_chunks` 行故意保留（`:14-14`）。

## 5. 不变量与踩过的坑

- 指纹优先序 `DOI > arXiv > 标题+首作者+年份 > sha256`（`app/services/paper_service.py:3-5`、`build_fingerprint:127-140`），指纹唯一性是**部分**索引，删除即释放（见第 3 节）。
- `papers.status` 的 DB 默认值是小写 `'pending'`（`models.py:126-128`），而应用写的是大写常量 `STATUS_PENDING = "PENDING"`（`paper_service.py:41-45`），且筛选时 `.strip().upper()`（`:340`）——绕过 ORM 插入的行会是小写。
- `ingestion_jobs.stage` 默认 `'received'`（`models.py:458-460`），而 housekeeping 用大写判断终态 `("COMPLETED","FAILED")`（`app/workers/housekeeping.py:154`）；`finished_at IS NULL` 才是"活着"的统一判据。
- `knn_vector` 映射无法原地修改：`ensure_index` 从不在已存在的索引上重写 mapping（`app/search/opensearch.py:69-82`），换分词器/维度只能新建索引 + `_reindex`（`scripts/create_index.py:1-27`）。
- **但给活索引「加」新字段是允许的**：`opensearch.update_mapping()`（`opensearch.py:113`）走 `PUT _mapping`，只补 `build_mapping()` 里新增的 properties；存量文档用 `scripts/refresh_index_metadata.py`（→ `bulk_update_documents`，`opensearch.py:145`）批量 partial update，不重算向量。**必须赶在第一个带该字段的文档之前**，否则 `dynamic: true` 会先把它映成 `text`（`pages`/`paper_type` 这类要按 keyword 过滤的字段就废了）；改**已有**字段的类型仍然只能新建索引。
- 别名切换有闸门：只有新旧索引文档数完全相等才允许切（`opensearch.py:231-233`，`create_index.py` 第 3 步），失败时不动别名。
- 单节点 OpenSearch 无副本（`number_of_replicas: 0`，`mappings.py:160`）且安全插件关闭（`infra/docker-compose.yml:52`）。
- MinIO 的 `move_object` 是"拷贝+删除"，best effort；失败时调用方保留旧 key，只有路径异常（`object_storage.py:389-423`）——shell 论文场景下意味着对象可能不在 `papers/<paper_id>/`，`delete_prefix` 会漏删。
- 解包目录 TTL 24h、GC 每 300s 一次且启动即跑（`housekeeping.py:217`、`:257-286`、`:419-430`）；staging 对象只要被"活"作业引用就绝不删（`:249`）。
- `_HashingReader` 只拦截 `read`/`readinto`，其余属性透传（`object_storage.py:72-106`）。

## 6. 配置项（键 → 默认值 → 作用 → 出处）

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `POSTGRES_DSN` | `postgresql+psycopg://postgres:postgres@localhost:5432/paperbox` | 唯一 DSN 来源，Alembic 也用它 | `app/core/config.py:50-53`；`migrations/env.py:31`；`alembic.ini:5-7` 留空 |
| 连接池 | `pool_size=5` / `max_overflow=10` / `pool_recycle=1800` / `pool_pre_ping=True` | 引擎行为（非环境变量） | `app/db/session.py:26-33` |
| `OPENSEARCH_INDEX` | `paper_chunks_v3` | 物理索引名 | `app/core/config.py:62`；`.env.example:17` 与实测 `.env` 均为 `paper_chunks_v3` |
| `OPENSEARCH_ALIAS` | `paper_chunks_current` | 读写别名 | `app/core/config.py:63`；`app/search/opensearch.py:24-27` |
| `EMBEDDING_DIMENSION` | `1024` | 决定 `knn_vector.dimension` | `app/core/config.py:77`；`app/search/mappings.py:124` |
| `MINIO_BUCKET` | `paperbox` | 默认桶 | `app/core/config.py:72`；`.env.example:25` |
| `MINIO_SECURE` | `False` | 明文 HTTP | `app/core/config.py:71` |
| `papers` / `uploads` / `original.pdf` | 常量 | 正式前缀、暂存前缀、正式文件名 | `app/services/object_storage.py:37-41` |
| `BULK_BATCH_SIZE` | `200` | 批量索引批大小 | `app/search/opensearch.py:30` |
| `INGEST_ARCHIVE_TMP_DIR` | `""` → 系统 temp | 解包根目录 | `app/core/config.py:186`、`:250-254`；`.env.example:187` |
| `INGEST_ARCHIVE_TTL_HOURS` | `24` | 解包目录保留期 | `app/core/config.py:188` |
| `INGEST_GC_INTERVAL_S` | `300` | GC 间隔，启动即跑一次 | `app/core/config.py:192`；`app/workers/housekeeping.py:419-430` |
| `OPENSEARCH_JAVA_OPTS` | `-Xms1g -Xmx1g` | 单节点 JVM 堆 | `infra/docker-compose.yml:51` |
| `OPENSEARCH_BACKUP_DIR` | `./data/opensearch-backups` | 快照仓库落点（挂到容器 `/mnt/backups`，与 `-Epath.repo` 成对） | `infra/docker-compose.yml:70-75`；`infra/.env:15` |
| 快照策略 | `paperbox-daily`（`30 3 * * *` Asia/Shanghai，留 14 份/30 天） | SM 定时快照：`paper_chunks_*,search-relevance-*` | `scripts/setup_snapshots.py:41-56`；真机 `_plugins/_sm/policies` |
| 镜像/端口 | `postgres:15.2-alpine:5432`、`opensearchproject/opensearch:3.6.0:9200`、`minio/minio:RELEASE.2025-07-23T15-54-02Z-cpuv1:9000/9001` | 依赖版本与端口 | `infra/docker-compose.yml:17`、`:40`、`:77`、`:29-30`、`:61-62`、`:89-91` |

## 7. 测试位置与覆盖（tests/xxx.py → 覆盖什么）

| 测试 | 覆盖 |
|---|---|
| `tests/test_deletion.py` | 删除顺序：OpenSearch → MinIO → PG 标记；用记录式 fake 调 `delete_paper` |
| `tests/test_fingerprint_release.py` | `papers.fingerprint` 是**部分**唯一索引而非普通唯一约束（`Paper.__table_args__`） |
| `tests/test_metadata_models.py` | 4 个部分唯一索引、14 张表的列与关系（纯 metadata 断言，不连库） |
| `tests/test_index_migration.py` | cjk 分词字段、keyword/int/embedding 未变、`_reindex` body、别名切换 body 与闸门、迁移流程（fake client） |
| `tests/test_primary_version.py` | 主版本优先级 `published_pdf > original > arxiv_pdf`、`is_primary` 转移 |
| `tests/test_upload_stream.py` | `_HashingReader` 的 sha256、`upload_stream_hashed` 返回值 |
| `tests/test_stored_cleanup.py` | STORED 之后删 staging 对象与解包本地文件 |
| `tests/test_upload_gc.py` | housekeeping：活作业对象不删、终态/孤儿对象删、TTL 目录回收 |
| `tests/test_metadata_identifiers.py` | 标识符归一化、主标识符推导、幂等写入 |
| `tests/test_provenance.py` | 每字段一条 current、历史保留、rollback |
| `tests/test_venue_editions.py` | venue/edition 拆分：一个 venue 多个 edition |
| `tests/test_search_log.py` | `search_queries` 序列化与降级（fake session 故意失败） |
| `tests/test_ingest_compressed.py` | 解包安全（zip-slip/zip bomb/非 zip 415） |
| `tests/test_bulk_ingest_dir.py` | 目录导入脚本的纯逻辑（候选过滤、429 退避、报告计数） |

说明：`tests/test_metadata_models.py:12-14` 明确 "the real `alembic upgrade head` run is part of the acceptance walkthrough, not of the unit suite"——单元测试不验证真实迁移执行。

## 8. 未做 / 已知缺口

- 文档与代码冲突（以代码为准）：`models.py:3-14` 与 `README.md:513` 说 9/13 张表，实际 14 张；`mappings.py:1` 与 `README.md:399` 提到索引名，运行时一律由 `OPENSEARCH_INDEX` 决定（`.env.example:17` 与工作树 `.env` 均为 `paper_chunks_v3`）。
- `build_extracted_key` / `build_figure_key` / `papers/<id>/supplementary/` 只是预留布局，无任何调用方（`object_storage.py:5-9`、`:114-119`）。
- **备份只覆盖 OpenSearch**：快照仓库（`paperbox_backup`）不包含 PostgreSQL 与 MinIO 原件 —— PG 在 WSL 的 ext4（`paperbox-data/` 备份不覆盖），MinIO 的对象要靠 `mc mirror` 或冻结的 PDF 副本。
- `papers.deleted_at` 之外没有清理机制：`paper_chunks` 行软删后长期保留，仅 `scripts/purge_deleted.py` 处理 OpenSearch/MinIO 遗留，需要人工触发。
- `search_queries` 无分区、无 TTL、无清理脚本（仅 `app/services/search_log_service.py` 写入）——是否另有保留策略未确认。
- `move_object` 失败会让对象留在 `papers/<临时id>/`，`delete_prefix` 按 `paper_id` 前缀删会漏掉它（`object_storage.py:389-423`），未找到补偿扫描。
- 未确认（本会话未连库/未连集群）：生产库结构是否与迁移链 head `7a2f4c9d51be` 完全一致；OpenSearch 实际 mapping 与别名指向；MinIO 中 `papers/`、`uploads/` 之外是否还有历史前缀对象。
- 未确认：`INGEST_ARCHIVE_TMP_DIR` 留空时 Windows 上 `tempfile.gettempdir()` 的具体路径（未读环境变量）。
