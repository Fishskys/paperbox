# 元数据层（多来源 / 匹配 / 合并 / 手动编辑）

| 项 | 内容 |
|---|---|
| 状态 | 当前元数据模型、合并规则、质量审计和索引同步契约 |
| 表 | 当前值、来源、字段声明、标识符、刊会、作者和标签，以及 `metadata_quality_decisions` / `metadata_index_updates` |
| 代码 | `app/services/metadata_*.py`、`provenance_service.py`、`venue_service.py`；解析策略在 `app/parsing/metadata_fields.py`，重试任务在 `app/workers/metadata_sync.py` |
| 脚本 | `scripts/backfill_metadata.py`（历史论文回填，幂等）、`scripts/import_metadata.py`（CLI 导入）、`scripts/acceptance_metadata.py`（真机验收 7 项） |
| 相关文档 | `docs/architecture/metadata-architecture.md`（设计背景、逐字段映射、术语表）、`docs/progress/project.md` [#17](../progress/project.md#17-元数据架构多来源--通用格式--外部导入2026-09-21)（实测数字） |

---

## 1. 数据模型总览

### 1.1 表的职责

| 表 | 一行代表 | 谁写它 | 出处 |
|---|---|---|---|
| `papers` | 一篇论文（**当前值**的物化） | `provenance_service.write_field`、`metadata_merge` | `models.py` |
| `paper_sources` | 一份来源数据（一次导入 / 一次解析） | `metadata_sources.upsert_source` | `models.py` |
| `paper_field_provenance` | 一条字段声明（append-only 账本） | `provenance_service.record_claim` / `promote` | `models.py` |
| `paper_identifiers` | 一个标识符（DOI / arXiv / IEEE article number / …） | `metadata_identifiers.upsert_identifier` / `replace_identifier` | `models.py` |
| `venues` | 一个期刊或会议**实体** | `venue_service.resolve_venue` | `models.py` |
| `venue_editions` | 某实体**某一年的那一届** | `venue_service.get_or_create_edition` | `models.py` |
| `paper_files` | 一个 PDF 版本（含主版本标记、来源归属） | `paper_service.register_original_file` / `select_primary_file` | `models.py` |
| `authors` + `paper_authors` | 作者字典 + 论文↔作者（带顺序） | `paper_service.get_or_create_author` | `models.py` / |
| `paper_tags` + `papers_tags` | 标签字典 + 论文↔标签（带 `kind`） | `metadata_tags.link_tags` / `replace_kind` | `models.py` / |
| `metadata_quality_decisions` | 版本化自动决策、前后值及证据 | `metadata_quality.record` | `models.py` |
| `metadata_index_updates` | 一篇论文最新待同步的元数据版本 | `metadata_index_sync.enqueue` / `flush_pending` | `models.py` |

关系主轴（三层 + 一个骨架）：

```
papers ──1:N──> paper_sources ──1:N──> paper_field_provenance
 │ ▲ │ │
 │ └─────────────────┴── paper_identifiers ─┘（骨架：一个标识符只属一篇论文）
 ├──> paper_files（多版本，只有一个 is_primary）
 ├──> venue_id ─> venues ─1:N─> venue_editions ◄── papers.venue_edition_id + venue_year
 ├──> paper_authors ─> authors
 └──> papers_tags ─> paper_tags
```

### 1.2 存储分层（什么放哪）

| 内容 | 位置 | 说明 |
|---|---|---|
| 结构化当前值（标题/年份/venue/…） | PostgreSQL `papers` | 查询与列表直接读，不 join 账本 |
| 来源快照（IEEE 一条 raw 约 8KB） | PostgreSQL `paper_sources.raw`（JSONB） | 原样保存，**不进 OpenSearch、不进 MinIO** |
| 字段级账本 | PostgreSQL `paper_field_provenance` | 历史行永不删除，回滚靠翻 `is_current` |
| 标识符、venue、标签 | PostgreSQL 对应表 | 见 [#3](#3-表里有什么) |
| PDF 原件 | MinIO（`papers/<paper_id>/…`） | 元数据层只存 `paper_files.object_key` / `sha256` |
| 可检索文本 + 向量 + **过滤字段快照** | OpenSearch chunk 文档 | 字段服务改元数据时事务内登记同步任务，后台只更新快照；不需要重算向量 |

---

## 2. 设计思路

### PDF 候选字段语义

`EmbeddedMetadata` 区分 XMP `description` 摘要与 Info `Subject` 主题，`field_origins` 保留字段出处并随原始 Info/XMP 写入来源快照。Subject 不直接成为摘要，只有通过出版信息/模板检查且与正文摘要相符时，重新提取服务才提供该候选。DOI 的显式出版标记识别独立于摘要处理。

作者解析先解码 HTML 实体，再区分多人字符串、数组及倒置姓名。正文作者提取使用首页标题与摘要之间的原生行位置，过滤单位、邮箱、会员身份及脚注，支持单词姓名与非英文姓名。文字层缺失、损坏或检测到不同文字占用同一行位置时，来源快照记录 `quality.text_state` 和 `needs_review`，不从该文字层自动提供作者。

`metadata_reextract.pdf_candidates` 是摄取、重新提取和只读验收共用的候选提取入口。原生作者区块在首次机构/邮箱行前截止；有效内嵌名单与正文名单存在实质差异时，正文完整性不能自行确认为真。正文边界不确定的候选不直接填空，保留现值或留空，并将候选存入历史；不按作者人数选值。文字层缺失等文档级问题只写质量审计及计数，不生成用户必须处理的字段冲突。

正文摘要跨行合并时，`65 nm CMOS`、`3.4 GHz` 等数量行不作为章节标题截断；独立罗马数字章节标记和 Introduction / Index Terms 边界仍终止摘要。

提取明确遇到摘要结束边界时记录 `quality.abstract_complete`。与该完整正文摘要一致的值可以排除历史截断前缀；未证明正文边界时，不按文本更长自动排除另一个有效摘要。

### 持续自动决策与审计

`metadata_policy.pdf_choice` 统一服务于合并和复核。规则版本 `pdf-fields-v4` 先拒绝出版/模板摘要、机构/邮箱等无效作者候选，再比较 PDF 正文证据；结构化导入和人工结果仍受原有权威级别保护。摘要正文印证、可信且完整的作者区块可以修正另一个 PDF 来源；证据不足保留现值，不请求人工在提取方法之间裁决。

PDF 内嵌、正文启发式和文件名提供同一论文的内部候选，最终各字段只有一个 `is_current` 值。沿用 `source_type`、`decided_by`、来源中的质量证据和自动决策审计，不增加“临时/正式”元数据状态。内部来源的未选值属于历史，包含旧规则产生的差异；不会进入字段复核清单。自动处理不表示字段已经核实正确，缺项可等待外部导入补齐。唯一且无强身份冲突的外部记录可覆盖这些 PDF 值；人工决定、外部同级分歧和论文归属歧义仍受保护。

外部记录确认与 PDF 相同的字段值时，也追加外部来源的当前声明，旧声明仍在；不改展示值、不重复同步索引。后续 PDF 重新提取不能推翻该外部确认，重复导入不重复追加确认声明。

姓名缩写的点后空格，以及至少两个连续字母构成的姓名词末、后接空格或结尾的单个句点，仅参与等价比较，例如 `E.A. Wulf` 与 `E. A. Wulf`、`Changqing. Feng` 与 `Changqing Feng`；不改写实际姓名，也不合并重名。缩写句点、词间边界、字母差异、重音、连字符、人数和顺序保留；`Arai` 与 `Ami` 不能按格式等价处理。原生作者区块已满足完整性检查，且两份名单同序同人数、逐名仅有重音或标点排版差异时，允许该正文证据确定拼写。

`byline_abbreviations` 在 PDF 证据处理中匹配缩写与全名：名单同序同人数、姓氏一致、给定名词数一致，逐词相等或是对应首字母；同一缩写映射多个不同全名时拒绝自动匹配。完整且可靠的正文署名同时印证当前名单与历史候选时，保留当前写法并归档缩写差异，不扩写全库姓名或合并作者实体。缺少完整署名、名字字母不同、漏掉中间名、姓氏重音不同、人数或顺序变化时保留现值及内部证据，不制造人工任务。

作者候选中的会议栏目、技术标题词、出版收稿说明、学位标记、单位地址和损坏字形标记由共享 `author_problem` 拒绝；例如 `RANGE SENSORS` 不是姓名。标题片段比较兼容文字层漏空格。拒绝不依赖另一份名单已完整，也不依赖全大写、姓名词数或 ASCII 限制；非英文姓名、单词姓名和全大写的正常姓名保留。来源原文及拒绝原因写入审计，不能只删除复核列表中的显示项。

`metadata_quality_decisions` 以决策载荷指纹唯一约束实现幂等，保存规则版本、执行者、字段、前值、候选、所选值、原声明、来源、证据和执行时间。候选自身无效的拒绝按候选、来源和证据去重，不因后续正文填空反复记账；依赖现值的比较保留完整上下文。自动处理不写人工 `decision_at`；当前人工值及已人工否决的相同候选跨规则版本受到保护。机器归档的候选在新证据足够时仍可自动采纳，旧审计不删除。重复候选复用字段历史，内容或证据变化则保留新的来源快照。`quality_counts` 与结构化日志按动作和原因统计拒绝、自动解决、复核及文字层问题。

### PDF 历史证据补齐

`scripts/reconcile_pdf_quality_history.py` 用于给既有 PDF 作者/摘要差异补齐证据。默认只读捕获当前声明、实际展示值、全部未裁决 PDF 候选和主文件 SHA256，再以受限并发读取前两页文字、首页布局与内嵌字段；提取缓存绑定文件、论文上下文和解析代码指纹。`--candidates` 可指定已有诊断报告中的 `items` 作为范围。

`--apply --plan` 只应用已生成的预览，并重新计算决策。当前值、历史候选、主文件或规则发生变化会拒绝应用，人工结果始终受保护。只有正文证据支持的新值才能替换实际展示值；历史展示值与字段账本不一致时，证据不足便保留展示值。

历史作者修复还比较实际展示名单：有效名单出现实质字母、人数变化，或准备新引入末尾引号、姓名中的可疑句点、黏连姓名而减少词间边界时，不凭正文文字层自动覆盖。`--field authors` / `--field abstract` 可分别预览或应用。验证基准若经渲染 PDF 核实需要修正，`audit_pdf_metadata_quality.py --review-overrides` 接受带原值、校正值、PDF SHA256 和理由的独立记录；报告保留校正依据，不改写原始基准或放宽姓名等价规则。

应用时追加带证据的来源和当前声明，保留原始来源快照及旧声明；同值刷新仅补来源证据，不重写作者关系。未选的内部候选标为 `quality_rejected`，该标记表示自动未采纳，并不保证候选事实错误。自动审计记录处理前后值、原声明、逐候选理由和 PDF 校验值，不填写人工裁决时间。相同预览重试不重复应用，实际字段变更沿用事务索引同步队列。

```bash
uv run python scripts/reconcile_pdf_quality_history.py \
  --report logs/eval/pdf-history-preview.json
uv run python scripts/reconcile_pdf_quality_history.py \
  --apply --plan logs/eval/pdf-history-preview.json --report logs/eval/pdf-history-result.json
```

预览、缓存及应用报告仅保存在忽略的运行目录。缺失文件、提取失败或失效的预览会明确报告；未成功处理不能作为已解决统计。

### PDF 历史冲突质量处理

`scripts/reconcile_pdf_metadata.py` 接受已经对照 PDF 检查过的 JSON 决策数组，默认只验证和预览，传入 `--apply` 才提交。它不是导入流水线的新优先级策略。

每项决策包含 `paper_id`、`field`、`current_id`、`provenance_id`、`kept`、`rejected`、`value`、`reason`；作者处理还必须包含 `evidence`。工具仅接受当前来源为 `pdf_heuristic`、历史来源为 `pdf_embedded` 的作者或摘要差异，拒绝已人工裁决或预览后发生变更的条目。作者字典按归一化姓名复用，展示名可能与历史声明的大小写、标点不同；此时必须在 `materialized_kept` 明确提供已复核的实际展示值。相同字段的相同历史候选可用 `duplicate_provenance_ids` 一并处理，不能夹带不同值或已人工裁决的候选。批次在同一数据库事务内执行，任一验证失败即不提交。

处理保留原始声明及来源，在已匹配的 `paper_sources.raw` 内持久化处理前后值、实际展示前后值、原始声明 ID、原始来源 ID、原决策标记、理由、PDF 证据、规则版本、执行工具和时间。被排除的历史声明记为 `quality_rejected`，不再作为待复核冲突；机器处理不写代表人工点击的 `decision_at`。值发生变化时通过字段服务追加新声明并同步论文表或作者关系，原声明仍可通过回滚接口恢复。

示例：

```bash
uv run python scripts/reconcile_pdf_metadata.py \
  --plan logs/eval/pdf-review-plan.json --report logs/eval/pdf-review-preview.json
uv run python scripts/reconcile_pdf_metadata.py \
  --plan logs/eval/pdf-review-plan.json --report logs/eval/pdf-review-result.json --apply
```

数据库提交后，工具只更新受影响论文的 OpenSearch `authors` 关键词，不重算向量、不重写分块正文。数据库与索引并非同一事务：报告先落盘再刷新索引，完全相同的决策重试不重复改库，并可重试索引同步。该工具不改变持续导入时的候选识别、来源优先级或冲突规则。

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
- **做法**：所有标识符进 `paper_identifiers`，`scheme` 是**开放枚举**（加平台 = 加取值，不加列，`metadata_identifiers.py`）；部分唯一索引保证**一个标识符只属一篇论文**；主标识符（只有 `doi` / `arxiv`）决定 `papers.fingerprint`，兜底链是 `DOI > arXiv > 标题+首作者+年 > sha256`。
- **代价**：`papers.doi` / `papers.arxiv_id` 变成**镜像列**（真值在标识符表），必须靠 `mirror_legacy_columns`保持同步。镜像同步在**应用层**（非 DB 触发器），所以理论上有漂移可能；用只读审计 `GET /api/consistency/doi`（或 `scripts/audit_identifier_mirrors.py`）做看门狗——它只报告不修复，覆盖 6 类漂移（mirror 无 identifier、identifier 无 mirror、值不等、同篇重复 identifier、指向已删论文的孤儿 identifier、缺 normalized_value）。

### 2.4 venue 拆成两级

- **要解决的问题**：会议论文的 venue 粒度是"会议 + 年份"，而且要能"只按会议搜"和"按会议+年份搜"两种粒度。
- **做法**：`venues` 存实体（`name` / `normalized_name` / `kind` / `publisher` / `issn`），`venue_editions` 存某一年那一届（`year` / `location` / `dates` / `publication_number` / `is_number`）；`papers` 同时挂 `venue_id`、`venue_edition_id` 和冗余列 `venue_year`。**绝不用"会议+年份"拼接串**。
- **代价**：写入时要两级都解析（`resolve_venue` / `get_or_create_edition`），venue 别名消歧目前只做到 `normalize_text` 级（`venue_service.py`）。

### 2.5 作者只存名字

- **做法**：`authors` 是归一化字典表；`paper_authors.display_name` 保存该论文的实际姓名拼写，`author_order` 保序且唯一。同一名单允许同名作者重复出现；旧行没有 `display_name` 时回退到字典姓名。`orcid` / `affiliation` 列存在但元数据层不写。
- **边界**：不建 `author_identifiers`，IEEE 记录里的作者 id 直接丢弃——记下所有作者名字就够用。

### 2.6 标签带 kind

- **做法**：`paper_tags` 是标签字典，`papers_tags.kind` 区分来源：`ieee_terms` / `author_terms` / `dynamic_index_terms` / `source_tag`（默认 `source_tag`）。同一篇论文的 IEEE 索引词与来源标签互不混淆，检索过滤可按 kind 或全量。

---

## 3. 表里有什么

### 3.1 `paper_sources`（`models.py`）

| 列 | 类型 / 约束 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `paper_id` | UUID，FK→`papers.id`，**可空** | 空 = 还没归属到任何论文（复核清单的数据源） |
| `source_type` | VARCHAR(32) NOT NULL | `ieee_api` / `arxiv_api` / `crossref` / `pdf_embedded` / `pdf_heuristic` / `import_file` / `manual`（`metadata_sources.py`） |
| `source_ref` | VARCHAR(512) NOT NULL | 来源内的稳定引用：`doi:…` / `arxiv:…` / `ieee:<article_number>` / 路径+sha256 / `heuristic:<sha>` 等（`metadata_sources.py`） |
| `content_type` | VARCHAR(32) | `ieee_raw` / `csl_json` / `generic_json` 等 |
| `raw` | JSONB NOT NULL，默认 `{}` | **来源原样**（IEEE 一条约 8KB），只在 PG |
| `match_status` | VARCHAR(16) NOT NULL，默认 `pending` | `matched` / `pending` / `ambiguous` / `rejected`；复核队列取 `pending` + `ambiguous` |
| `match_method` | VARCHAR(32) | 怎么匹配上的，见 [#4.4](#44-证据匹配器match_record) |
| `match_confidence` | FLOAT | 0.5 / 0.8 / 0.9 / 0.95 / 1.0 |
| `fetched_at` | DATETIME | 来源侧取数时间 |
| `imported_at` | DATETIME NOT NULL，默认 `now()` | |
| `importer` | VARCHAR(128) | API / CLI / 脚本名 |

约束与索引：`UNIQUE(source_type, source_ref)`（`uq_paper_sources_type_ref`，`models.py`）＝**幂等导入**；`ix_paper_sources_paper_id`、`ix_paper_sources_match_status`。

### 3.2 `paper_identifiers`（`models.py`）

| 列 | 类型 / 约束 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `paper_id` | UUID NOT NULL，FK→`papers.id` | 非空：一条标识符必须属于某篇论文 |
| `scheme` | VARCHAR(32) NOT NULL | 论文级开放枚举：`doi` / `arxiv` / `ieee_article_number` / `isbn` / `pmid` / `openalex` / `semantic_scholar` / `url` / `sha256`（`metadata_identifiers.py`）；ISSN 属于 venue，不得写入本表 |
| `value` | TEXT NOT NULL | 原样值 |
| `normalized_value` | TEXT NOT NULL | 规范化值：DOI 去前缀小写、arXiv 去版本后缀、sha256 校验 64 hex |
| `first_source_id` | UUID，FK→`paper_sources.id`（ON DELETE SET NULL） | 谁先带来的；合并写 identifier 时透传 `source_id`，旧的无来源行在首次有来源的重述时补齐 |
| `is_primary` | BOOLEAN NOT NULL，默认 `false` | 只有 `doi` / `arxiv` 可为真（`PRIMARY_SCHEMES`）；决定 `papers.fingerprint` |
| `created_at` | DATETIME NOT NULL，默认 `now()` | |

约束与索引：`UNIQUE(paper_id, scheme, normalized_value)` ＝同一篇论文内不重复；**部分唯一** `UNIQUE(scheme, normalized_value) WHERE paper_id IS NOT NULL`（`uq_paper_identifiers_scheme_value`）＝一个标识符只属一篇论文；`ix_paper_identifiers_paper_id`。

语义要点：删除论文时 `soft_delete_paper`（`paper_service.py`）会**删掉标识符行**以释放 DOI；`upsert_identifier`遇到"属主已软删"的行会把它改指到新论文。

### 3.3 `paper_field_provenance`（`models.py`）

| 列 | 类型 / 约束 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `paper_id` | UUID NOT NULL，FK→`papers.id` | |
| `source_id` | UUID，FK→`paper_sources.id`（SET NULL） | NULL = 系统或人工写入，无来源行 |
| `field` | VARCHAR(64) NOT NULL | 开放集合，见下 |
| `value` | JSONB NOT NULL | 标量或结构化值（venue 是对象、authors 是数组） |
| `confidence` | FLOAT | 结构化来源 1.0、首页启发式 0.5 |
| `is_current` | BOOLEAN NOT NULL，默认 `false` | 每个字段只有一条为真 |
| `decided_by` | VARCHAR(32) NOT NULL，默认 `initial` | `initial` / `structured_override` / `manual`；人工保留现值使用 `dismissed`，PDF 质量处理排除的历史候选使用 `quality_rejected`（`provenance_service.py`） |
| `decided_at` | DATETIME NOT NULL，默认 `now()` | **这条声明写入账本**的时刻（机器语义）；名字是历史遗留，别当裁决时刻用 |
| `decision_at` | DATETIME NULL | 人类**裁决**这一行的时刻（`dismiss` / `rollback` / 手工 PATCH）；`NULL` = 没有人类碰过（迁移 `a41f7c2d9b30`） |
| `identifier_id` | UUID，FK→`paper_identifiers.id` | `identifier:<scheme>` 类声明指向具体标识符行 |

`field` 的取值空间（`provenance_service.py`）：

- 10 个标量：`title` / `abstract` / `language` / `year` / `volume` / `issue` / `pages` / `publication_date` / `paper_type` / `url`（`SIMPLE_FIELDS`，每个正好对应 `papers` 一列）
- `venue`、`authors`（结构化值，不走标量列映射）
- `identifier:<scheme>`，如 `identifier:doi`
- `tag:<kind>`，如 `tag:ieee_terms`

约束与索引：**部分唯一** `UNIQUE(paper_id, field) WHERE is_current`（`uq_paper_field_provenance_current`）；`ix_paper_field_provenance_paper_field`。历史行永不删除——回滚 = 把历史某行置回 `is_current=true` 并写回 `papers` 列（`rollback_field`，`provenance_service.py`）。

### 3.4 `venues`（`models.py`）与 `venue_editions`

| 表 | 列 | 约束 |
|---|---|---|
| `venues` | `id`、`name`(512)、`normalized_name`(512)、`kind`(32，可空：journal / conference)、`publisher`(255)、`issn`(64)、`created_at`、`updated_at` | `UNIQUE(normalized_name)` |
| `venue_editions` | `id`、`venue_id`(FK NOT NULL)、`year`(INT NOT NULL)、`location`(TEXT)、`dates`(TEXT)、`publication_number`(64)、`is_number`(64) | `UNIQUE(venue_id, year)`（`uq_venue_editions_venue_year`） |

`papers` 侧对应三列：`venue_id`、`venue_edition_id`、`venue_year`（冗余，带 `ix_papers_venue_year`，`models.py`）——搜"会议"命中 `venues.name`，搜"会议+年份"命中 `venue_id + venue_year`，两种粒度都不需要 join 届次表。

写入方 `venue_service`：`get_or_create_edition` / `resolve_venue` / `attach_venue`，语义是**只填空**。

### 3.5 `papers` 的元数据相关列（`models.py`）

| 组 | 列 |
|---|---|
| 标识 | `id`(UUID PK)、`fingerprint`(255 NOT NULL)、`external_id`(255，**当前全仓无引用**) |
| 书目 | `title`(TEXT NOT NULL)、`abstract`、`language`(32)、`year`(INT)、`volume`(32)、`issue`(32)、`pages`(64，形如 `631-635`)、`publication_date`(DATE)、`paper_type`(32：journal / conference / preprint / early_access / standard) |
| venue | `venue_id`、`venue_edition_id`、`venue_year` |
| 镜像列 | `doi`(255)、`arxiv_id`(64)——真值在 `paper_identifiers`，由 `mirror_legacy_columns` 同步 |
| 状态 | `status`(32 NOT NULL，默认 `'pending'`；含 `AWAITING_FILE` = 只有元数据、还没 PDF 的壳)、`deleted_at`（软删） |
| 索引与模型 | `embedding_model`(128)、`embedding_dimension`(INT)、`created_at`、`updated_at` |

索引：部分唯一 `uq_papers_fingerprint_live (fingerprint) WHERE deleted_at IS NULL`；`ix_papers_doi` / `ix_papers_arxiv_id` / `ix_papers_year` / `ix_papers_status` / `ix_papers_venue_year` / `ix_papers_created_at`。

### 3.6 `paper_files`（`models.py`）

| 列 | 类型 / 约束 | 说明 |
|---|---|---|
| `id`、`paper_id`、`created_at`、`updated_at` | | |
| `source_id` | UUID，FK→`paper_sources.id` | **这个 PDF 由哪份来源带来** |
| `kind` | VARCHAR(32) NOT NULL，默认 `original` | `published_pdf` / `original` / `arxiv_pdf` / `supplement` |
| `object_key` | VARCHAR(1024) NOT NULL | MinIO 键，如 `papers/<paper_id>/original.pdf` |
| `bucket`、`filename`、`content_type`、`size_bytes`、`sha256`(64) | | `ix_paper_files_sha256` 支撑按文件判重 |
| `is_primary` | BOOLEAN NOT NULL，默认 `false` | 每篇论文只有一个主版本 |
| `deleted_at` | DATETIME | 软删 |

约束与索引：**部分唯一** `UNIQUE(paper_id) WHERE is_primary AND deleted_at IS NULL`（`uq_paper_files_primary`）；`ix_paper_files_paper_id`。

主版本优先级 `published_pdf(3) > original(2) > arxiv_pdf(1) > supplement(0)`（`paper_service.py`）；`select_primary_file`() 取优先级最高、同级取先到；`original_file(paper)`返回的**是主版本文件**，`GET /api/papers/{id}/file`、reindex、删除都走它。**只有主版本被解析/切块/索引**，非主版本照样登记但不解析。

### 3.7 作者与标签（`models.py` 的 `Paper`、`223-315`）

| 表 | 列 | 约束 |
|---|---|---|
| `authors` | `id`、`name`(512)、`normalized_name`(512)、`orcid`(64)、`affiliation`(TEXT)、时间戳 | `ix_authors_name` + **`uq_authors_normalized_name`（UNIQUE，迁移 `0de3ab5e24dc`）** |
| `paper_authors` | `paper_id`、`author_id`、`display_name`(TEXT，可空)、`author_order`(INT NOT NULL，默认 0)、`is_corresponding`(BOOL) | `UNIQUE(paper_id, author_order)`；同名作者允许多次出现 |
| `paper_tags` | `id`、`name`(128)、`normalized_name`(128)、时间戳 | `UNIQUE(normalized_name)` |
| `papers_tags` | `paper_id`、`tag_id`、`kind`(32 NOT NULL，默认 `source_tag`)、`created_at` | `UNIQUE(paper_id, tag_id)`、`ix_papers_tags_tag_id` |

`kind ∈ {ieee_terms, author_terms, dynamic_index_terms, source_tag}`（`metadata_tags.py` 的 `KIND_IEEE_TERMS`）；落库入口 `link_tags` / `replace_kind` / `tags_by_kind`。

作者唯一性：`get_or_create_author`按 `normalized_name` 查行，查找**容错**（最老行优先 + 多条时 WARNING）；唯一性由 `uq_authors_normalized_name`（迁移 `0de3ab5e24dc`，先把重复行的关联重指到最老行再删重复行）保证，未跑迁移的库照常工作。

### 3.8 四个部分唯一索引 = 模型不变量

| 不变量 | 索引 | 定义处 | 违反时会怎样 |
|---|---|---|---|
| 一个标识符只属一篇论文 | `uq_paper_identifiers_scheme_value` | `models.py` | 同一 DOI 注册不上第二篇；删除论文时**必须**释放标识符行 |
| 每个字段只有一个当前值 | `uq_paper_field_provenance_current` | `models.py` | 翻转当前值必须在同一事务里先降旧行 |
| 每篇论文只有一个主版本文件 | `uq_paper_files_primary` | `models.py` | 主版本翻牌必须在同一事务内改完所有行 |
| 指纹只被存活论文占用 | `uq_papers_fingerprint_live` | `models.py` | 软删行保留原指纹供追溯，删除即释放 |

---

## 4. 实现架构

### 4.1 模块分工（`app/services/`，4727 行）

| 模块 | 行数 | 职责 | 主要入口 |
|---|---|---|---|
| `provenance_service.py` | 626 | 字段级账本 + **唯一写入口**（claim → `papers` 列） | `record_claim`、`promote`、`set_field`、`rollback_field`、`write_field`、`recorded_conflicts` |
| `metadata_import.py` | 901 | 三格式外部记录导入 | `detect_format`、`parse_records`、`import_records`、`_import_one`、`import_payload`、`import_file` |
| `metadata_service.py` | 457 | PDF 首页启发式抽取（标题/作者/年份/venue/DOI） | 供 `tasks._backfill_metadata` 调用 |
| `metadata_identifiers.py` | 450 | 标识符规范化、主标识符、指纹 | `normalize_identifier`、`primary_identifier`、`upsert_identifier`、`replace_identifier`、`refresh_primary`、`upgrade_fingerprint`、`mirror_legacy_columns` |
| `metadata_merge.py` | 427 | R2 合并裁决与落库 | `decide`、`apply_decision`、`merge_values`、`conflict_report` |
| `metadata_matcher.py` | 422 | 五步匹配（外部记录 ↔ 已入库论文） | `match_record` |
| `venue_service.py` | 405 | venue / edition 解析与写入（只填空） | `get_or_create_edition`、`resolve_venue`、`attach_venue` |
| `metadata_manual.py` | 328 | 人工编辑、回滚、`GET metadata` 视图 | `patch_metadata`、`rollback_metadata`、`metadata_view` |
| `metadata_sources.py` | 308 | 来源行幂等写入、人工归属、复核查询 | `upsert_source`、`attach_source`、`review_queue` |
| `metadata_shell.py` | 233 | 壳论文（`AWAITING_FILE`）建立与采纳 | `create_shell`、`adopt_paper`、`attach_source_to_shell` |
| `metadata_tags.py` | 170 | 标签字典与 kind 分类落库 | `link_tags`、`replace_kind`、`tags_by_kind` |

### 4.2 写入路径：`write_field` 是唯一的「字段名 → papers 列」映射

`write_field`（`provenance_service.py`）按字段分派，映射表 `SIMPLE_FIELDS`是 10 个标量 → `papers` 列的一一对应；其余走结构化写入：

| 字段 | 落点 |
|---|---|
| `SIMPLE_FIELDS` 的 10 个标量 | 同名 `papers` 列（`title` / `abstract` / `language` / `year` / `volume` / `issue` / `pages` / `publication_date` / `paper_type` / `url`） |
| `venue` | `_write_venue` → `venues` + `venue_editions` + `papers.venue_id/venue_edition_id/venue_year` |
| `authors` | `_write_authors` → `authors` + `paper_authors` |
| `identifier:<scheme>` | `_write_identifier` → `paper_identifiers` + 镜像列 + 指纹 |
| `tag:<kind>` | `_write_tags` → `paper_tags` + `papers_tags` |

一次写入的顺序：`record_claim`（记声明，append-only）→ `promote`（把这条翻成 `is_current=true`，同时降旧行）→ 写 `papers` 列。`set_field` 是带 `override` 语义的封装，`manual` 用它强制覆盖。

### 4.3 合并规则 R2（现状）

自动导入分成两个连续关口：`metadata_matcher.match_record` 必须先返回唯一、无强冲突的 `matched` 论文，`metadata_import._import_one` 才调用 `merge_values`。`ambiguous` 来源只进入复核队列，不能修改论文；未匹配记录建立 `AWAITING_FILE` 壳论文，不会覆盖已有论文。

通用字段的来源权威级别由 `metadata_merge.SOURCE_AUTHORITY` 定义：

| 级别 | 来源 | 行为 |
|---|---|---|
| 3 | `manual` | 人工值可强制覆盖，并保留旧声明供回滚 |
| 2 | `ieee_api` / `arxiv_api` / `crossref` / `import_file` | 已精确匹配的外部书目元数据，可覆盖 PDF 自带值和弱抽取值 |
| 1 | `pdf_embedded` | 通用字段可覆盖弱来源；作者与摘要先走 PDF 质量策略；不能覆盖外部书目元数据 |
| 0 | `pdf_heuristic` / `filename` | 通用字段只填空；正文作者与摘要可凭可靠证据修正 PDF 值；文件名只提供未占用的标识符 |

`decide` 先判断等值和人工保护，再对 PDF 作者/摘要执行 `pdf_choice`，其余有效空字段可 `filled`；人工来源可 `overridden`；标题走独立可信度阶梯；其余字段比较 `SOURCE_AUTHORITY`。来源级别更低则 `archived`，外部同级实质分歧登记待复核声明。内部来源证据不足时保留现值并归档候选。标题把 `pdf_embedded` 与 `pdf_heuristic` 视为同级，避免运行页眉覆盖正确标题。

不按摘要长度或作者数量选择。除 PDF 证据规则外，`_special_winner` 仅接受显式省略号截断且新摘要保留原文前缀的补全；`year` 同级冲突保留现值。`tag:<kind>` 覆盖时调用 `metadata_tags.replace_kind`，不同 kind 互不影响。

人工裁决（`_dismiss`/回滚）不删除声明。落库与报告链为 `apply_decision` → `merge_values` → `conflict_report`。`provenance_service.conflict_page` 使用同一质量与格式策略，再按论文和字段去重、计数、分页；已拒绝、人工处理、低权威历史声明以及内部提取差异不反复告警。当前无作者时，不确定的正文名单仍只保留为历史。

复核查询先只连接、排序候选声明及来源标识，再按批次读取 PDF 候选的 `raw.quality` 子对象，同一来源在请求内复用。完整来源快照不参与广泛连接和排序；质量判断、分页与总数仍使用相同候选集合。

格式比较只用于判断，不改写当前字段、来源原文或历史账本。归一化包括 Unicode 兼容字符、空白、HTML 实体、常见上下标/斜体标记及排版引号、连接号；标题和姓名比较忽略大小写，关键词按集合比较。作者顺序、数量、重音、缩写和实质标点差异保留，不把 PDF 创建时间等同于发表日期。

来源未知、外部同级实质分歧、外部历史声明优先级高于当前值时继续复核。内部历史差异按当前共享策略自动归为历史，不表示现值已经核实正确。历史页及回滚仍能访问所有声明。

### 4.4 证据匹配器（`match_record`）

| 步 | 依据 | 置信度 | 结果 |
|---|---|---|---|
| 1 | `paper_identifiers`，按 `doi > arxiv > ieee_article_number` 顺序探测（`_MATCH_SCHEMES`）；ISSN 是刊物标识，不参与论文身份匹配 | 外部/人工来源 1.0；PDF/文件名/无来源 0.95 | 外部/人工命中可自动匹配；其余必须有兼容标题或主文件名标识符支持 |
| 2 | `paper_files.sha256` | 1.0 | `matched`，`sha256` |
| 3 | 严格完整 DOI 文件名命中，且候选唯一 | 0.95 | `matched`，`filename_doi`；PDF 标题可能损坏，不作为必要条件；并列或结构化 DOI 冲突则 `ambiguous` |
| 4 | 严格 DOI 文件名中出现输入 IEEE article number + 规范化标题一致，且候选唯一 | 0.9 | `matched`，`filename_ieee_article_number_title`；并列则 `ambiguous` |
| 5 | 规范化标题 + 首作者 + 年份三者全中，且最高证据候选唯一 | 0.8 | `matched`，`title_year_author`；并列则 `ambiguous`，不按创建时间选第一个 |
| 6 | 仅标题命中（候选 = 规范化标题的**每个词**都 AND 命中，再 Python 精确等值；不设 200 上限/最长词截断） | 0.5 | **`ambiguous`**，不自动挂 |
| 7 | 文件名归一化相等 | 0.5 | `ambiguous` |
| — | 都没有 | — | `unmatched` → 导入侧建壳 |

强冲突优先于正向证据：主文件名携带不同 DOI、候选已有不同的外部/人工论文级 identifier、输入的多个 identifier 分别属于不同论文，或 DOI 命中但标题明显无关且没有 SHA256/文件名支持时，结果为 `ambiguous`。候选明细包含标题、identifier、文件名支持和冲突原因。

身份判断读取标识符当前声明的来源和人工裁决标记，并保留首次来源中更高的权威级别。PDF 内嵌 DOI 属于提取证据，不能仅凭其不同就否决已经通过唯一标题、首作者、年份匹配的外部记录；外部确认和人工决定仍具有否决权。标题必须规范化精确相等、年份必须一致；首作者允许同词数、同姓氏的对应首字母/全名及重音写法差异。不同完整名字、缺少中间名或并列候选仍为 `ambiguous`，该支持信号不合并作者实体。

正文 DOI 提取仅扫描前两页，排除 References / Bibliography / 参考文献段落和编号参考条目，优先明确的 DOI 声明；参考文献边界一旦出现，后续页面也不再提供 DOI 候选。同一页同等级多个不同 DOI 时留空，不任取第一个。此规则减少误认引用 DOI，不能替代论文归属检查。

文件名 DOI 只接受完整 `10.<4-9位注册号>_<后缀>.pdf`，目录名和描述性文件名中的 DOI token 不参与。下载器会把 DOI 中每个 `/` 编码成 `_`：已有 DOI 与文件名比较时将全部 `/` 编码后精确比较；反向提取只有在文件名恰好一个 `_` 时才安全，多下划线文件名不猜测也不持久化 DOI。每步都跳过 `exclude_paper_id`（摄取时传本次新建的论文，绝不匹配自己）。`pending` 不是匹配器的输出，而是 `paper_sources.match_status` 的列默认值。

历史壳清理由 `scripts/reconcile_metadata_shells.py` 执行，默认 dry-run。唯一匹配的来源重放到已有 PDF 论文；其余没有文件、chunk、ingestion job 的空壳硬删除。任何带上述依赖的异常 `AWAITING_FILE` 行都会跳过。

摄取与重新提取会把严格文件名中的 DOI/arXiv 登记为 `source_type='filename'` 的弱声明：只在论文尚无该 scheme 且值未被其他论文占用时填空，不能覆盖文档内嵌、结构化导入或人工值；IEEE article number 不从 DOI 尾部推导。

### 4.5 摄取链：PDF → 归属哪篇论文 → 主版本 → 元数据回填

1. `_run_pipeline`（`tasks.py`）：进 `PROCESSING` → `STORED` 注册文件后，调 `_resolve_target_paper`。
2. `_match_existing_paper`：读取 PDF 内嵌元数据（Info / XMP）和首页启发式，用正文补足内嵌缺少的匹配信号；先尝试内嵌候选，无强冲突时再尝试正文候选，不绕过强身份冲突。命中已有论文（含壳）→ `metadata_shell.adopt_paper`，把 `reused_paper_id` / `match_method` 写进 `job.payload`。
3. `apply_primary_selection`（`paper_service.py`）判主版本；非主版本立刻 `_finish_non_primary`（`tasks.py`）结束。
4. 主版本继续：`_reset_placeholder_title`→ `_backfill_metadata`→ `_restore_placeholder_title`→ 指纹升级（`sha256` 撞车则丢弃本次论文）→ 切块 → 嵌入 → `delete_by_paper_id` + `bulk_index_chunks` → `INDEXED`。
5. `_backfill_metadata` 按层写声明：第 1 层 `pdf_embedded`（结构化，confidence 1.0）→ 第 2 层 `pdf_heuristic`（confidence 0.5），每层各自 `upsert_source` 后 `merge_values`。

`pdf_embedded` 的出版日期只来自明确的 XMP/PRISM `publicationDate`。PDF Info `/CreationDate` 表示文件制作时间，只进入来源的原始快照，不生成 `publication_date` 或 `year` 声明。

主版本四场景：

| 场景 | 行为 |
|---|---|
| 首个版本 | `primary`，正常解析索引 |
| 更高优先级后到 | `promoted`：同一次流水线继续 `PARSING→INDEXING`，用 `delete_by_paper_id` 换掉旧文档 |
| 更低优先级后到 | `non_primary`：只登记文件；作业 `COMPLETED` + `payload["indexed"]=false`、`reason="non_primary_version"` |
| 主版本被移除 | `remove_file`（`paper_service.py`）按优先级重选；无剩余文件 → `FAILED`，索引留待人工 |

### 4.6 壳论文（`AWAITING_FILE`）：元数据先、PDF 后

- 建壳：`create_shell`——`status=AWAITING_FILE`，指纹取主标识符，`title` 先留空，合并填值后再补占位标题；**无 `paper_files`、无 chunk、不索引**。
- 采纳：PDF 到达且匹配到壳 → `adopt_paper`：改 `paper_files.paper_id` 与 MinIO 键、`is_primary=false`、补 `source_id`；**先把 `ingestion_jobs` 改指到壳论文再删临时论文行**（否则 `cascade="all, delete-orphan"` 会把作业一起删掉），最后把壳置 `PENDING` 走正常流水线——**复用同一个 `paper_id`**。
- 长期无 PDF：可按 `status=AWAITING_FILE` 查（`GET /api/papers?status=AWAITING_FILE`），**不自动删除**。

### 4.7 HTTP 端点（9 个 + 1 个配套）

| 端点 | 作用 | 出处 |
|---|---|---|
| `POST /api/metadata/import` | 导入外部记录，**默认 dry_run**；试运行先批量预取已导入来源并直接报告 `unchanged`，其余记录在 SAVEPOINT 内按实际顺序执行后回滚，因此批内匹配统计与 apply 一致 | `api/metadata.py` |
| `GET /api/metadata/review` | 复核清单（`pending` / `ambiguous` 来源 + 真冲突） | |
| `POST /api/metadata/sources/{source_id}/attach` | 人工把来源挂到某篇论文并重放合并 | |
| `POST /api/metadata/apply` | 批量应用人工决定（`mode=overwrite` 走 PATCH 语义） | |

导入报告的每个 `sources[]` 条目除 `match_method` / `match_confidence` 外，还返回 `reason`、`evidence[]`、`match_conflicts[]` 和 `candidates[]`；dry-run 与 apply 使用同一解释结构，便于批量复核自动匹配与否决原因。
| `POST /api/metadata/reextract` | **全库**对已存 PDF 重跑元数据质量合并，**默认 dry_run**，admin | |
| `POST /api/papers/{id}/metadata/reextract` | **单篇**重跑元数据质量合并，**默认 dry_run**，write | `api/papers.py` |
| `GET /api/papers/{id}/metadata` | 当前值、字段账本、最近 100 条 `quality_decisions`、`index_sync` 状态 | `api/papers.py` 的 `get_paper_metadata()` |
| `PATCH /api/papers/{id}/metadata` | 人工编辑（未知键进 `rejected`；改 `doi`/`arxiv_id` 会升级指纹） | |
| `POST /api/papers/{id}/metadata/rollback` | 把某字段回滚到历史声明 | `patch_paper_metadata()` |
| `POST /api/papers/{id}/reindex` | 让元数据改动进入检索过滤（配套，不是元数据端点） | |

单篇与全库 reextract 共用三层发现与质量合并；dry-run 在 SAVEPOINT 内执行相同规则后回滚，不写来源、审计或同步任务。两者都**不重解析正文、不重切块、不重嵌入**。`refresh_index=true` 提交后立即尝试快照同步，失败由后台重试；false 仅省略立即刷新，不关闭可靠的后台同步。响应增加 `quality_counts`、`index_pending`；`index_documents` 表示此次立即更新的文档数。

契约：请求体支持 multipart 文件、`application/json`，以及 JSONL 的 `application/x-ndjson`、
`application/ndjson`、`application/jsonl`，其它类型 → 415（`_payload_from_request`）；
`source_type` 非法 → 422；同一记录重复导入计入 `unchanged`，不产生第二行来源。

#### JSONL 输入

`metadata_import.load_payload` 统一解码 JSON/JSONL；`.jsonl` / `.ndjson` 后缀或 JSONL 媒体类型
明确指定逐行读取，未指定时也能识别多条连续的 JSON 对象。每个非空行必须是一个对象，支持 UTF-8 BOM、
CRLF、空行与无换行的末行。解码结果交给原有 `parse_records_tolerant`、匹配和导入服务，
IEEE 嵌套作者、来源原文、dry-run、去重及 limit 行为与 JSON 一致。
JSONL 语法错误或非对象行在开始导入前被拒绝，API 返回 422 并标明物理行号；
可解码记录的字段错误仍进入 `failures`，不影响其它记录按现有规则处理。

IEEE Xplore 的 `issue` 偶尔采用“期号: 专题名”（如 `1: Single-Photon Technologies and Applications`）。`parse_ieee_record` 只把冒号前的数字期号写入 `papers.issue`，完整标签仍原样保存在 `paper_sources.raw`；该规则不对 CSL-JSON 或通用 JSON 做任意截断。

### 4.8 脚本

| 脚本 | 作用 |
|---|---|
| `scripts/backfill_metadata.py` | 给历史论文补来源行 / 声明 / 标识符 / 主版本标记，**幂等**，`--dry-run` / `--limit`；从不改 `papers.fingerprint` |
| `scripts/reextract_metadata.py` | 对已存 PDF 重跑质量合并，`--dry-run`（默认）/`--apply`/`--paper-id`/`--limit`/`--no-index`；`--no-index` 省略立即刷新，后台仍重试 |
| `scripts/audit_pdf_metadata_quality.py` | 只读回放已显式复核的历史摘要与指定数量的真实 PDF 作者样本；数据库事务只读，错误自动覆盖或提取失败退出非零；无复核基准也不算通过 |
| `scripts/acceptance_pdf_metadata_quality.py` | 使用临时 PostgreSQL schema 和 OpenSearch 索引验证 PDF 候选、dry-run 回滚、重导入幂等、人工保护、内部差异不入复核、外部 DOI 纠正、同值外部确认、强身份冲突和快照同步；结束或异常均清理，不写既有论文数据；可用 `--base-url` 只读验证已运行 API，报告复核计数与耗时 |
| `scripts/import_metadata.py` | CLI 导入（`--apply` / `--limit` / `--source-type` / `--report`） |
| `scripts/acceptance_metadata.py` | 真机验收 7 项：动态盘点当前库并验证回填不变量、IEEE 按 DOI 命中并 apply、无 DOI 记录进复核、手动改+回滚+指纹升级、venue 检索（需 reindex）、元数据先 PDF 后、索引一致性。**只在自己的壳论文上造数据**，`--cleanup` 删掉本次造的行 |
| `scripts/audit_identifier_mirrors.py` | **只读**核对 `papers.doi`/`arxiv_id` 镜像列与 `paper_identifiers`（6 类漂移），`--json`/`--limit`/`--no-fail`；有漂移 exit 1，不修复 |
| `scripts/audit_author_venue_quality.py` | **只读**核查 authors/venues 归一化质量：拼接名、空名、占位符（`authors` 之类）、同篇重复作者、author_order 断档、venue 空名/含 `;`/同键异名；`--json`/`--no-fail`，不修复 |

---

### 4.9 元数据如何传播到检索与读接口

- **索引快照**：`app/search/snapshot.py::paper_metadata_snapshot`（`Checker.check()`）把当前值写成 chunk 文档上的可过滤字段（`venue`/`venue_year`/`paper_type`/卷期页/`publication_date`/`identifiers`/四个 tag kind），INDEXING 阶段由
 `_index_rows`（`tasks.py`）调用。`provenance_service.write_field` 为已有分块的论文在同一 PG 事务内 upsert `metadata_index_updates`。后台 `metadata_sync` 批量锁定任务，只更新标题、作者、年份、标识符和过滤快照；保留文本与向量。成功仅删除已同步版本，失败记录尝试次数和安全错误类型，并移动到队尾避免阻塞其他论文。后续修改生成新版本，防止同步完成时误删更新。无分块的壳论文无需任务。
- **读接口**：`PaperOut`直接输出当前值列 `volume/issue/pages/publication_date/
 paper_type/venue_edition_id/venue_year`；`GET /api/papers` 支持 `venue`/`year_from`/`year_to`/`paper_type`/`tag`/`author`
 过滤（**读 PG 当前值**，与检索读快照是两条路）。`author` 按 `authors.normalized_name` 匹配、多人 AND；
 有 `q` 时结果按标题词频相关度排序，无 `q` 时最新优先。
- **一致性**：`GET /api/consistency` 核对三端（元数据本身不参与，它只在 PG）；`GET /api/consistency/doi` 单独核对 `papers.doi`/`arxiv_id` 镜像列与 `paper_identifiers` 是否同步（只读看门狗，见 §2.3）。
- **测试落点**：`tests/test_index_snapshot.py`（9）、`tests/test_refresh_index_metadata.py`（13）、`tests/test_paper_list_filters.py`（17）、`tests/test_consistency.py`（22）、`tests/test_identifier_mirror_audit.py`（9）、`tests/test_author_links.py`（5）。

## 5. 配置项

来源、字段和质量规则为代码常量；同步周期和批量大小由环境变量控制：

| 键 | 作用 | 出处 |
|---|---|---|
| `METADATA_SYNC_INTERVAL_S` | 失败后的后台重试周期，默认 5 秒，大于 0 | `app/core/config.py` |
| `METADATA_SYNC_BATCH_SIZE` | 每批待同步论文数，默认 20，范围 1–200 | 同上 |
| `OPENSEARCH_URL` | 验收脚本直查文档数 | `app/core/config.py` |
| `PAPER_API_KEY` | 验收脚本调 API 的鉴权头 | |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` | 写进 chunk 文档，随过滤字段进索引 | |

CLI 参数（非配置）：`backfill_metadata.py --dry-run/--limit`、`import_metadata.py --apply/--limit/--source-type/--report`、`acceptance_metadata.py --base-url/--cleanup/--opensearch-url`。

---

## 6. 测试位置与覆盖

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_review_fixes.py` | 重复标识符、多个主文件、多条 current 声明通过实际写入验证约束 |
| `tests/test_metadata_identifiers.py` | 标识符归一化、主标识符选择、幂等写入与镜像一致性 |
| `tests/test_metadata_matcher.py` | 强身份唯一命中、弱证据与冲突身份不误合并 |
| `tests/test_metadata_merge.py` | 填空、按来源权威级别覆盖、保留同级冲突和人工决定 |
| `tests/test_metadata_api.py` | HTTP 契约：`dry_run` 默认、415、人工归属两侧不存在时 404 |
| `tests/test_metadata_jsonl.py` | 真实样例的 JSON/JSONL 预览与跨格式去重、API/CLI 导入、坏行无部分写入 |
| `tests/test_metadata_scripts.py` | 回填幂等、从不改指纹、CLI 报告（session 打桩到 SQLite） |
| `tests/test_awaiting_file.py` | 元数据先建壳 → PDF 到达复用同一 `paper_id` |
| `tests/test_manual_metadata.py` | `manual` 编辑立即生效且可回滚 |
| `tests/test_primary_version.py` | 主版本优先级；非主版本不解析；`needs_reindex` 标志 |
| `tests/test_pdf_field_quality.py` | Subject 印证、作者分隔和区块、非英文/单词姓名、文字层质量 |
| `tests/test_pdf_quality_workflows.py` | 自动拒绝/修正/复核、审计幂等、人工保护、同名作者、事务同步与失败重试 |

测试选择标准见[测试行为边界](testing-policy.md)。

真机验收：`uv run python scripts/acceptance_metadata.py --cleanup`（需 API 已启动），7 项见 [#4.8](#48-脚本)，实测数字在 `docs/progress/project.md` [#17](../progress/project.md#17-元数据架构多来源--通用格式--外部导入2026-09-21)。

---

## 7. 边界与已知行为（现状）

**做**：多来源记录、五步匹配、R2 合并、字段级账本与回滚、venue 两级、主版本选择、壳论文复用、三格式导入、人工编辑。

**不做**：

- **网络抓取**：`ieee_api` / `arxiv_api` / `crossref` 只有取值与落库路径，没有任何调用代码（`metadata_sources.py` 只是常量）。
- **站点专用 translator、RIS / BibTeX 解析**：只认 IEEE Xplore raw / CSL-JSON / 通用 JSON。
- **自动合并重复论文**：重复只被报告（`backfill_metadata.py` 的 `duplicate_identifiers`），由人决定。
- **`author_identifiers`**：作者只存名字，IEEE 的作者 id 丢弃。
- **venue 别名消歧**：只做 `normalize_text` 级归一化（`venue_service.py`）。
- **改元数据自动 reindex**：需要显式 `POST /api/papers/{id}/reindex`。
- **回填调度**：`backfill_metadata.py` 是一次性幂等脚本，没有增量调度。

**已知行为（不是缺陷，读代码时会撞上）**：

- `papers.external_id`（`models.py`）全仓无引用，是历史遗留列。
- `ImportReport.unmatched`（`metadata_import.py`）有字段无自增点，恒为 0；没有证据的记录一律计入 `created_shell`。
- `PrimaryOutcome.needs_reindex`（`paper_service.py`）只有单测引用，app 无调用方——主版本翻牌由同一次流水线继续索引完成。
- `papers.doi` / `papers.arxiv_id` 是镜像列，改它们必须同时改 `paper_identifiers`（走 `replace_identifier` + `mirror_legacy_columns`）。
