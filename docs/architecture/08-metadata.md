# 元数据层（多来源 / 匹配 / 合并 / 手动编辑）

| 项 | 内容 |
|---|---|
| 状态 | 依据 commit `2196176` 的工作树实测（2026-09-22）。本文只写**现状**：表怎么设计、代码怎么组织、有几张表、表里有什么 |
| 表 | 元数据落在 **10 张表**上：4 张元数据专用表（`paper_sources` / `paper_identifiers` / `paper_field_provenance` / `venue_editions`）+ 6 张复用表（`papers` / `venues` / `paper_files` / `authors` + `paper_authors` / `paper_tags` + `papers_tags`）；全库共 14 张表 |
| 代码 | `app/services/metadata_*.py`（8 个）+ `provenance_service.py` + `venue_service.py` = 11 个模块 / 4727 行；`app/api/metadata.py`、`app/api/papers.py`；`app/schemas/metadata.py`；`app/workers/tasks.py`；`app/db/models.py` |
| 脚本 | `scripts/backfill_metadata.py`（历史论文回填，幂等）、`scripts/import_metadata.py`（CLI 导入）、`scripts/acceptance_metadata.py`（真机验收 7 项） |
| 相关文档 | `docs/architecture/metadata-architecture.md`（设计背景、逐字段映射、术语表）、`AGENTS.md` §3.9（环境契约）、`docs/progress/project.md` §17（实测数字） |

---

## 1. 数据模型总览

### 1.1 十张表各是什么

| 表 | 一行代表 | 谁写它 | 出处 |
|---|---|---|---|
| `papers` | 一篇论文（**当前值**的物化） | `provenance_service.write_field`、`metadata_merge` | `models.py:76` |
| `paper_sources` | 一份来源数据（一次导入 / 一次解析） | `metadata_sources.upsert_source` | `models.py:468` |
| `paper_field_provenance` | 一条字段声明（append-only 账本） | `provenance_service.record_claim` / `promote` | `models.py:588` |
| `paper_identifiers` | 一个标识符（DOI / arXiv / IEEE article number / …） | `metadata_identifiers.upsert_identifier` / `replace_identifier` | `models.py:527` |
| `venues` | 一个期刊或会议**实体** | `venue_service.resolve_venue` | `models.py:206` |
| `venue_editions` | 某实体**某一年的那一届** | `venue_service.get_or_create_edition` | `models.py:655` |
| `paper_files` | 一个 PDF 版本（含主版本标记、来源归属） | `paper_service.register_original_file` / `select_primary_file` | `models.py:326` |
| `authors` + `paper_authors` | 作者字典 + 论文↔作者（带顺序） | `paper_service.get_or_create_author` | `models.py:177` / `:237` |
| `paper_tags` + `papers_tags` | 标签字典 + 论文↔标签（带 `kind`） | `metadata_tags.link_tags` / `replace_kind` | `models.py:265` / `:293` |

关系主轴（三层 + 一个骨架）：

```
papers ──1:N──> paper_sources ──1:N──> paper_field_provenance
  │  ▲                 │                      │
  │  └─────────────────┴── paper_identifiers ─┘   （骨架：一个标识符只属一篇论文）
  ├──> paper_files（多版本，只有一个 is_primary）
  ├──> venue_id ─> venues ─1:N─> venue_editions  ◄── papers.venue_edition_id + venue_year
  ├──> paper_authors ─> authors
  └──> papers_tags ─> paper_tags
```

### 1.2 存储分层（什么放哪）

| 内容 | 位置 | 说明 |
|---|---|---|
| 结构化当前值（标题/年份/venue/…） | PostgreSQL `papers` | 查询与列表直接读，不 join 账本 |
| 来源快照（IEEE 一条 raw 约 8KB） | PostgreSQL `paper_sources.raw`（JSONB） | 原样保存，**不进 OpenSearch、不进 MinIO** |
| 字段级账本 | PostgreSQL `paper_field_provenance` | 历史行永不删除，回滚靠翻 `is_current` |
| 标识符、venue、标签 | PostgreSQL 对应表 | 见 §3 |
| PDF 原件 | MinIO（`papers/<paper_id>/…`） | 元数据层只存 `paper_files.object_key` / `sha256` |
| 可检索文本 + 向量 + **过滤字段快照** | OpenSearch chunk 文档 | 过滤字段（venue/year/tags/doi）是**索引时的快照**，所以改元数据要 `POST /api/papers/{id}/reindex` 才影响过滤（`README.md:282`） |

---

## 2. 设计思路

### 2.1 当前值与历史声明分离

- **要解决的问题**：多来源会互相覆盖同一个字段，覆盖之后就查不出"这个值是谁写的、之前是什么"。
- **做法**：`papers` 的列只放**当前值**（查询快、可直接过滤）；每次写入同时往 `paper_field_provenance` 记一条**声明**（`field` + `value` + `source_id` + `confidence` + `decided_by`），只把其中一条标 `is_current=true`。账本 append-only，历史行不删。
- **代价**：一次导入会写多行；回答"为什么是这个值"要 join 账本（`metadata_view` 负责组装给 API 看）。

### 2.2 来源记录独立成表

- **要解决的问题**：同一篇论文可能来自 IEEE 导出、arXiv、PDF 内嵌、首页启发式、人工修改，来源之间要能追溯、能幂等重放。
- **做法**：一份来源数据 = `paper_sources` 一行，`raw` 原样存 JSONB；`UNIQUE(source_type, source_ref)` 让重复导入不产生第二行；`paper_id` **可空**——还没匹配上的记录先落在表里，等人工在复核清单里归属。
- **代价**：表会变长（每次解析、每次导入都留痕）；换来的是"任何字段都能追到来源"。

### 2.3 标识符独立成骨架

- **要解决的问题**：DOI 是跨平台的论文身份证，但**部分论文没有 DOI**，需要有兜底；同时不同平台各有自己的编号（IEEE article number、ISSN、PMID、OpenAlex…）。
- **做法**：所有标识符进 `paper_identifiers`，`scheme` 是**开放枚举**（加平台 = 加取值，不加列，`metadata_identifiers.py:52-63`）；部分唯一索引保证**一个标识符只属一篇论文**；主标识符（只有 `doi` / `arxiv`，`:66`）决定 `papers.fingerprint`，兜底链是 `DOI > arXiv > 标题+首作者+年 > sha256`。
- **代价**：`papers.doi` / `papers.arxiv_id` 变成**镜像列**（真值在标识符表），必须靠 `mirror_legacy_columns`（`:397`）保持同步。

### 2.4 venue 拆成两级

- **要解决的问题**：会议论文的 venue 粒度是"会议 + 年份"，而且要能"只按会议搜"和"按会议+年份搜"两种粒度。
- **做法**：`venues` 存实体（`name` / `normalized_name` / `kind` / `publisher` / `issn`），`venue_editions` 存某一年那一届（`year` / `location` / `dates` / `publication_number` / `is_number`）；`papers` 同时挂 `venue_id`、`venue_edition_id` 和冗余列 `venue_year`。**绝不用"会议+年份"拼接串**。
- **代价**：写入时要两级都解析（`resolve_venue` / `get_or_create_edition`），venue 别名消歧目前只做到 `normalize_text` 级（`venue_service.py:112`）。

### 2.5 作者只存名字

- **做法**：`authors` 是字典表（`name` / `normalized_name` 唯一），`paper_authors` 记顺序；`orcid` / `affiliation` 列存在但元数据层不写。
- **边界**：不建 `author_identifiers`，IEEE 记录里的作者 id 直接丢弃——记下所有作者名字就够用。

### 2.6 标签带 kind

- **做法**：`paper_tags` 是标签字典，`papers_tags.kind` 区分来源：`ieee_terms` / `author_terms` / `dynamic_index_terms` / `source_tag`（默认 `source_tag`）。同一篇论文的 IEEE 索引词与来源标签互不混淆，检索过滤可按 kind 或全量。

---

## 3. 表里有什么

### 3.1 `paper_sources`（`models.py:468-524`）

| 列 | 类型 / 约束 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `paper_id` | UUID，FK→`papers.id`，**可空** | 空 = 还没归属到任何论文（复核清单的数据源） |
| `source_type` | VARCHAR(32) NOT NULL | `ieee_api` / `arxiv_api` / `crossref` / `pdf_embedded` / `pdf_heuristic` / `import_file` / `manual`（`metadata_sources.py:27-33`） |
| `source_ref` | VARCHAR(512) NOT NULL | 来源内的稳定引用：`doi:…` / `arxiv:…` / `ieee:<article_number>` / 路径+sha256 / `heuristic:<sha>` 等（`metadata_sources.py:69-95`） |
| `content_type` | VARCHAR(32) | `ieee_raw` / `csl_json` / `generic_json` 等 |
| `raw` | JSONB NOT NULL，默认 `{}` | **来源原样**（IEEE 一条约 8KB），只在 PG |
| `match_status` | VARCHAR(16) NOT NULL，默认 `pending` | `matched` / `pending` / `ambiguous` / `rejected`（`:45-48`）；复核队列取 `pending` + `ambiguous`（`:58`） |
| `match_method` | VARCHAR(32) | 怎么匹配上的，见 §4.4 |
| `match_confidence` | FLOAT | 0.5 / 0.8 / 1.0 |
| `fetched_at` | DATETIME | 来源侧取数时间 |
| `imported_at` | DATETIME NOT NULL，默认 `now()` | |
| `importer` | VARCHAR(128) | API / CLI / 脚本名 |

约束与索引：`UNIQUE(source_type, source_ref)`（`uq_paper_sources_type_ref`，`models.py:481`）＝**幂等导入**；`ix_paper_sources_paper_id`、`ix_paper_sources_match_status`。

### 3.2 `paper_identifiers`（`models.py:527-586`）

| 列 | 类型 / 约束 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `paper_id` | UUID NOT NULL，FK→`papers.id` | 非空：一条标识符必须属于某篇论文 |
| `scheme` | VARCHAR(32) NOT NULL | 开放枚举 10 个：`doi` / `arxiv` / `ieee_article_number` / `issn` / `isbn` / `pmid` / `openalex` / `semantic_scholar` / `url` / `sha256`（`metadata_identifiers.py:52-63`） |
| `value` | TEXT NOT NULL | 原样值 |
| `normalized_value` | TEXT NOT NULL | 规范化值：DOI 去前缀小写、arXiv 去版本后缀、ISSN 去非数字、sha256 校验 64 hex（`:72-116`） |
| `first_source_id` | UUID，FK→`paper_sources.id`（ON DELETE SET NULL） | 谁先带来的 |
| `is_primary` | BOOLEAN NOT NULL，默认 `false` | 只有 `doi` / `arxiv` 可为真（`PRIMARY_SCHEMES`，`:66`）；决定 `papers.fingerprint` |
| `created_at` | DATETIME NOT NULL，默认 `now()` | |

约束与索引：`UNIQUE(paper_id, scheme, normalized_value)`（`:522`）＝同一篇论文内不重复；**部分唯一** `UNIQUE(scheme, normalized_value) WHERE paper_id IS NOT NULL`（`uq_paper_identifiers_scheme_value`，`:529`）＝一个标识符只属一篇论文；`ix_paper_identifiers_paper_id`。

语义要点：删除论文时 `soft_delete_paper`（`paper_service.py:634`）会**删掉标识符行**以释放 DOI；`upsert_identifier`（`:230`）遇到"属主已软删"的行会把它改指到新论文。

### 3.3 `paper_field_provenance`（`models.py:588-653`）

| 列 | 类型 / 约束 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `paper_id` | UUID NOT NULL，FK→`papers.id` | |
| `source_id` | UUID，FK→`paper_sources.id`（SET NULL） | NULL = 系统或人工写入，无来源行 |
| `field` | VARCHAR(64) NOT NULL | 开放集合，见下 |
| `value` | JSONB NOT NULL | 标量或结构化值（venue 是对象、authors 是数组） |
| `confidence` | FLOAT | 结构化来源 1.0、首页启发式 0.5 |
| `is_current` | BOOLEAN NOT NULL，默认 `false` | 每个字段只有一条为真 |
| `decided_by` | VARCHAR(32) NOT NULL，默认 `initial` | `initial` / `structured_override` / `manual`（`provenance_service.py:37-39`） |
| `decided_at` | DATETIME NOT NULL，默认 `now()` | |
| `identifier_id` | UUID，FK→`paper_identifiers.id` | `identifier:<scheme>` 类声明指向具体标识符行 |

`field` 的取值空间（`provenance_service.py:61-106`）：

- 10 个标量：`title` / `abstract` / `language` / `year` / `volume` / `issue` / `pages` / `publication_date` / `paper_type` / `url`（`SIMPLE_FIELDS`，每个正好对应 `papers` 一列）
- `venue`、`authors`（结构化值，不走标量列映射）
- `identifier:<scheme>`，如 `identifier:doi`
- `tag:<kind>`，如 `tag:ieee_terms`

约束与索引：**部分唯一** `UNIQUE(paper_id, field) WHERE is_current`（`uq_paper_field_provenance_current`，`:583`）；`ix_paper_field_provenance_paper_field`。历史行永不删除——回滚 = 把历史某行置回 `is_current=true` 并写回 `papers` 列（`rollback_field`，`provenance_service.py:253`）。

### 3.4 `venues`（`models.py:206-231`）与 `venue_editions`（`:659-688`）

| 表 | 列 | 约束 |
|---|---|---|
| `venues` | `id`、`name`(512)、`normalized_name`(512)、`kind`(32，可空：journal / conference)、`publisher`(255)、`issn`(64)、`created_at`、`updated_at` | `UNIQUE(normalized_name)` |
| `venue_editions` | `id`、`venue_id`(FK NOT NULL)、`year`(INT NOT NULL)、`location`(TEXT)、`dates`(TEXT)、`publication_number`(64)、`is_number`(64) | `UNIQUE(venue_id, year)`（`uq_venue_editions_venue_year`） |

`papers` 侧对应三列：`venue_id`、`venue_edition_id`、`venue_year`（冗余，带 `ix_papers_venue_year`，`models.py:84`）——搜"会议"命中 `venues.name`，搜"会议+年份"命中 `venue_id + venue_year`，两种粒度都不需要 join 届次表。

写入方 `venue_service`：`get_or_create_edition:204` / `resolve_venue:252` / `attach_venue:285`，语义是**只填空**。

### 3.5 `papers` 的元数据相关列（`models.py:76-175`）

| 组 | 列 |
|---|---|
| 标识 | `id`(UUID PK)、`fingerprint`(255 NOT NULL)、`external_id`(255，**当前全仓无引用**) |
| 书目 | `title`(TEXT NOT NULL)、`abstract`、`language`(32)、`year`(INT)、`volume`(32)、`issue`(32)、`pages`(64，形如 `631-635`)、`publication_date`(DATE)、`paper_type`(32：journal / conference / preprint / early_access / standard) |
| venue | `venue_id`、`venue_edition_id`、`venue_year` |
| 镜像列 | `doi`(255)、`arxiv_id`(64)——真值在 `paper_identifiers`，由 `mirror_legacy_columns` 同步 |
| 状态 | `status`(32 NOT NULL，默认 `'pending'`；含 `AWAITING_FILE` = 只有元数据、还没 PDF 的壳)、`deleted_at`（软删） |
| 索引与模型 | `embedding_model`(128)、`embedding_dimension`(INT)、`created_at`、`updated_at` |

索引：部分唯一 `uq_papers_fingerprint_live (fingerprint) WHERE deleted_at IS NULL`；`ix_papers_doi` / `ix_papers_arxiv_id` / `ix_papers_year` / `ix_papers_status` / `ix_papers_venue_year` / `ix_papers_created_at`。

### 3.6 `paper_files`（`models.py:326-374`）

| 列 | 类型 / 约束 | 说明 |
|---|---|---|
| `id`、`paper_id`、`created_at`、`updated_at` | | |
| `source_id` | UUID，FK→`paper_sources.id` | **这个 PDF 由哪份来源带来** |
| `kind` | VARCHAR(32) NOT NULL，默认 `original` | `published_pdf` / `original` / `arxiv_pdf` / `supplement` |
| `object_key` | VARCHAR(1024) NOT NULL | MinIO 键，如 `papers/<paper_id>/original.pdf` |
| `bucket`、`filename`、`content_type`、`size_bytes`、`sha256`(64) | | `ix_paper_files_sha256` 支撑按文件判重 |
| `is_primary` | BOOLEAN NOT NULL，默认 `false` | 每篇论文只有一个主版本 |
| `deleted_at` | DATETIME | 软删 |

约束与索引：**部分唯一** `UNIQUE(paper_id) WHERE is_primary AND deleted_at IS NULL`（`uq_paper_files_primary`，`:324`）；`ix_paper_files_paper_id`。

主版本优先级 `published_pdf(3) > original(2) > arxiv_pdf(1) > supplement(0)`（`paper_service.py:63-68`）；`select_primary_file`(`:428`) 取优先级最高、同级取先到；`original_file(paper)`（`:402`）返回的**是主版本文件**，`GET /api/papers/{id}/file`、reindex、删除都走它。**只有主版本被解析/切块/索引**，非主版本照样登记但不解析。

### 3.7 作者与标签（`models.py:177-205`、`223-315`）

| 表 | 列 | 约束 |
|---|---|---|
| `authors` | `id`、`name`(512)、`normalized_name`(512)、`orcid`(64)、`affiliation`(TEXT)、时间戳 | `ix_authors_name` + **`uq_authors_normalized_name`（UNIQUE，2026-09-22 迁移 `0de3ab5e24dc`）** |
| `paper_authors` | `paper_id`、`author_id`、`author_order`(INT NOT NULL，默认 0)、`is_corresponding`(BOOL) | `UNIQUE(paper_id, author_id)`、`UNIQUE(paper_id, author_order)` |
| `paper_tags` | `id`、`name`(128)、`normalized_name`(128)、时间戳 | `UNIQUE(normalized_name)` |
| `papers_tags` | `paper_id`、`tag_id`、`kind`(32 NOT NULL，默认 `source_tag`)、`created_at` | `UNIQUE(paper_id, tag_id)`、`ix_papers_tags_tag_id` |

`kind ∈ {ieee_terms, author_terms, dynamic_index_terms, source_tag}`（`metadata_tags.py:22-25`）；落库入口 `link_tags:64` / `replace_kind:141` / `tags_by_kind:133`。

作者唯一性：`get_or_create_author`（`paper_service.py:181`）按 `normalized_name` 查行，旧实现用 `scalar_one_or_none()`，重复行会让整篇论文 `MultipleResultsFound` 失败；现在既加了唯一约束（迁移 `0de3ab5e24dc`，先把重复行的关联重指到最老行再删重复行），查找也改成**容错**（最老行优先 + 多条时 WARNING），未跑迁移的库照常工作。

### 3.8 四个部分唯一索引 = 模型不变量

| 不变量 | 索引 | 定义处 | 违反时会怎样 |
|---|---|---|---|
| 一个标识符只属一篇论文 | `uq_paper_identifiers_scheme_value` | `models.py:545` | 同一 DOI 注册不上第二篇；删除论文时**必须**释放标识符行 |
| 每个字段只有一个当前值 | `uq_paper_field_provenance_current` | `models.py:599` | 翻转当前值必须在同一事务里先降旧行 |
| 每篇论文只有一个主版本文件 | `uq_paper_files_primary` | `models.py:334` | 主版本翻牌必须在同一事务内改完所有行 |
| 指纹只被存活论文占用 | `uq_papers_fingerprint_live` | `models.py:89` | 软删行保留原指纹供追溯，删除即释放 |

---

## 4. 实现架构

### 4.1 模块分工（`app/services/`，4727 行）

| 模块 | 行数 | 职责 | 主要入口 |
|---|---|---|---|
| `provenance_service.py` | 626 | 字段级账本 + **唯一写入口**（claim → `papers` 列） | `record_claim:165`、`promote:209`、`set_field:220`、`rollback_field:253`、`write_field:300`、`recorded_conflicts:533` |
| `metadata_import.py` | 901 | 三格式外部记录导入 | `detect_format:108`、`parse_records:554`、`import_records:615`、`_import_one:651`、`import_payload:833`、`import_file:854` |
| `metadata_service.py` | 457 | PDF 首页启发式抽取（标题/作者/年份/venue/DOI） | 供 `tasks._backfill_metadata` 调用 |
| `metadata_identifiers.py` | 450 | 标识符规范化、主标识符、指纹 | `normalize_identifier:72`、`primary_identifier:118`、`upsert_identifier:196`、`replace_identifier:270`、`refresh_primary:301`、`upgrade_fingerprint:353`、`mirror_legacy_columns:397` |
| `metadata_merge.py` | 427 | R2 合并裁决与落库 | `decide:172`、`apply_decision:263`、`merge_values:320`、`conflict_report:375` |
| `metadata_matcher.py` | 422 | 五步匹配（外部记录 ↔ 已入库论文） | `match_record:267` |
| `venue_service.py` | 405 | venue / edition 解析与写入（只填空） | `get_or_create_edition:204`、`resolve_venue:252`、`attach_venue:285` |
| `metadata_manual.py` | 328 | 人工编辑、回滚、`GET metadata` 视图 | `patch_metadata:110`、`rollback_metadata:259`、`metadata_view:272` |
| `metadata_sources.py` | 308 | 来源行幂等写入、人工归属、复核查询 | `upsert_source:127`、`attach_source:202`、`review_queue:230` |
| `metadata_shell.py` | 233 | 壳论文（`AWAITING_FILE`）建立与采纳 | `create_shell:30`、`adopt_paper:133`、`attach_source_to_shell:220` |
| `metadata_tags.py` | 170 | 标签字典与 kind 分类落库 | `link_tags:64`、`replace_kind:141`、`tags_by_kind:133` |

### 4.2 写入路径：`write_field` 是唯一的「字段名 → papers 列」映射

`write_field`（`provenance_service.py:300`）按字段分派，映射表 `SIMPLE_FIELDS`（`:61-72`）是 10 个标量 → `papers` 列的一一对应；其余走结构化写入：

| 字段 | 落点 |
|---|---|
| `SIMPLE_FIELDS` 的 10 个标量 | 同名 `papers` 列（`title` / `abstract` / `language` / `year` / `volume` / `issue` / `pages` / `publication_date` / `paper_type` / `url`） |
| `venue` | `_write_venue:364` → `venues` + `venue_editions` + `papers.venue_id/venue_edition_id/venue_year` |
| `authors` | `_write_authors:406` → `authors` + `paper_authors` |
| `identifier:<scheme>` | `_write_identifier:431` → `paper_identifiers` + 镜像列 + 指纹 |
| `tag:<kind>` | `_write_tags:463` → `paper_tags` + `papers_tags` |

一次写入的顺序：`record_claim`（记声明，append-only）→ `promote`（把这条翻成 `is_current=true`，同时降旧行）→ 写 `papers` 列。`set_field` 是带 `override` 语义的封装，`manual` 用它强制覆盖。

### 4.3 合并规则 R2（现状）

三类来源集合（`metadata_merge.py:53-68`）：

| 集合 | 取值 |
|---|---|
| 结构化（可覆盖启发式） | `ieee_api` / `arxiv_api` / `crossref` / `pdf_embedded` / `import_file` / `manual` |
| 启发式（唯一会被覆盖的一方） | `pdf_heuristic` |
| 不受约束 | `manual`（PATCH 想改什么就改什么） |

`decide`（`:172`）对单个字段给出三种动作：`filled` / `overridden` / `conflict`。规则只有两条：

1. **只填空**：列里已有值就不覆盖，但照样记一条 `is_current=false` 的声明（历史不丢，可回滚）。
2. **一个例外**：`pdf_heuristic` 的值可以被任何结构化来源覆盖。结构化来源**之间不比较权威性**——分歧保留现值并登记为冲突。

字段特例（`_special_winner:163`）：`abstract` 取最长、`authors` 取最多、`year` 冲突时保现值。

落库与报告：`apply_decision:263` → `merge_values:320`（批量）→ `conflict_report:375`；复核清单由 `recorded_conflicts:533` 生成，**只列"真冲突"**（`is_current=false` 且值不同、来源不是 `pdf_heuristic`、论文未删）——"结构化覆盖启发式"是规则 2 的正常工作，不出现在清单里。

### 4.4 五步匹配器（`match_record`，`metadata_matcher.py:267-340`）

| 步 | 依据 | 置信度 | 结果 |
|---|---|---|---|
| 1 | `paper_identifiers`，按 `doi > arxiv > ieee_article_number > issn` 顺序探测（`_MATCH_SCHEMES:67-72`） | 1.0 | `matched`，method 同名（`METHOD_*:51-59`） |
| 2 | `paper_files.sha256` | 1.0 | `matched`，`sha256` |
| 3 | 规范化标题 + 首作者 + 年份三者全中 | 0.8 | `matched`，`title_year_author` |
| 4 | 仅标题命中（候选列表，最多 200 个，`_TITLE_CANDIDATE_LIMIT:76`） | 0.5 | **`ambiguous`**，不自动挂 |
| 5 | 文件名归一化相等 | 0.5 | `ambiguous` |
| — | 都没有 | — | `unmatched` → 导入侧建壳 |

每步都跳过 `exclude_paper_id`（摄取时传本次新建的论文，绝不匹配自己，`:272-278`）。`pending` 不是匹配器的输出，而是 `paper_sources.match_status` 的列默认值。

### 4.5 摄取链：PDF → 归属哪篇论文 → 主版本 → 元数据回填

1. `_run_pipeline`（`tasks.py:442`）：进 `PROCESSING` → `STORED` 注册文件后，调 `_resolve_target_paper`（`:734`）。
2. `_match_existing_paper`（`:783`）：先读 PDF 内嵌元数据（Info / XMP）匹配，失败再跑首页启发式匹配；命中已有论文（含壳）→ `metadata_shell.adopt_paper`，把 `reused_paper_id` / `match_method` 写进 `job.payload`。
3. `apply_primary_selection`（`paper_service.py:465`）判主版本；非主版本立刻 `_finish_non_primary`（`tasks.py:853`）结束。
4. 主版本继续：`_reset_placeholder_title`（`:843`）→ `_backfill_metadata`（`:869`）→ `_restore_placeholder_title`（`:859`）→ 指纹升级（`sha256` 撞车则丢弃本次论文）→ 切块 → 嵌入 → `delete_by_paper_id` + `bulk_index_chunks` → `INDEXED`。
5. `_backfill_metadata` 按层写声明：第 1 层 `pdf_embedded`（结构化，confidence 1.0）→ 第 2 层 `pdf_heuristic`（confidence 0.5），每层各自 `upsert_source` 后 `merge_values`。

主版本四场景：

| 场景 | 行为 |
|---|---|
| 首个版本 | `primary`，正常解析索引 |
| 更高优先级后到 | `promoted`：同一次流水线继续 `PARSING→INDEXING`，用 `delete_by_paper_id` 换掉旧文档 |
| 更低优先级后到 | `non_primary`：只登记文件；作业 `COMPLETED` + `payload["indexed"]=false`、`reason="non_primary_version"` |
| 主版本被移除 | `remove_file`（`paper_service.py:507`）按优先级重选；无剩余文件 → `FAILED`，索引留待人工 |

### 4.6 壳论文（`AWAITING_FILE`）：元数据先、PDF 后

- 建壳：`create_shell`（`metadata_shell.py:30`）——`status=AWAITING_FILE`，指纹取主标识符，`title` 先留空，合并填值后再补占位标题；**无 `paper_files`、无 chunk、不索引**。
- 采纳：PDF 到达且匹配到壳 → `adopt_paper`（`:133`）：改 `paper_files.paper_id` 与 MinIO 键、`is_primary=false`、补 `source_id`；**先把 `ingestion_jobs` 改指到壳论文再删临时论文行**（否则 `cascade="all, delete-orphan"` 会把作业一起删掉），最后把壳置 `PENDING` 走正常流水线——**复用同一个 `paper_id`**。
- 长期无 PDF：可按 `status=AWAITING_FILE` 查（`GET /api/papers?status=AWAITING_FILE`），**不自动删除**。

### 4.7 HTTP 端点（7 个 + 1 个配套）

| 端点 | 作用 | 出处 |
|---|---|---|
| `POST /api/metadata/import` | 导入外部记录，**默认 dry_run** | `api/metadata.py:92` |
| `GET /api/metadata/review` | 复核清单（`pending` / `ambiguous` 来源 + 真冲突） | `:147` |
| `POST /api/metadata/sources/{source_id}/attach` | 人工把来源挂到某篇论文并重放合并 | `:164` |
| `POST /api/metadata/apply` | 批量应用人工决定（`mode=overwrite` 走 PATCH 语义） | `:217` |
| `GET /api/papers/{id}/metadata` | 当前值 + 字段账本视图 | `api/papers.py:193` |
| `PATCH /api/papers/{id}/metadata` | 人工编辑（未知键进 `rejected`；改 `doi`/`arxiv_id` 会升级指纹） | `:186` |
| `POST /api/papers/{id}/metadata/rollback` | 把某字段回滚到历史声明 | `:206` |
| `POST /api/papers/{id}/reindex` | 让元数据改动进入检索过滤（配套，不是元数据端点） | `:275` |

契约：请求体既不是 multipart 也不是 JSON → 415（`_payload_from_request:60-89`）；`source_type` 非法 → 422；同一记录重复导入计入 `unchanged`，不产生第二行来源。

### 4.8 脚本

| 脚本 | 作用 |
|---|---|
| `scripts/backfill_metadata.py` | 给历史论文补来源行 / 声明 / 标识符 / 主版本标记，**幂等**，`--dry-run` / `--limit`；从不改 `papers.fingerprint` |
| `scripts/import_metadata.py` | CLI 导入（`--apply` / `--limit` / `--source-type` / `--report`） |
| `scripts/acceptance_metadata.py` | 真机验收 7 项：回填数字、IEEE 按 DOI 命中并 apply、无 DOI 记录进复核、手动改+回滚+指纹升级、venue 检索（需 reindex）、元数据先 PDF 后、索引一致性。**只在自己的壳论文上造数据**，`--cleanup` 删掉本次造的行 |

---

### 4.9 元数据如何传播到检索与读接口（2026-09-22）

- **索引快照**：`app/search/snapshot.py::paper_metadata_snapshot`（`:74`）把当前值写成 chunk 文档上的可过滤字段
  （`venue`/`venue_year`/`paper_type`/卷期页/`publication_date`/`identifiers`/四个 tag kind），INDEXING 阶段由
  `_index_rows`（`tasks.py:1051`）调用。**改元数据不会自动改变检索过滤**：要么 `POST /api/papers/{id}/reindex`
  （重算向量，≈1 chunk/s），要么 `uv run python scripts/refresh_index_metadata.py`（只改快照、秒级；2026-09-22
  真机 2883 文档全部更新、0 失败）。
- **读接口**：`PaperOut`（`app/schemas/paper.py:24`）直接输出当前值列 `volume/issue/pages/publication_date/
  paper_type/venue_edition_id/venue_year`；`GET /api/papers` 支持 `venue`/`year_from`/`year_to`/`paper_type`/`tag`
  过滤（**读 PG 当前值**，与检索读快照是两条路）。
- **一致性**：`GET /api/consistency` 核对三端（元数据本身不参与，它只在 PG）。
- **测试落点**：`tests/test_index_snapshot.py`（9）、`tests/test_refresh_index_metadata.py`（13）、
  `tests/test_paper_list_filters.py`（10）、`tests/test_consistency.py`（22）、`tests/test_author_links.py`（5）。

## 5. 配置项

元数据层**没有自己的配置键**：来源类型、scheme、置信度、字段名、主版本优先级、R2 三类集合全是代码常量（`app/services/metadata_*.py` 顶部）。只有这些既有键会间接影响它：

| 键 | 作用 | 出处 |
|---|---|---|
| `OPENSEARCH_URL` | 验收脚本直查文档数 | `app/core/config.py:55` |
| `PAPER_API_KEY` | 验收脚本调 API 的鉴权头 | `:72` |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` | 写进 chunk 文档，随过滤字段进索引 | `:65-66` |

CLI 参数（非配置）：`backfill_metadata.py --dry-run/--limit`、`import_metadata.py --apply/--limit/--source-type/--report`、`acceptance_metadata.py --base-url/--cleanup/--opensearch-url`。

---

## 6. 测试位置与覆盖

| 测试文件 | 行数 | 覆盖 |
|---|---|---|
| `tests/test_metadata_models.py` | 229 | 表名、服务依赖的列、四个部分唯一索引（纯模型断言，不连 PostgreSQL） |
| `tests/test_metadata_identifiers.py` | 378 | 标识符阶梯 `DOI > arXiv > 标题+首作者+年 > sha256`；IEEE `article_number` 落在标识符表 |
| `tests/test_metadata_matcher.py` | 284 | 第 1–3 步可自动挂；仅标题命中**不得**自动挂（转复核） |
| `tests/test_metadata_merge.py` | 356 | R2 三条分支：填空 / 结构化覆盖启发式 / 结构化对结构化留现值并登记冲突 |
| `tests/test_metadata_api.py` | 480 | HTTP 契约：`dry_run` 默认、415、人工归属两侧不存在时 404 |
| `tests/test_metadata_scripts.py` | 407 | 回填幂等、从不改指纹、CLI 报告（session 打桩到 SQLite） |
| `tests/test_awaiting_file.py` | 475 | 元数据先建壳 → PDF 到达复用同一 `paper_id` |
| `tests/test_manual_metadata.py` | 404 | `manual` 编辑立即生效且可回滚 |
| `tests/test_primary_version.py` | 229 | 主版本优先级；非主版本不解析；`needs_reindex` 标志 |

真机验收：`uv run python scripts/acceptance_metadata.py --cleanup`（需 API 已启动），7 项见 §4.8，实测数字在 `docs/progress/project.md` §17。

---

## 7. 边界与已知行为（现状）

**做**：多来源记录、五步匹配、R2 合并、字段级账本与回滚、venue 两级、主版本选择、壳论文复用、三格式导入、人工编辑。

**不做**：

- **网络抓取**：`ieee_api` / `arxiv_api` / `crossref` 只有取值与落库路径，没有任何调用代码（`metadata_sources.py:27-33` 只是常量）。
- **站点专用 translator、RIS / BibTeX 解析**：只认 IEEE Xplore raw / CSL-JSON / 通用 JSON。
- **自动合并重复论文**：重复只被报告（`backfill_metadata.py` 的 `duplicate_identifiers`），由人决定。
- **`author_identifiers`**：作者只存名字，IEEE 的作者 id 丢弃。
- **venue 别名消歧**：只做 `normalize_text` 级归一化（`venue_service.py:112`）。
- **改元数据自动 reindex**：需要显式 `POST /api/papers/{id}/reindex`。
- **回填调度**：`backfill_metadata.py` 是一次性幂等脚本，没有增量调度。

**已知行为（不是缺陷，读代码时会撞上）**：

- `papers.external_id`（`models.py:89`）全仓无引用，是历史遗留列。
- `ImportReport.unmatched`（`metadata_import.py:593`）有字段无自增点，恒为 0；没有证据的记录一律计入 `created_shell`。
- `PrimaryOutcome.needs_reindex`（`paper_service.py:455-457`）只有单测引用，app 无调用方——主版本翻牌由同一次流水线继续索引完成。
- `papers.doi` / `papers.arxiv_id` 是镜像列，改它们必须同时改 `paper_identifiers`（走 `replace_identifier` + `mirror_legacy_columns`）。
