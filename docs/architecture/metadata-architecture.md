# paperbox 元数据架构（多来源 / 通用格式 / 外部导入）

| 项 | 内容 |
|---|---|
| 状态 | **已实现并真机验收通过（2026-09-21）**。实现计划见 `.hermes/plans/2026-09-21_201622-metadata-discovery-storage.md`；验收脚本 `scripts/acceptance_metadata.py`（7 项全 PASS），实测数字见 `docs/progress/project.md` §17 |
| 适用对象 | 所有接触元数据、导入流水线、匹配/合并逻辑的开发与 Agent |
| 与其它文档的关系 | `.hermes/plans/2026-09-10_215600-paperbox-master-plan.md` 是需求权威；`AGENTS.md` 是环境契约与执行纪律；`docs/architecture/MVP-SPEC.md` 是接口摘要；**本文件是元数据架构的权威**（冲突时以本文件为准，并同步修订本文件） |
| 一句话 | **论文（papers）← 来源记录（paper_sources）← 字段声明（paper_field_provenance）**，标识符（paper_identifiers）作去重骨架，venue 与年份分离存储 |

---

## 1. 目标与约束

**已知约束（来自主人）**

1. 需支持 IEEE、arXiv 等多平台；**平台众多 → 不给每个平台定格式**，用通用格式；
2. DOI 是跨平台"论文身份证"，要重视；**部分论文没有 DOI → 必须有兜底**；
3. 已有 IEEE 批量获取元数据的方式 → 系统要支持**外部导入元数据并与已入库论文匹配**；
4. 论文**可能不止一个来源**（同一篇论文：IEEE 正式版 + arXiv 预印本 + 本地 PDF）；
5. venue 粒度 = **会议 + 年份分开存**，搜索"会议"或"会议+年份"都要能命中；
6. 作者只记名字即可；IEEE `article_number` 要存但归入标识符表。

**硬约束（项目级，不可违反）**：不引入 Redis/Celery；应用 `--workers 1`；无前端（API 驱动）；
不改动 `paper_chunks`/`ingestion_jobs`/`search_queries`/`authors` 的表结构；零新增依赖。

---

## 2. 设计原则（借鉴 Zotero 的什么、不借鉴什么）

**借鉴**

| Zotero 的做法 | 本项目的落地 |
|---|---|
| 发现分层：本地内嵌 → 通用网页元数据 → 标识符解析 → 站点专用抓取 → 模糊反查 → 外部导入 | 同顺序分层（§5），但**不做站点专用抓取**（脆且维护成本高）；外部导入是正式通道 |
| 通用 schema + 自由扩展袋（`extra`）承载平台特有 ID | `paper_identifiers`（结构化，比无结构 `extra` 更强）+ `paper_sources.raw`（原样快照） |
| 主键是本地 key，**标识符只是字段** | `papers.id`（UUID）是主键；DOI/arXiv/IEEE 号都在 `paper_identifiers` |
| 去重/合并是交互式的（候选 + 字段级挑选） | 无 UI → 用"自动只填空 + 待复核清单 + 可回滚"等价替代（§8、§9） |
| 交换格式用 CSL-JSON | 导入/导出接受 CSL-JSON（同时支持 IEEE raw JSON 与本项目通用 JSON） |

**不借鉴 / 做得更好**

- 不做站点专用 translator（IEEE Xplore 之类）；平台接入一律走 API（§14）。
- **字段级 provenance**：Zotero 基本没有，本项目把"哪个字段、来自哪个来源、何时、置信度、是否被覆盖"全记下来（§9），这是无 UI 情况下唯一的事后可解释手段。

**三层模型（核心）**

```
papers（论文实体）──1:N── paper_sources（来源记录）──1:N── paper_field_provenance（字段声明）
   │                          └── raw JSONB（来源原样）
   ├──1:N── paper_identifiers（合并后的规范标识符；唯一约束骨架）
   ├──1:N── paper_files（原件；is_primary 决定哪份入库）
   ├──N:M── paper_tags（papers_tags.kind 区分索引词类别）
   ├──N:M── authors（paper_authors.author_order 保序）
   └──N:1── venues ──1:N── venue_editions（年份/地点/日期）
```

**为什么这么分**：`papers` 上的列是**物化的当前值**（供检索与过滤，快），真相与历史在 provenance；
多来源不会把 `papers` 撑成"每来源一组列"，也不会因为合并而丢掉"另一个来源说什么"。

---

## 3. 数据模型

### 3.1 `paper_sources`（新增）

**作用**：记录"这篇论文被哪些来源描述过"；**未匹配上的来源也先落这里**（元数据先到、PDF 后到的场景）。

| 列 | 说明 |
|---|---|
| `id` | UUID 主键 |
| `paper_id` | FK papers，**可空**（NULL = 尚未匹配） |
| `source_type` | `ieee_api` / `arxiv_api` / `crossref` / `pdf_embedded` / `pdf_heuristic` / `import_file` / `manual` |
| `source_ref` | 来源内稳定标识：`doi:…` / `arxiv:…` / `ieee:7065247` / `file:<path>:<sha256>` / `paper:<id>:heuristic` |
| `content_type` | 来源自报类型：journal / conference / preprint / early_access / standard / unknown |
| `raw` | **JSONB，来源原始记录原样**（IEEE 的 article 对象、arXiv Atom entry、CSL-JSON 条目） |
| `match_status` | `matched` / `pending` / `ambiguous` / `rejected` |
| `match_method` / `match_confidence` | 匹配依据与置信度 |
| `fetched_at` / `imported_at` / `importer` | 来源侧时间 / 入库时间 / 导入者标识 |

**约束**：`UNIQUE(source_type, source_ref)` → **幂等**（同一条来源记录重复导入不产生第二行）。
**为什么存 raw**：将来解析逻辑改了可以**重放**，不用重新抓/重新消耗 API 额度。

### 3.2 `paper_identifiers`（新增）

**作用**：合并后的规范标识符集合；**匹配与去重的骨架**；`papers.fingerprint` 的来源。

| 列 | 说明 |
|---|---|
| `paper_id` | FK papers（非空） |
| `scheme` | `doi` / `arxiv` / `ieee_article_number` / `issn` / `isbn` / `pmid` / `openalex` / `semantic_scholar` / `url` / `sha256` |
| `value` / `normalized_value` | 原样 + 规范化（复用 `paper_service.normalize_doi` / `normalize_arxiv_id`） |
| `first_source_id` | FK paper_sources（谁最先带来它） |
| `is_primary` | 决定 `papers.fingerprint`（按 `DOI > arXiv > 标题+首作者+年 > sha256`） |

**约束**：`UNIQUE(paper_id, scheme, normalized_value)`；
**`UNIQUE(scheme, normalized_value) WHERE paper_id IS NOT NULL`** ← 一个标识符只属于一篇论文（去重底线）。
多来源声明同一 DOI 时这里只有一行，各来源的主张在 provenance（`field='identifier:doi'`）→ **不需要第三张关联表**。

### 3.3 `paper_field_provenance`（新增）

**作用**：字段级账本——谁写的、何时、置信度、是否当前生效、被谁覆盖过。

| 列 | 说明 |
|---|---|
| `field` | `title` / `abstract` / `year` / `venue` / `volume` / `issue` / `pages` / `authors` / `publication_date` / `paper_type` / `identifier:doi` / `tag:ieee_terms` … |
| `value` | JSONB（标量或结构：作者列表、标识符） |
| `source_id` | FK paper_sources（NULL = 系统/人工） |
| `confidence` / `decided_by` / `decided_at` | 置信度 / `initial` \| `structured_override` \| `manual` / 裁决时间 |
| `is_current` | 该字段当前生效值 |

**约束**：`UNIQUE(paper_id, field) WHERE is_current`（每字段一个当前值）；**历史行永不删除**（回滚 = 把历史行置回 `is_current=true`）。

### 3.4 `venue_editions`（新增）+ `venues`（复用）

- `venues`：会议/期刊**实体**（`name` / `normalized_name` / `kind`（已有列）/ `publisher` / **`issn`（新增）**）——**不含年份**。
- `venue_editions`：`venue_id` + `year` + `location`（IEEE `conference_location`）+ `dates`（`conference_dates`）+ `publication_number` + `is_number`，`UNIQUE(venue_id, year)`。
- `papers` 侧：`venue_id`（已有）+ `venue_edition_id` + `venue_year`（冗余，供快速过滤）。

**检索语义**：搜"会议"→ 命中 `venues.name`；搜"会议 + 年份"→ `venue_id + venue_year`（或 edition）；**绝不把"会议 2015"拼成一个词条**。

### 3.5 `papers`（改造）

| 变化 | 说明 |
|---|---|
| 新增列 | `volume` / `issue` / `pages`（"631-635"）/ `publication_date`（IEEE 只给到月）/ `paper_type`（journal/conference/preprint/early_access/standard）/ `venue_edition_id` / `venue_year` |
| 新增状态 | **`AWAITING_FILE`**（元数据先到、等 PDF 的壳论文） |
| 保留 | `doi` / `arxiv_id` 作为主标识符的**便捷镜像**；`fingerprint` 语义不变（部分唯一索引）；`external_id` 是死列，暂不清理 |

### 3.6 `paper_files`（改造）

| 变化 | 说明 |
|---|---|
| 新增列 | `source_id`（该 PDF 由哪个来源带来）、**`is_primary`**（唯一入库的那份） |
| kind 取值 | `original` / `arxiv_pdf` / `published_pdf` / `supplement` |
| 约束 | `UNIQUE(paper_id) WHERE is_primary AND deleted_at IS NULL` |

### 3.7 `papers_tags`（改造）与 `authors`（不动）

- `papers_tags` 加 `kind ∈ {ieee_terms, author_terms, dynamic_index_terms, source_tag}`：
  IEEE 的三类索引词（官方主题词 / 作者关键词 / 系统扩展词）得以区分与过滤；将来 arXiv categories 或自定义标签共用同一结构。
- `authors` / `paper_authors` **表结构不动**：只写 `name` / `normalized_name` / `author_order`；
  `orcid` / `affiliation` 保留空列**不填**（IEEE 的作者 ID 与机构只留在 `paper_sources.raw`）。

---

## 4. 存储位置与数据分层

| 数据 | 位置 | 理由 |
|---|---|---|
| 结构化元数据当前值、来源记录、provenance、标识符、venue/edition、标签 | **PostgreSQL** | 唯一事实源；需要唯一约束、索引、事务 |
| 来源原始快照 `raw` | **PG 的 JSONB 列** | 体量小（IEEE 一条 ≈8KB，1000 条 ≈8MB）、可用 `jsonb` 操作符查询、**不污染检索索引** |
| PDF 原件（多来源各一份） | **MinIO** | 二进制、大、需要流式与 sha256 去重 |
| 可检索文本 + 向量 | **OpenSearch** | 只放 chunk（文本 + 向量），**不放元数据快照** |

---

## 5. 发现分层与本期范围

| 层 | 机制 | 本期 |
|---|---|---|
| 1 | **PDF 内嵌元数据**（Info 字典 + XMP：`dc:title`、`prism:doi`…），零网络 | ✅ 做（`source_type='pdf_embedded'`） |
| 2 | 首页文本启发式（现有：字号判标题、作者块、摘要、年份、DOI/arXiv 正则） | ✅ 已有（`source_type='pdf_heuristic'`） |
| 2b | **文件名标识符提示**：`1706.03762__topic.pdf` 的主干给出 arXiv id | ✅ 做（`source_type='filename'`，2026-09-30） |
| 3 | **外部导入**（IEEE 批量 JSON / CSL-JSON / 本项目通用 JSON） | ✅ 做（`import_file`） |
| 4 | DOI 内容协商（`Accept: application/vnd.citationstyles.csl+json`） | ⏳ 列入计划，本期不做（`crossref`） |
| 5 | 平台 API：IEEE Xplore（`article_number`/`doi`）、arXiv API（Atom） | ⏳ 列入计划，本期不做（`ieee_api` / `arxiv_api`） |
| 6 | 模糊反查（Crossref/OpenAlex 标题检索） | ⏳ 列入计划，本期不做；**必须带置信度 + 复核** |
| 7 | 人工（单篇手动更新 / 人工归属） | ✅ 预留（`manual`） |

> 关键点：**本期不做网络层，但存储模型已经为它们留好位置**——接入时只加一个 importer，不动表。

摄取时管道按 **1 → 2 → 2b** 的顺序跑（`app/workers/tasks.py::_backfill_metadata`，代码注释里称这三步为 layer 1/2/3）：
先读 PDF 自己的说法，再读首页，最后才看文件名。2b 是**最低权威**的一层，理由见 §6 与 §8。

---

## 6. 标识符体系与兜底链

- `scheme` 是**开放枚举**（不是列）：接入新平台只加取值，不改表结构。
- 规范化：DOI（小写、去 `https://doi.org/` 前缀）、arXiv（去版本号）、ISSN（去连字符）等，复用 `paper_service` 已有函数。
- **兜底链**（无 DOI 时）：`DOI > arXiv > 规范化标题 + 首作者 + 年 > 文件 sha256`——这条链**已经存在**于 `papers.fingerprint`，
  本次只是把它的输入从"两个固定列"改为"`paper_identifiers` 的主标识符"。
- 标识符变更（如导入带来 DOI）→ **指纹升级**，复用既有 `_upgrade_fingerprint` 路径（含 reindex 的 `dedupe=False` 守卫）。
- **文件名给的 id 是最低一档证据**（2026-09-30 加）：只在论文还不知道任何 arXiv id 时补空，而且**当这个 id 已属于另一篇活论文时直接丢弃**（不写声明、不写来源行）——
  标识符表本来就会拒绝挂载（`uq` 约束），但声明会被留下来，形成"论文声称自己持有某个不属于它的 id"，回滚时还会被写到 `papers.arxiv_id` 上。
  名称与身份表打架属于**查重信号**，交给五步匹配器（§7 步 5），不属于元数据填充。

---

## 7. 匹配（外部记录 ↔ 已入库论文）

| 步 | 依据 | 置信度 | 结果 |
|---|---|---|---|
| 1 | `scheme + normalized_value` 命中 `paper_identifiers` | 1.0 | 自动 `matched` |
| 2 | `paper_files.sha256` 命中 | 1.0 | 自动 `matched` |
| 3 | 规范化标题 + 首作者 + 年份（三者全中） | 0.8 | 自动 `matched` |
| 4 | 仅标题命中 | 0.5 | `ambiguous`（进复核清单，**不自动挂**） |
| 5 | 文件名归一化相等 | 0.5 | `ambiguous` |
| 6 | 都没有 | — | 建**壳论文**（`AWAITING_FILE`）或记为 `unmatched`（`dry_run` 时只报告） |

复核与人工：`GET /api/metadata/review`（pending/ambiguous 来源 + 冲突字段）、`POST /api/metadata/sources/{id}/attach`（人工归属）。

---

## 8. 合并规则 R2（**不引入来源权威性排序**）

1. **只填空**：目标字段已有值 → 不覆盖，只写 provenance（`is_current=false`）并登记冲突。
2. **唯一例外**：现有值的来源是**弱来源**（`pdf_heuristic`、`filename`），而新值是**结构化来源**（`ieee_api`/`arxiv_api`/`crossref`/`import_file`/`pdf_embedded`/`manual`）→ 覆盖，`decided_by='structured_override'`，旧值保留为历史行。
   两个弱来源互相矛盾时按规则 3 处理（只填空 + 登记冲突），不比较谁更可信。
3. **结构化来源之间**：只填空 + 登记冲突，**不比较谁更权威**。
4. **字段特例**：`abstract` 取最长；`authors` 取条数最多的一份；`year` 冲突保留现值并登记。
   **只适用于结构化来源之间**（2026-10-10 补）：这条的前提是"各来源截断程度不同，所以更长 = 更完整"，
   而弱来源（`pdf_heuristic` / `filename`）不是截断而是**误读** —— 它把标题碎片、摘要句子当成人名，
   列表反而更长。所以规则 2 必须双向成立：弱来源既不能靠"更新"覆盖结构化值，也不能靠"更长"赢它；
   两个弱来源之间按规则 3 处理（保现值 + 登记冲突），规则 4 不替它们比长度。
5. **回滚**：把某条历史 provenance 置回 `is_current=true`，并把当时的 `papers` 列写回；不删历史。

> 为什么这样：现有 68 篇的历史元数据全部由启发式写入（回填时统一标 `pdf_heuristic`），因此 IEEE 导入能修正它们；
> 同时避免了维护一张"谁比谁权威"的排序表。

**更正 = 替换，不是并存**（2026-09-30）：R2 判成覆盖（或回滚、手动改）时，`provenance_service._write_identifier` 走
`replace_identifier`——**删掉同 scheme 的旧行**并强制镜像列跟随。只插入新行是不够的：`primary_identifier` 取"同 scheme 里最早的那一行"，
于是 `papers.arxiv_id` 与 `papers.fingerprint` 会继续指向刚被否定的旧值（指纹还直接决定查重）。

---

## 9. 字段级 provenance 与手动更新

| 端点 | 作用 |
|---|---|
| `GET /api/papers/{id}/metadata` | 当前值 + **每字段的来源与历史**（谁写的、何时、被谁覆盖、可回滚候选） |
| `PATCH /api/papers/{id}/metadata` | 手动设字段：建/复用 `source_type='manual'` 来源行，写 `decided_by='manual'` 的 provenance 并**强制覆盖**；标识符变更同步 `paper_identifiers` + 指纹升级 |
| `POST /api/papers/{id}/metadata/rollback` | `{"field": …, "provenance_id": …}` → 回滚该字段到指定历史主张 |
| `POST /api/metadata/apply`（可选批量） | 从报告文件批量应用人工决定（`mode:"overwrite"`, `fields:[…]`） |

**`manual` 是唯一不受 R2 约束的来源**（人工意志优先），但仍写 provenance，因此仍可回滚。

---

## 10. 两种导入顺序

**A. PDF 先（现状）**：流水线建 paper（sha256 指纹 + 首页启发式）→ 之后导入元数据时按 §7 匹配 → 挂 `paper_sources` + 合并 + 升级指纹。

**B. 元数据先（新增）**：
1. 导入匹配不到论文 → 建**壳论文**（`status='AWAITING_FILE'`，字段落库、`fingerprint` 取主标识符、**无 chunks 无文件**）；
2. PDF 到达 → 流水线**先找壳**（sha256 → 内嵌 DOI → arXiv → 标题+年+作者）→ 命中则**复用同一 `paper_id`**（不新建），
   把 PDF 挂成 `paper_files(kind='original', source_id=…)`，状态转 `PENDING` 继续跑；
3. 壳长期无 PDF：可查（`GET /api/papers?status=AWAITING_FILE`）、housekeeping 报告，**不自动删除**。

---

## 11. 主版本规则（多来源 PDF 只入库一份）

| 规则 | 内容 |
|---|---|
| 优先级 | `published_pdf`（正式发表版） > `original`（首个导入） > `arxiv_pdf`（预印本） |
| 非主版本 | 存 MinIO、`paper_files` 有行（`is_primary=false`、带 `source_id`）、可在 `GET /api/papers/{id}` 列出；**不解析、不进 chunks、不进 OpenSearch** |
| 首个版本 | 直接成为主版本，正常跑完流水线 |
| 更高优先级后到 | 主版本翻牌 → **触发 reindex**（重新 PARSING→INDEXING、替换 chunks、删旧文档 + bulk 重索引） |
| 更低优先级后到 | 只登记文件；作业以 `COMPLETED` + `payload["indexed"]=false, reason="non_primary_version"` 结束，**不触碰已有索引** |
| 主版本被删除 | 按同优先级从剩余文件重选并 reindex；无剩余文件 → 论文转 `FAILED` 并保留索引待人工处理 |

**落地要点**：流水线在 `STORED` 注册文件后**先决定主版本**，再决定是否继续 `PARSING`；
`_run_pipeline` 读的对象必须是**主版本文件**，而不是"本次刚上传的那份"。

---

## 12. IEEE 记录映射（用真实样例逐字段）

| IEEE 字段 | 落点 |
|---|---|
| `doi` | `paper_identifiers(scheme=doi, is_primary)` + `papers.doi` |
| `article_number` | `paper_identifiers(scheme=ieee_article_number)` |
| `issn` | `paper_identifiers(scheme=issn)` + `venues.issn` |
| `title` / `abstract` | `papers.title` / `papers.abstract`（+provenance） |
| `publication_year` / `publication_date` | `papers.year` / `papers.publication_date`（月精度）+ `venue_editions.year` |
| `publication_title` | `venues.name`；`kind` 由 `content_type` 推 |
| `content_type` | `papers.paper_type` + `paper_sources.content_type` |
| `volume` / `issue` / `start_page` / `end_page` | `papers.volume` / `.issue` / `.pages` |
| `authors[].full_name` + `author_order` | `authors` + `paper_authors(author_order)`（**机构与作者 ID 不写结构化列**） |
| `index_terms.{ieee_terms,author_terms,dynamic_index_terms}` | `paper_tags` + `papers_tags(kind=对应类别)` |
| `html_url` / `pdf_url` / `abstract_url` | `papers.url`（html_url 优先）+ 全部留 `raw` |
| `publication_number` / `is_number` | `venue_editions.publication_number` / `.is_number` |
| `conference_location` / `conference_dates` | `venue_editions.location` / `.dates` |
| `citing_paper_count` / `download_count` / `insert_date` / `license` / `rank` | **不入结构化列**，只留 `raw` |

**导入通道**：`POST /api/metadata/import`（multipart/JSON，`dry_run` **默认 true**）+ `scripts/import_metadata.py`；
格式自动识别：含 `articles` → IEEE raw；JSON 数组含 `DOI`/`type` → CSL-JSON；否则本项目通用 JSON。

### 12.1 三种格式的字段清单（`app/services/metadata_import.py`）

| 格式 | 识别规则（`detect_format`） | 顶层形状 |
|---|---|---|
| `ieee_raw` | 对象里有 `articles` 数组 | `{"articles": [ {…}, … ]}` |
| `csl_json` | 数组里有元素带 `DOI` 或 `type` | `[ {…}, … ]` |
| `generic` | 其余（对象或数组都行） | `[ {…} ]` 或单个 `{…}` |

**`generic`（本项目通用格式）**：键就是 claim 字段名，值直接给。

- 直接字段：`title` `abstract` `year` `authors` `venue` `volume` `issue` `pages`
  `publication_date` `paper_type` `language` `url`
- 标识符：`identifier:doi` `identifier:arxiv` `identifier:ieee_article_number` `identifier:issn`
  （**只有这 4 个**走 generic 解析；`pmid`/`openalex`/`semantic_scholar` 等 scheme 只存在于
  `paper_identifiers` 侧，导入时要用 IEEE/CSL 形状或手动 PATCH）
- 标签：`tag:ieee_terms` `tag:author_terms` `tag:dynamic_index_terms` `tag:source_tag`
  （对应 `papers_tags.kind`）
- 别名（`_GENERIC_ALIASES`）：`doi`→`identifier:doi`、`arxiv_id`/`arxiv`→`identifier:arxiv`、
  `article_number`/`ieee_article_number`→`identifier:ieee_article_number`、`issn`→`identifier:issn`、
  `journal`/`conference`→`venue`、`type`/`content_type`→`paper_type`、`start_page`→`pages`、
  `keywords`→`tag:author_terms`、`tags`→`tag:source_tag`
- 值形状：`authors` 可以是数组，也可以是 `;` 分隔的字符串；`venue` 可以是字符串（等价
  `{"name": …}`）或对象 `{"name", "year", "content_type", "issn", "publisher", "location",
  "dates", "publication_number", "is_number"}`；`year` 可以是 `2021` / `"2021"` / `"2021-05"` /
  CSL 的 `date-parts`；`paper_type` 取 `journal|conference|preprint|early_access|standard`。
  空值（`null`/`""`/`[]`/`{}`）一律忽略。
- 匹配用元键（**不是** claim 字段）：`filename` `sha256` `path` —— 参与 `source_ref` 与
  文件名/摘要匹配，不会写进 `papers` 列。

**`ieee_raw`**：`doi` `arxiv_id` `article_number` `issn` `content_type` `title` `abstract`
`publication_year` `publication_date`（月精度→`YYYY-MM-01`）`authors[].full_name` + `author_order`
`publication_title`（→venue，`content_type` 推 kind）`volume` `issue` `start_page`/`end_page`（→`pages`）
`html_url`/`abstract_url`/`pdf_url`（→`url`，html 优先）
`index_terms.{ieee_terms,author_terms,dynamic_index_terms}` `conference_location` `conference_dates`
`publication_number` `is_number` `insert_date`/`fetched_at`/`retrieved_at`（→`fetched_at`）。
`content_type` 经 `venue_service.paper_type_for_content_type` 落 `papers.paper_type`。

**`csl_json`**：`DOI` `title` `abstract` `issued`/`published`（→`year`）
`author[].{family,given}` 或 `{literal}` 或字符串 `container-title`（→venue）`type`（→`paper_type`：
`article-journal`/`article-magazine`/`article`→journal，`paper-conference`/`proceedings-article`→conference，
`posted-content`→preprint，`standard`→standard）`ISSN` `volume` `issue` `page`（→`pages`）`URL`
`keyword`（字符串按逗号切或数组 →`tag:author_terms`）。

**`source_ref`（去重身份，`_source_ref`）**：`DOI > arXiv > IEEE article_number > 文件(path+sha256)
> record:<sha256(record)[:32]>`；与 `UNIQUE(source_type, source_ref)` 配合，同一记录重复导入计入
`unchanged`，不产生第二行来源。每份记录原文逐字存进 `paper_sources.raw`。

**`source_type`（`metadata_sources.SOURCE_TYPES`）**：`ieee_api` `arxiv_api` `crossref`
`pdf_embedded` `pdf_heuristic` `import_file`（默认）`manual`；未知值 → 422。

---

## 13. 术语表

| 术语 | 含义 |
|---|---|
| 来源（source） | 一份元数据描述从哪来（IEEE API / arXiv API / PDF 内嵌 / 首页启发式 / 导入文件 / 人工） |
| 字段声明（field claim） | 某来源对某字段的一次主张（值 + 置信度 + 时间），存 `paper_field_provenance` |
| 主标识符 | `paper_identifiers.is_primary` 的那条，决定 `papers.fingerprint` |
| 壳论文（shell paper） | 只有元数据、还没有 PDF 的论文（`status='AWAITING_FILE'`） |
| 主版本（primary file） | 该论文唯一入库（解析/索引）的那份 PDF |
| R2 | 合并规则：只填空 + 结构化来源可覆盖启发式；结构化之间不比较权威性 |

---

## 14. 未做但已列入计划

| 项 | 接入方式 |
|---|---|
| IEEE Xplore API | 用 `article_number`/`doi` 拉元数据（需 API key）→ `source_type='ieee_api'` |
| arXiv API | 用 arXiv id 拉 Atom → `arxiv_api` |
| DOI 内容协商 | `Accept: application/vnd.citationstyles.csl+json` → `crossref` |
| Crossref / OpenAlex 标题反查 | 无标识符时的模糊兜底，**必须带置信度 + 复核** |
| RIS / BibTeX 解析 | 本期只支持 IEEE raw JSON / CSL-JSON / 通用 JSON |
| venue 别名与消歧 | 本期只做 `normalize_text` 级归一化 |

---

## 15. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-21 | 建立本文件：三层模型、4 张新表、R2 合并规则、主版本规则、两种导入顺序、IEEE 映射（定稿，待实现） |
| 2026-09-21 | **实现完成并真机验收通过**：迁移 `7a2f4c9d51be`（4 张新表 + `papers`/`paper_files`/`papers_tags`/`venues` 加列）、服务 `metadata_{identifiers,sources,tags,merge,matcher,shell,import,manual}.py`、端点 8 个、脚本 3 个（`backfill_metadata` / `import_metadata` / `acceptance_metadata`）。两处与初稿的实现细化：① 删除论文时**释放**其 `paper_identifiers`（否则墓碑永久占住 DOI，与 `papers.fingerprint` 的"删除即释放"一致）；② 元数据改动后要经 `POST /api/papers/{id}/reindex` 才能进检索过滤字段（过滤字段在 chunk 文档上）。 |
| 2026-09-30 | 加**文件名标识符提示**（§5 层 2b、§6、§8）：`source_type='filename'`、`metadata_service.arxiv_id_from_filename`（`YYMM` 必须是真月份，`notes-2024.12345.pdf` 不算）、`tasks._may_offer_arxiv_id` 两道守卫（论文已知 arXiv id / 该 id 已属于别的活论文）；同时把"更正 = 替换 + 镜像跟随"写成 R2 的正式语义。 |
| 2026-09-23 | 补 §12.1：三种可导入格式（`ieee_raw` / `csl_json` / `generic`）的字段清单、别名表、值形状、`source_ref` 去重阶梯与 `source_type` 取值（此前只在 §12 末尾一句话带过）。 |
