# Changelog

本文件记录 paperbox（论文知识服务 REST API）的所有重要变更。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本 SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- **MCP agent interface 的传输层与第一个工具**：paperbox 现在可以在自身进程里挂载
  `/mcp`（Streamable HTTP），把检索/阅读/维护能力直接暴露给 agent。**默认关闭**
  （`MCP_ENABLED=false`），开启时**必须**显式给出 `MCP_ALLOWED_HOSTS`（白名单为空则启动报错，
  不使用 SDK 的 localhost 默认值），白名单外的 `Host` 返回 421。已实现 `paper_job_status`
  （可带 `wait_seconds` 有界等待）；其余工具按契约文档 `docs/architecture/11-mcp-agent-interface.md`
  推进。新增配置键：`MCP_ENABLED`/`MCP_ALLOWED_HOSTS`/`MCP_WRITE_ENABLED`/`MCP_ALLOW_DELETE`/
  `MCP_ALLOW_METADATA_WRITE`/`MCP_ALLOW_REINDEX`/`MCP_MAX_CHARS`/`MCP_MAX_CHARS_CEILING`/
  `MCP_WAIT_SECONDS`/`MCP_DOWNLOAD_TTL_SECONDS`/`MCP_TOOLSET`/`PAPER_API_KEYS`。
- **MCP 读工具全部可用（6 个）**：`paper_search`（默认开精排，带 `filters`/`facets`）、`paper_get`
  （元数据 + 字段来源 + 降级记录）、`paper_get_chunks`（按阅读顺序分页，带字符预算与续读游标）、
  `paper_get_context`（取检索命中前后文，目标块标 `primary`）、`paper_get_file`、`paper_job_status`。
  每个工具都返回统一的 `Envelope{data,meta,warnings,citations}`，`citations[]` 带 `paper_id`/页码/章节/
  `chunk_id`/≤200 字原文片段 —— agent 可以直接写"该结论见第 7 页 III-B 节"。检索/读取逻辑走
  `app/services/search_pipeline.py` 与 `app/services/chunk_service.py`（REST 与 MCP 共用同一实现，
  同查询 total 与排序逐位相同）。
- **短期签名下载链接**：`paper_get_file` 返回 `GET /api/downloads/{paper_id}?exp=&sig=`（HMAC 覆盖
  `paper_id`+过期时间，默认 300 s，`MCP_DOWNLOAD_TTL_SECONDS` 可调）。链接**不含长期凭据**，
  过期或签名被改一律 403，对象存储仍经应用代理、桶保持私有。
- **用户手册（`UserManual.md`）**：完整的部署与配置说明（应用 + 四个容器，按变量逐项列出作用/默认值/可选值）、
  全部接口的说明与参数、以及排障清单（状态码、作业错误码、解析降级码与常见问题）。

### Changed

- **README 精简为概览**（简介 / 快速开始 / 架构 / 目录 / 端点一览 / 声明）：配置项、接口参数、使用示例与服务器部署
  迁入用户手册；旧版 README 归档到 `docs/old/README-20261004.md`。
- **仓库根 `.env.example` 的默认凭据与 `infra/.env.example` 对齐**（PostgreSQL 口令、MinIO 用户名/口令），
  两份模板可直接复制使用；`DOCLING_URL` 默认留空（关闭 docling 后端、立即降级为内置 pypdf），
  不再指向私网地址。

### Fixed

- **非 ASCII 文件名导致下载 500**：`Content-Disposition: attachment; filename=<中文名>.pdf` 被 Starlette
  按 latin-1 编码，抛 `UnicodeEncodeError` —— 任何文件名含中文的论文都下不下来（REST 与 MCP 共用的
  流式下载路径）。改为 RFC 6266 双段头（ASCII 回退名 + `filename*=UTF-8''<百分号编码>`）。
- **`GET /api/jobs/{job_id}` 传入非 UUID 的 id 会 500**：作业 id 是 UUID 列，非法字符串直接
  落到 PostgreSQL 触发 `DataError`。现在在服务层就判为"查不到"，REST 返回 404、MCP 返回
  `NOT_FOUND`（MCP 验收时发现，两条路径一起修好）。

## [0.2.0] - 2026-10-01

检索质量收口 + 原生融合后端定档 + 精排轻量化。本版的核心变化是**默认检索路径换了实现**，
但对外契约（请求/响应字段、`total`/`candidates`/evidence 规则）保持一致。

### Added

- **原生 hybrid 检索后端**：`mode=hybrid` 现在由 OpenSearch 侧一次请求完成两路融合并把候选折叠成**论文**
  （每篇附带若干证据块），取代此前「两条查询 + 应用内融合 + 应用内聚合」。它已是**默认**实现，
  请求体 `backend: "python"` 可逐次切回旧实现，响应回显实际使用的后端。性能：同一批查询 p50 快 17.8%。
- **过滤面（facet）**：`facets=true` 时一次检索同时返回每个过滤键的可用取值
  （`venue` / `paper_type` / `year` / 三类索引词等），按**论文数**统计，与 `GET /api/papers` 的等价过滤计数一致。
- **精排模型可替换**：任意 ONNX 交叉编码器只要换两个环境变量即可上线（不需要改代码），
  便于按机器规格在中英质量/速度之间取舍。
- **评测报告对撞工具**：把同一批查询的两个后端路径逐查询配对，做 bootstrap 95% 置信区间对比，
  并给出「同深度指标」（消除「返回列表长短」对 Recall@10/NDCG@10 的天然偏袒）与 p50/p95 延迟。
- **评测旁路（Search Relevance Workbench）**：第二套独立工具链交叉验证检索结论。
- **OpenSearch 快照运维**：一条命令幂等创建快照仓库 + 定时快照策略，支持取基线快照与**恢复演练**
  （还原到临时索引、比对文档数后自动清理），不污染线上索引。
- 检索响应新增 `candidates`（实际喂进聚合的候选块数）、`backend`（实际使用的融合后端）、
  `facets`、`rewrite`（查询改写信息）字段。

### Changed

- **`total` 的口径**：从「候选池大小」改为**论文数真值**（独立聚合统计，失败只降级不影响检索），
  新增 `candidates` 承接旧数字，避免「为什么返回的论文比候选少」这类困惑。
- **精排默认档换成 int8 量化多语言交叉编码器**：单次 50 候选 13.8s → **3.2s**，精排自身内存 1742 → **824 MiB**；
  **代价是中文**：中文定标集 HR@1 0.800 → 0.700（英文 50 条持平）。内存/CPU 宽裕的机器可一行配置换回原档。
- **索引 `dynamic` 由 `true` 收紧为 `strict`**：写入未声明字段此前会静默变成全文可搜字段，现在直接报错。
  索引物理名更新为 `paper_chunks_v3`（别名 `paper_chunks_current` 不变，调用方无感）。
- **解析侧省钱开关默认关**：docling 的公式转 LaTeX（实测 5 页 5.9s → 39.2s，正文最坏 252s）默认关闭，
  需要时按部署打开；语义切块模式同样默认关闭。
- 工作目录与数据落位从 D: 盘迁到 C:（D: 盘已弃用），OpenSearch/MinIO/模型缓存目录一并迁移并逐字节核对。

### Fixed

- 原生后端：**截断按论文计数**（此前按底层片段计数，`top_k=10` 可能只返回 3 篇，并切断某篇的证据）。
- 原生后端：**精排池按论文内的片段展开**（此前每篇只有融合赢家那一个片段进交叉编码器，
  导致「最佳片段不是融合赢家」的论文排不上去，`ndcg@1` 比旧实现低 0.10）。
- 超长章节标题不再让整篇论文导入失败（章节列放宽为长文本；超长标题降级为正文并记降级码，信息不丢）。
- 手动修正论文标识符后，论文主表里的镜像列与去重指纹现在会同步跟随（此前只写新行，可能继续指向被否定的旧值）。
- 三端一致性检查不再把「解析产物缓存」误报为孤儿对象（但在已删除论文上的同类对象仍算残留）。
- 索引名与配置默认值统一收口到 v3，修掉几处因历史重建遗留的指向漂移。
- 评测定标集按当前语料重写（60 条查询 / 142 条标注），并修正几处过期文档引用。

## [0.1.0] - 2026-09-29

首个可发布版本：论文知识服务的完整 MVP + P1 能力。

### Added

- 论文导入（URL / 单文件 / 多文件 / 服务器目录 / zip 压缩包），原件入对象存储、元数据入 PostgreSQL、
  切块带页码与章节、向量化后入 OpenSearch。
- 双解析后端：**docling**（主，含远端 docling-serve 部署）与 **pypdf**（降级），解析产物可缓存、可重放。
- 两种切块模式：定长（默认）与语义低谷切分。
- 检索：关键词（BM25，CJK bigram 分词）/ 语义（kNN）/ 混合（RRF），两阶段交叉编码器精排，
  论文级聚合与证据块回显，可选查询改写。
- 元数据三层模型（论文 / 来源记录 / 字段级账本）+ 标识符去重骨架 + 合并规则与手动修正、回滚。
- 运维：任务队列与阶段进度、失败归因、失败重试、上传准入与清理、三端一致性对账。
- 题录导入（IEEE raw / CSL-JSON / 通用 JSON）、解析后端真机验收脚本、量化评测闭环。
