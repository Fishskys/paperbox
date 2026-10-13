# PDF 解析、结构识别与切块

| 项 | 内容 |
|---|---|
| 状态 | 依据 commit `54048a3`（2026-09-22，chunking / pdf / structure 地基）与 `2c3d514`（docling 侧）的工作树核对；[#1](#1-职责边界做什么--不做什么)–[#8](#8-未做--已知缺口) 每一节都含 docling / pypdf **两侧**条目。本文只写**结构与不变量** —— 逐段实现说明、实测数字与人工判读仍在 `docs/progress/parser.md`（[#4.3](../progress/parser.md#43-t8-双后端验收2026-09-295-份输入--2-后端) 数字 / 第 5.1 节–第 5.9 节 实现与验收）与 `docs/examine/解析双后端验收-20260929.md` |
| 关键文件 | `app/parsing/pdf.py`、`app/parsing/structure.py`、`app/parsing/chunking.py`、`app/parsing/layout.py`（降级侧几何）、`app/parsing/markdown.py`（共用方言）、`app/parsing/docling_client.py`（docling 客户端）、`app/services/parser_service.py`（编排）、`app/services/degradation_service.py`（账本 sink）、`app/services/metadata_service.py`、`app/core/errors.py`、`app/workers/tasks.py`（流水线与落库索引） |
| 相关文档 | [04-embedding.md](04-embedding.md)（向量侧）、[05-search.md](05-search.md)（索引快照与过滤）、[metadata-architecture.md](metadata-architecture.md)（发现分层）、`docs/progress/parser.md`（解析线的实现记录）、`docs/examine/解析双后端验收-20260929.md`（双后端人工判读） |

## 1. 职责边界（做什么 / 不做什么）

做什么：

| 能力 | 实现位置 |
|---|---|
| 抽取文本层，逐页产出 `PageText`（1-based 页码） | `app/parsing/pdf.py` |
| 文本规范化：统一换行、丢控制字符、保留行结构 | 控制字符清单在 `app/parsing/text.py` 的 `strip_control_chars()`；`app/parsing/pdf.py` 的 `normalize_page_text()` 逐页套用 |
| 章节识别 + 段落化（硬换行拼回散文） | `app/parsing/structure.py`、`detect_sections()`、|
| 远端 docling 解析：版面顺序 + 标题层级 + 真表格 + 公式 LaTeX → markdown | `app/parsing/docling_client.py` 的 `convert_markdown()` |
| 后端选择 / 降级留痕 / 解析产物缓存 | `app/services/parser_service.py` 的 `parse_pdf()`（纯函数）、`parse_paper_file()`（带缓存） |
| 降级侧的几何重排与页眉页脚剔除（双栏：先左栏后右栏） | `app/parsing/layout.py` 的 `reorder_pages()`、`strip_running_lines()` |
| 两后端**共用**的 markdown 方言（页标记 / 标题层级 / 归一化） | `app/parsing/markdown.py` 的 `page_spans_from_markdown()`、`prepare_docling_markdown()`、`render_markdown()` |
| 降级账本 sink（`parsing`/`chunking`/… 阶段词表） | `app/services/degradation_service.py`|
| 段落感知切块 + token 估算 + 重叠窗口 | `app/parsing/chunking.py` 的 `chunk_document()` |
| 解析结果**反推**成 `PageText`+`Section`，把两后端收进同一条切块路（**第 6.1 节**） | `app/parsing/markdown.py`，`app/parsing/chunking.py` |
| 读 PDF 内嵌元数据（Info 字典 + XMP，含 PRISM） | `app/parsing/pdf.py` 的 `extract_embedded_metadata()` |
| 首页启发式元数据（标题/作者/摘要/年份/DOI/arXiv） | `app/services/metadata_service.py` |
| 失败归因到稳定 `error_code` | `app/core/errors.py` |

不做什么：

- **不做 OCR**：文本层缺失没有兜底，`NO_TEXT_LAYER_HINT` 明确写 "OCR is required (not supported yet)"（`errors.py`）。
- **不做「一个后端一套切块」**：两后端都把结果交给同一份 markdown 方言，`chunk_markdown`反推出 sections 后走同一个 `chunk_document`。换 `PARSER_BACKEND` 换的是**文本来源**（顺序 / 标题 / 表格 / 公式），不是切块策略。
- **版面能力分两侧**：**默认后端是 `docling`**（远端版面模型给阅读顺序 / 标题层级 / 真表格 / 公式），降级侧 `pypdf` 不做版面分析 —— 只用 `Page.extract_text()` 的默认顺序（`pdf.py`），分栏靠 `layout.py` 的**几何重排**补救、表格只留占位。两条路都**不做 OCR**（`DOCLING_OCR` 默认关），**公式默认也不做**（`DOCLING_FORMULA_ENRICHMENT` 默认关：它是最贵的一项，实测 5 页 5.9s→39.2s、最坏一篇 252s）。
- **不做网络元数据**：发现分层 3-6 层（外部导入、DOI 内容协商、平台 API、模糊反查）不在本模块（[metadata-architecture.md](metadata-architecture.md)）。
- **不写库、不切索引、不算向量**：本模块只返回内存对象；落 `paper_chunks`、调 embedding、bulk 到 OpenSearch 都在 `app/workers/tasks.py`。
- **不做标识符规范化**：DOI 原样返回并保留大小写（`pdf.py` 的 `extract_embedded_metadata()`；`tests/test_pdf_embedded.py` 的 `test_xmp_packet_is_read_field_by_field()` 断言保留 `10.1109/JSSC...`）。

## 2. 关键文件与函数（文件 → 函数/类 → 作用）

`app/parsing/text.py`

| 函数/类 | 作用 |
|---|---|
| `CONTROL_CHAR_TABLE` | 待删控制字符：C0 全删（除 `\t`/`\n`），另加 0x7F |
| `strip_control_chars` | 按上表剔除；pypdf 逐页文本、内嵌元数据、两后端共用的 markdown 方言都调它 —— **只此一份定义** |

`app/parsing/pdf.py`

| 函数/类 | 作用 |
|---|---|
| `PdfParseError` | 字节流读不成 PDF 时的唯一异常出口 |
| `PageText` / `is_blank` | `page`(1-based) + `text`；`is_blank` = `not text.strip()` |
| `_page_text` | 单页抽取；单页异常吞掉记 warning 返回 `""` |
| `normalize_page_text` | CRLF→LF、`strip_control_chars`、连续空行压成 `\n\n`、逐行 strip |
| `_text` | 内嵌元数据字段归一：`strip_control_chars` + strip，空串→`None`（列表递归 join） |
| `extract_pages` | `PdfReader(BytesIO)` → 逐页 `PageText`；空 payload/损坏/加密/页树异常均转 `PdfParseError` |
| `_XMP_NS` / `_XMP_ACCESSORS` / `_XMP_LOCAL_NAMES` | 只认 `dc`/`prism`/`xmp`；本地名→pypdf accessor 兜底表；手解 16 个本地名（含 `doi`、`publicationName`、`startingPage`） |
| `_DOI_IN_TEXT` / `_ARXIV_IN_TEXT` / `_ISBN_IN_TEXT` / `_ISSN_IN_TEXT` | 从元数据文本捞标识符 |
| `EmbeddedMetadata` / `as_dict` / `is_empty` | 14 个可空字段 + `raw`；`as_dict` 只留非空值 |
| `_xmp_packet` | `ElementTree.fromstring(xmp.stream.get_data())` → `simple`（普通值）/ `alt`（`rdf:Alt` 按 `xml:lang` 分桶） |
| `_alt_value` / `_simple_value` | 语言优先序 `x-default` → `en-US` → `en` → 任意非空 |
| `_split_authors` / `_split_keywords` | 作者列表保原样、字符串按 `;`/`and`/`&`/换行切（**不按逗号**）；关键词按 `,`/`;` 切 |
| `_first_match` / `_year_from_date` / `_strip_version` | 首个正则命中（有捕获组取组 1，去尾 `.,;`）；年份取 `19xx/20xx`；arXiv 去 `vN` |
| `extract_embedded_metadata` | 层 1 主入口，**从不抛异常**，失败退化为空对象 |
| `SizedLine` / `extract_sized_lines` | `extract_text(visitor_text=...)` 取每段字号，按换行 flush 成 `(text, size)`，行字号取片段最大值 |

`app/parsing/structure.py`

| 函数/常量 | 作用 |
|---|---|
| `SECTION_BODY` | 兜底章节名 `"Body"` |
| `_NUMBERED_HEADING` | `^(数字[.数字]*\|[IVXivx]+(-字母)?)[.)]?\s+标题` |
| `_ROMAN_TAIL` | **已定义但全仓无引用（死代码）** |
| `_ALL_CAPS` | `^[A-Z][A-Z0-9 \-&/,:'()]+$` |
| `KNOWN_HEADINGS` | 25 个别名 → 规范名（`abstract`、`related work`、`references`、`appendix`…） |
| `_MAX_HEADING_WORDS` / `_MAX_HEADING_CHARS` / `_MAX_PAGE_HEADING_LOOKAHEAD` | 14 词 / 110 字符 / 回看 3 行 |
| `_NUMERIC_TOKEN` / `_looks_like_table_row` | 标题候选里数字 token 占比 ≥50% 判为表格行 |
| `Section` / `label` | `title/page_start/page_end/number/paragraphs:[(page,text)]`；`label = "number title"` |
| `_match_heading` | 三条识别规则的唯一入口 |
| `_heading_is_page_header` | 同一行在后续 3 行内复现 → 判 running header 并加入 `suppressed` |
| `detect_sections` | 逐页逐行状态机，`pending` 累积，遇标题闭合上节；末尾丢掉零段落占位节 |
| `_append_paragraphs` / `_join_wrapped_lines` | 按 `\n\n` 切段；`-\n` 断词缝合，单换行转空格 |
| `PageBlock` / `page_blocks` | 把一页切成「标题 / 段落 / 表格行块」，交给 `markdown.render_markdown` 渲染；`_append_paragraphs` 的老行为不变 |
| `merge_short_sections` | 相邻两节正文均 < `target_chars`（默认 1200）**且次节标题全大写**时并入前节 |

`app/parsing/chunking.py`

| 项 | 作用 |
|---|---|
| 常量 | `CHARS_PER_TOKEN=4`、`MAX_TOKENS=450`、`DEFAULT_TARGET_TOKENS=400`、`DEFAULT_OVERLAP_TOKENS=48`、`PARAGRAPH_SEPARATOR="\n\n"`、`MIN_CHUNK_CHARS=1` |
| `CHUNK_MODE_*` / `CHUNK_MODES` | `length` / `semantic`；与 `config.CHUNK_MODES` 必须一致（有单测钉住） |
| 语义常量 | `SEMANTIC_SIMILARITY_THRESHOLD=0.80`、`SEMANTIC_DIP_WINDOW=1`、`SEMANTIC_MIN_TOKENS=200` |
| 降级词表 | `DEGRADE_STAGE="chunking"`、`DEGRADE_SEMANTIC_FALLBACK="semantic_fallback"`、**`DEGRADE_HEADING_TOO_LONG="section_title_too_long"`**、`DegradeSink = Callable[[str, str, dict], None]` |
| `MAX_SECTION_TITLE_CHARS`/ `DEGRADE_HEADING_TOO_LONG`/ `_report_long_heading` | `markdown.py` 里超过 200 字符的「标题」按正文处理（见不变量 20） |
| `estimate_tokens` | CJK 感知估算：拉丁 `len//4`，CJK 1.25 token/字（`_CJK_TOKENS_PER_CHAR`，取 1.0-1.5 带的中位）；混合文本两段相加。**不是 tiktoken** |
| `split_sentences` | 句子切分（保留终止符；`et al.`/`Fig. 5`/`J. Smith`/`Eq. 3` 不误断；换行与 CJK `。！？` 独立成句） |
| `cosine_similarity` / `similarity_dips` | 相邻句子余弦；**低于阈值且为邻域局部最小**才判为断点 |
| `Chunk` / `_Piece` / `_Pending` | 见 [#3](#3-数据结构表字段索引或内存结构)；`_Piece.break_before` 标记「此处有语义低谷」 |
| `_section_pieces` | 段落 → `_Piece`（长度模式的输入） |
| `_report_fallback` | 把一次降级交给 `on_degrade(stage, code, {section, sentences, ...})`；没有 sink 时是 no-op |
| `_semantic_pieces` | 语义模式的输入：句子嵌入 → 低谷处收束 + 段落空行保留；嵌入失败/向量数不符 → `None`（回落长度模式 + WARNING + `_report_fallback`，两处调用点） |
| `_split_oversized_piece` | 单段落超 `max_chars` 时按字符窗切，优先 `. ! ? \n`，其次空格；只给首个窗口保留 `break_before` |
| `_overlap_suffix` | 取尾部 `overlap*4` 字符，再从首个 `\n`/`. `/` ` 之后取起 |
| `_finalize` | 生成 `Chunk`；页码取 span 页面的 min/max |
| `_chunk_section` | 单节装配主循环；超 `target_chars` **或** 命中 `break_before` 且已 ≥ `semantic_min_tokens` 即 flush 并带 overlap 前缀重开；接受 `on_degrade`并透传给 `_semantic_pieces` |
| `chunk_markdown` | **流水线入口（第 6.1 节）**：`ParseBundle` → `pages_and_sections_from_markdown` → `merge_short_sections` → `chunk_document`（`embed_fn`/`on_degrade` 原样透传） |
| `chunk_document` | 入口：参数校验（含 `semantic_threshold`/`semantic_min_tokens`）→ 无有效节时造 `Body` 节 → 逐节切、全局递增 `chunk_index`；`embed_fn=None` 即长度模式；接受 `on_degrade`并透传 |

元数据与调度

| 函数 | 作用 |
|---|---|
| `extract_metadata` | 层 2 入口，返回 `{title,abstract,year,authors,arxiv_id,doi}` |
| `detect_title` | 字号优先（前 40 行中 ≥ 最大字号 `*0.92` 的行拼接）：|
| `detect_authors` / `_names_from_line` / `_plausible_author` | 标题行之后的作者块；无分隔符时按两词一组切分 |
| `detect_abstract` | `Abstract` 起，遇 `_SECTION_MARKER`/空行结束；扫描前 2 页 |
| `detect_year` / `_year_from_arxiv_id` | arXiv 优先；否则首页 2 页最高频年份（同频取大年份） |
| `detect_doi` | 首页首个 DOI 形态 token，去尾 `.;` |
| `heuristic_claim_values` / `embedded_claim_values` | 转成合并引擎吃的 `{provenance field: value}` |
| `_backfill_metadata` | 层 1 → 层 2 顺序写 claim |
| `_placeholder_title` / `_reset_placeholder_title` / `_restore_placeholder_title` | 文件名占位标题的清除与兜底 |
| `classify_failure` | 异常 → `error_code`；两个解析码（`DoclingUnavailable`→`PARSE_BACKEND_UNAVAILABLE`、`DoclingFailed`→`PARSE_FAILED`）只在「明确要求 docling 且不许降级」时出现 |

`app/parsing/docling_client.py`（581 行 —— docling-serve 客户端）

| 项 | 作用 |
|---|---|
| `CONVERT_PATH` / `VERSION_PATH` | `/v1/convert/file`、`/version` |
| `HEADING_HIERARCHY_OPTIONS` | 请求固定带 `do_pdf_heading_hierarchy=true`（v1.35.0 的 markdown `heading_hierarchy_options` 是 no-op） |
| `STATIC_FIELDS` | 其余固定选项（`to_formats=md`；OCR / 表格 / 公式开关取自配置） |
| `FORMULA_FALLBACK_REASON` | 常量 `"formulas=text"` |
| `DoclingError` / `DoclingUnavailable` / `DoclingFailed` | 三类失败；`is_client_error` 只对 HTTP 4xx 为真 |
| `DoclingResult` | 一次成功转换：`markdown/page_count/parser_version/processing_time/raw_json/formula_fallback/notes`；`degraded_reason` 由 `formula_fallback` 推出 |
| `page_count_from_markdown` | 数 `<!-- page-break -->` 得页数（与 pypdf 侧同一判据） |
| `build_request_fields` | 组装 multipart 字段（`page_range` 来自 `PARSER_MAX_PAGES`） |
| `parse_conversion_payload` | 解析响应；`status=failure` 视作文档级失败 |
| `resolve_parser_version` / `version_from_server` / `_version_fallback` | `GET /version`（毫秒级，缓存探针用）；不可用则回落包版本 |
| `is_transient_failure` | 5xx / 超时 / 传输错误 → 可重试 |
| `convert_markdown` | **唯一对外入口**：带公式一次 → 失败去公式重试一次 → 仍失败按瞬时/不可回旋分类抛错 |
| `_convert_once` | 单次 HTTP 调用（重试与超时口径都在这里） |

`app/parsing/layout.py`（824 行 —— 降级侧的几何重排）

| 项 | 作用 |
|---|---|
| `ORDER_UNVERIFIED_REASON` | 常量 `"reading order not verified"` |
| `TextFragment` | `(text, x, y, size, page)`；`right` 是估算右边界（`_ADVANCE_RATIO`） |
| `PageLayout` | 一页的坐标 + **layout 模式文本**（重排的真来源） |
| `ColumnLayout` / `LayoutReport` / `RunningLinesResult` | 单页判定 / 页级报告 / 页眉页脚剔除结果 |
| `extract_page_layouts` | **两次 pypdf 提取**：layout 模式文本 + `visitor_operand_after` 实时坐标（见 第 5.22 节） |
| `_shared_baselines` / `_detect_split` | 栏缝判定（宽度 25%–75%、两侧 ≥2 fragment、共享基线 ≥2 行） |
| `reorder_fragments` | 几何层重排 + 输出 `breaks`（段边界） |
| `_column_split` / `_split_layout_line` | 文本层按**最宽的中间空格串**切栏 |
| `reorder_page_text` | 文本层重排（段内先左后右，段间以空行分隔） |
| `same_content` | **安全网**：字符多重集比对，不等即拒绝重排 |
| `looks_multi_column` | 怀疑双栏但没修好 → 记 `reading order not verified` |
| `reorder_pages` | 逐页重排入口，返回 `LayoutReport` |
| `strip_running_lines` | 页眉页脚剔除（首/尾 2 行、≥60% 页重复、<120 字符） |
| `prepare_pages` | 重排 + 剔除的组合入口（`render_markdown` 调它） |

`app/parsing/markdown.py`（第 6.1 节 适配器，497 行 —— 两后端共用的方言 + 反向适配）

| 项 | 作用 |
|---|---|
| `PAGE_BREAK_DEFAULT` / `TABLE_FALLBACK_MARKER` | `<!-- page-break -->`、`<!-- table (structure unavailable in fallback) -->` |
| `DEGRADED_NO_FORMULA` / `DEGRADED_TABLE` / `DEGRADED_ORDER` | 三个降级词：`no formula latex` / `table structure` / `reading order not verified` |
| `PageSpan` | `page` + **半开**区间 `[char_start, char_end)`（`markdown[a:b]` 恰是一页） |
| `ParseBundle` | 解析器统一返回：`markdown/page_count/spans/backend/parser_version/degraded_reason/timings/headings/raw_json/cache_hit`（**字段冻结**） |
| `normalize_markdown` | LF 化、压空行、`html.unescape`（`&gt;` 已反转义）、`strip_control_chars`（**放最后**：`&#1;` 这类反转义后才出现的控制字符也要丢掉） |
| `heading_level` | `Section.number` / `title` → markdown 标题层级 |
| `page_spans_from_markdown` | 按页标记切 `PageSpan`，**两侧共用** |
| `clamp_heading_levels` | 超 6 级收敛（`####### REFERENCES` → 6 个 `#`） |
| `promote_paper_title` | 首个 `Abstract` / `I.` 之前的 `##` 提为 `#`（补 docling 不产 `title` 标签的短板） |
| `prepare_docling_markdown` | docling 导出的 markdown → 方言（归一化 + 层级钳制 + 标题提升） |
| `_render_block` / `render_markdown` | pypdf 侧：`PageBlock` → markdown（表格退级为占位、按 `Section` 映射层级） |
| `_HEADING_LINE` / `_NUMERIC_HEADING` / `_LETTER_HEADING` / `_COMMENT_LINE` | 识别标题行、`III.`/`A.` 编号与注释行（页标记与占位符不当标题） |
| `split_heading_number` | `III. METHOD` → `("III", "METHOD")`；无编号时返回 `(None, title)` |
| `pages_and_sections_from_markdown` | **第 6.1 节 适配器**：按页标记切 `PageText`、按 `#` 层级切 `Section`、丢注释行 → 喂 `chunk_document` |

`app/services/parser_service.py`

| 项 | 作用 |
|---|---|
| `DOCLING_FALLBACK_PREFIX` / `DOCLING_BACKEND` | `"docling unavailable"` 前缀 / 后端名 |
| `PARSE_CACHE_VERSION` / `PARSE_*_ARTIFACT` | 缓存版本 + 三个产物名（`document.md` / `document.json` / `parse-meta.json`） |
| `DEGRADE_STAGE` / `CODE_*` | 阶段名 `parsing` + 五个 code：`docling_unavailable` / `formulas_as_text` / `table_structure_lost` / `reading_order_unverified` / `pagination_truncated`，兜底 `parse_degraded` |
| `degradation_codes` | **唯一**的「原因文本 → 账本 code」映射表（新增原因必须在这里登记） |
| `_report_degradation` | 把 bundle 的降级交给 `on_degrade` |
| `ArtifactStore` | 缓存只需要的两个方法（`download_bytes` / `upload_bytes`）：生产用 MinIO、单测注内存实现 |
| `parse_pdf` | **纯函数入口**：后端选择 → docling / pypdf → 降级 → 报账（无 DB / MinIO / 任务状态） |
| `_max_pages_range` / `_partial_parse` | 截断区间、部分解析判据（既不读也不写缓存） |
| `parse_options` | **解析选项指纹**（第 5.27 节 判据 3）：`{page_break, max_pages, ocr, table_mode, formula, formula_preset}` —— 请求发什么它就记什么，写进 `parse-meta.json` 的 `options`；两个后端共用一个 dict（故意从严） |
| `_resolve_backend` | 后端名解析（显式参数 > `PARSER_BACKEND`） |
| `_parse_with_docling` / `_docling_convert` / `_parse_with_pypdf` | 两侧实现；docling 转换受 `Semaphore(PARSER_CONCURRENCY)` 保护 |
| `_merge_reasons` | 降级理由拼接（docling 原因**在前**） |
| `parse_paper_file` | 带 MinIO 产物缓存的外层入口：先查缓存，未命中才解析并写产物 |
| `_default_store` | **生产回落点**：不传 `store` 就用真 MinIO（单测必须注入，见 第 5.29 节） |
| `_artifact_key` / `_key_belongs_to` | 产物键位 + 防串到别的论文 |
| `_docling_version_probe` | 缓存命中判据第 5 条的 `GET /version` 探针 |
| `_load_cached_bundle` | **六条**判据 + 重放（`cache_hit=True`、`cache_load_s`） |
| `_store_bundle` | 写三个产物（meta 里带 `options`）；**meta 最后写**，半截写入 = 未命中 |
| `_decode_meta` / `_cached_json` / `_timings` / `_headings` / `_positive_int` / `_log_cache_problem` | meta 解析与容错（坏 meta、失败读取一律 WARNING，不抛） |

`app/services/degradation_service.py`（342 行 —— 降级账本）

| 项 | 作用 |
|---|---|
| `STAGE_PARSING` / `STAGE_CHUNKING` / `STAGE_EMBEDDING` / `STAGE_INDEXING` / `STAGES` | 阶段词表（`record()` 拒绝未知阶段） |
| `record` | 幂等 upsert：`UNIQUE(paper_id, stage, code)`，重复上报只 +`occurrences`，复发清 `resolved_at` |
| `resolve_stage` | 阶段完成时把本次**没报**的 code 盖上 `resolved_at` |
| `list_for_paper` / `open_degradations` / `paper_ids_with_open_degradations` | 查询入口（`GET /api/papers/{id}/degradations`、`reindex.py --degraded`） |
| `Recorder` | 流水线用的 sink：`__call__`（吞掉自身异常）、`seen`、`resolve` |
| `stages_for_report` | 报告聚合 |

## 3. 数据结构（表/字段/索引，或内存结构）

内存结构（均为 `slots=True` dataclass）：

| 结构 | 字段 | 备注 |
|---|---|---|
| `PageText` | `page:int`、`text:str` | `page` 1-based，直接进 chunk 的 `page_start` |
| `SizedLine` | `text:str`、`size:float` | 字号用于标题判定 |
| `EmbeddedMetadata` | `title/authors/abstract/doi/arxiv_id/venue/volume/issue/pages/publication_date/year/language/keywords/raw` | 全可空；`raw={info, xmp, issns, isbns}`（`pdf.py`） |
| `Section` | `title`、`page_start`、`page_end`、`number`、`paragraphs:[(page, text)]` | `label` 是 `number + title` |
| `Chunk` | `chunk_index`、`text`、`page_start`、`page_end`、`section`、`section_title`、`token_count`、`char_count`、`is_overlap`、`_spans` | `_spans` 仅供 `_finalize` 推页码；`is_overlap` 恒 `False` 且无消费方 |
| `TextFragment` | `text`、`x`、`y`、`size`、`page` | 降级侧几何判定的输入；`right` 是估算右边界 |
| `PageLayout` | `page`、`width`、`fragments`、`text` | `text` 是 pypdf layout 模式文本 —— 重排的真来源 |
| `ColumnLayout` | `lines`、`columns`、`split_x`、`applied`、`warning`、`breaks` | `breaks` = 调用方必须插段落边界的位置（栏边界或全宽元素） |
| `LayoutReport` | `pages`、`columns`、`reordered_pages`、`unverified_pages`、`warnings`、`failed` | `unverified_pages` 就是记 `reading order not verified` 的依据 |
| `RunningLinesResult` | `pages`、`dropped`、`page_numbers` | 页眉页脚剔除结果 |
| `PageBlock` | `kind`（`heading`/`paragraph`/`table`）、`page`、`text`、`lines`、`number`、`title` | pypdf 侧渲染 markdown 的中间结构 |
| `PageSpan` | `page`、`char_start`、`char_end` | **半开**区间；chunking 按它反查页码 |
| `ParseBundle` | `markdown`、`page_count`、`spans`、`backend`、`parser_version`、`degraded_reason`、`timings`、`headings`、`raw_json`、`cache_hit` | 解析器统一返回；**字段已冻结** |
| `DoclingResult` | `markdown`、`page_count`、`parser_version`、`processing_time`、`raw_json`、`formula_fallback`、`notes` | 客户端层结果；`degraded_reason` 由 `formula_fallback` 推出 |

落库映射（`tasks.py`；模型 `app/db/models.py` 的 `PaperChunk`）：

| `Chunk` 字段 | `paper_chunks` 列 | 约束/索引 |
|---|---|---|
| `chunk_index` | `chunk_index` | `uq_paper_chunks_paper_index`（唯一 `paper_id+chunk_index`） |
| `page_start`/`page_end` | 同名列 | `ck_paper_chunks_page_range`（`page_end >= page_start`） |
| `section` | `section` | `ix_paper_chunks_paper_section(paper_id, section)` |
| `section_title` | `subsection`（仅当 `section_title != section` 才写） | **`text`**（见不变量 20） |
| `text`/`token_count`/`char_count` | 同名列 | `text` NOT NULL |
| — | `embedding_model`/`embedding_dimension` | 取 `settings`（`tasks.py`） |

OpenSearch 文档字段（`tasks.py`）：`chunk_id/paper_id/title/authors/year/venue/doi/arxiv_id/tags/section/section_title/page_start/page_end/chunk_index/text/embedding/embedding_model/embedding_dimension/**parser_backend/parser_version**`；其中 `section_title = row.subsection or row.section`。注意过滤字段是索引时快照，改元数据后需 reindex 才生效（见 [05-search.md](05-search.md)）。

## 4. 调用链（从入口到落地，逐跳）

```
run_ingestion_job(`tasks.py` 的 `run_ingestion_job()`) / run_reindex_job(`run_reindex_job()`)
└─ _process_job() → _run_pipeline(...)()
 ├─ _advance_stage(STAGE_PARSING, 45.0) `tasks.py`
 ├─ object_storage.download_bytes(object_key) `tasks.py`
 ├─ extract_pages(data) `tasks.py` → `pdf.py` 的 `extract_pages()`
 │ └─ PdfReader → 逐页 _page_text(`pdf.py`) → normalize_page_text(`pdf.py` 的 `normalize_page_text()`)
 │ ← **只喂元数据启发式**（第 6.1 节 决策②）：文档文本的真实来源是下面的解析后端
 ├─ (可选) _resolve_target_paper(...) `tasks.py`（非主版本就地结束）
 ├─ _reset_placeholder_title(...) `tasks.py` → 
 ├─ _backfill_metadata(session, paper, pages, data) `tasks.py` → 
 │ ├─ extract_embedded_metadata(pdf_bytes) `pdf.py` 的 `extract_embedded_metadata()` → _xmp_packet(`pdf.py`)
 │ ├─ embedded_claim_values(...) `metadata_service.py` 的 `extract_metadata()` → merge_values(`tasks.py`, confidence 1.0)
 │ ├─ extract_metadata(pages, url, pdf_bytes) `metadata_service.py`
 │ │ └─ extract_sized_lines(`pdf.py` 的 `extract_sized_lines()`) → detect_title(`extract_pages()`) → detect_authors()
 │ │ → detect_abstract() → detect_year() → arxiv_id_from_url()/_from_text(`normalize_page_text()`) → detect_doi()
 │ └─ heuristic_claim_values(...) `metadata_service.py` 的 `detect_abstract()` → merge_values(`tasks.py`, confidence 0.5)
 ├─ _restore_placeholder_title(...) `tasks.py` → 
 ├─ _upgrade_fingerprint(..., discard_on_conflict=dedupe) `tasks.py` → 
 │ ← 解析**挪到这一步之后**：判成重复/非主版本的论文不再白付一次 docling（第 6.1 节）
 ├─ degradation_service.Recorder(...) `tasks.py`（降级 sink）
 ├─ _advance_stage(STAGE_CHUNKING, 60.0) `tasks.py`
 ├─ parser_service.parse_paper_file(paper.id, data, …) `tasks.py` → `parser_service.py` 的 `parse_paper_file()`
 │ ├─ _load_cached_bundle(`remove_local_file()`) 六条判据全中即重放（`cache_hit=True`），不再解析
 │ └─ parse_pdf(`run_reindex_job()`) 纯函数：无 DB / MinIO / 任务状态
 │ ├─ _resolve_backend() backend = 显式参数 > `PARSER_BACKEND`（默认 docling）
 │ ├─ backend=docling → _parse_with_docling()
 │ │ ├─ _docling_convert() `Semaphore(PARSER_CONCURRENCY)`
 │ │ │ └─ docling_client.convert_markdown(`docling_client.py` 的 `convert_markdown()`) → _convert_once()
 │ │ │（带公式一次 → 失败去公式重试一次 → 仍失败按瞬时/不可回旋分类）
 │ │ └─ prepare_docling_markdown(`markdown.py` 的 `prepare_docling_markdown()`) + page_spans_from_markdown(`page_spans_from_markdown()`)
 │ ├─ backend=pypdf → _parse_with_pypdf(`split_heading_number()`)
 │ │ ├─ layout.extract_page_layouts(`layout.py` 的 `extract_page_layouts()`) 两次提取（layout 文本 + 实时坐标）
 │ │ ├─ layout.reorder_pages(`reorder_pages()`) → strip_running_lines(`strip_running_lines()`) 顺序 + 页眉页脚
 │ │ ├─ structure.page_blocks(`structure.py` 的 `page_blocks()`) 标题 / 段落 / 表格行块
 │ │ ├─ markdown.render_markdown(`markdown.py` 的 `render_markdown()`) 写出共用方言
 │ │ └─ parser_version 打 `pypdf <版本>`（两侧都自报版本，混库才看得见）
 │ ├─ 任何 DoclingError → _merge_reasons(`split_heading_number()`) 前置 "docling unavailable …" → 落 pypdf
 │ └─ _report_degradation(`normalize_markdown()`) on_degrade("parsing", code, detail) → paper_degradations
 ├─ paper.parser_backend / .parser_version = bundle 戳 `tasks.py`（**混库可见**，[#3](#3-数据结构表字段索引或内存结构) 的列）
 ├─ degradations.resolve(STAGE_PARSING) `tasks.py`（本次没报的 parsing 降级就此作废）
 ├─ chunk_markdown(bundle, …, embed_fn=None|embed_texts,
 │ on_degrade=degradations) `tasks.py` → `chunking.py` 的 `chunk_markdown()`
 │ └─ pages_and_sections_from_markdown(`markdown.py` 的 `pages_and_sections_from_markdown()`) 反推 pages + sections
 │ → merge_short_sections(`structure.py` 的 `merge_short_sections()`) → chunk_document(`chunking.py` 的 `chunk_document()`)
 │ → 逐节 _chunk_section(`chunking.py`) → _semantic_pieces(`chunking.py`) → _report_fallback(`chunking.py`)
 │ → _split_oversized_piece(`chunking.py`)/_overlap_suffix(`chunking.py`)/_finalize(`chunking.py`)
 │ chunks 为空 → raise IngestionError("parsing produced no chunks") `tasks.py`
 ├─ _replace_chunks(...) `tasks.py` → 
 ├─ degradations.resolve(STAGE_CHUNKING) `tasks.py`（本次没报的降级就此作废）
 └─ embed_texts() → _write_embeddings() → _index_rows() → bulk_index_chunks() → _mark_indexed()
```

**解析后端的唯一入口**：上面这条链里的解析段就是全部 —— 线上不再有第二条文本来源。
`extract_pages`（`tasks.py`）**只**服务元数据启发式，产出不再进切块。换 `PARSER_BACKEND` 立刻改变
线上文本（默认已是 `docling`）；两后端产出同一份 markdown 方言，`chunk_markdown` 用 `markdown.py`
把方言反推回 `PageText`+`Section`，所以**下游只认 markdown，不认后端**。

````text
切后端对存量论文的两种做法：
 POST /api/papers/{id}/reindex → 用**当前** PARSER_BACKEND 重解析这一篇（缓存按 backend 分目录，换后端必然未命中 = 真跑一次远程解析；同后端且同选项才重放）
 重新导入同一份 PDF → 会被判成重复（指纹 = DOI/arXiv 身份，不是字节），**不会**换后端
````

失败落库：任何异常由 `_record_failure`（`tasks.py`）在**新事务里** `classify_failure` → `mark_failed(code=...)`，并把 `paper.status` 置 `FAILED`。

## 5. 不变量与踩过的坑

1. **页码 1-based，chunk 页码是区内 span 的 min/max**（`chunking.py`）；空白页不产生 span（`_section_pieces` 只收非空段落）。
2. **chunk 绝不跨章节**：每节独立调 `_chunk_section`，`next_index=len(chunks)`（`chunking.py`），故 `chunk_index` 文档内全局递增有序。
3. **硬上限 `MAX_TOKENS=450`**：`window_chars = max(1, min(target_chars, max_chars - overlap_chars))`（`chunking.py`），给 overlap 预留空间，否则超长段落永远带不上重叠。`tests/test_parsing.py` 的 `test_chunk_token_target_and_hard_cap()`、`Chunk` 断言 `token_count <= MAX_TOKENS`。
4. **参数校验**（`chunking.py`）：`target_tokens>0`、`overlap_tokens>=0` 且 `< target_tokens`、`max_tokens>=target_tokens`，否则 `ValueError`。
5. **控制字符必须删**：PostgreSQL `text` 不接受 NUL，其余 C0 会污染发往 OpenSearch 的 JSON。清单与剔除**只有一份**（`app/parsing/text.py` 的 `strip_control_chars()`），三条入口都过它：pypdf 逐页文本（`normalize_page_text()`）、内嵌元数据（`_text()`）、两后端共用的 markdown 方言（`normalize_markdown()`）。**docling 会把 PDF 的 NUL 原样带进它返回的 markdown**，所以只清 pypdf 侧不够 —— 那会让整条 ingest 死在 `INSERT INTO paper_chunks`（`DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`）；三条入口各有回归用例（`tests/test_parsing.py`、`tests/test_pdf_embedded.py`、`tests/test_markdown_fallback.py`、`tests/test_parser_service.py`）。改这条政策要 bump `PARSE_CACHE_VERSION`，否则旧缓存里的脏 markdown 会被原样重放。
6. **标题误判两处**：(a) running header——同一行在后续 3 行内复现即判页眉并入 `suppressed`，之后**同文本行被整篇跳过**（`structure.py`），正文里重复的短行也会被吞；(b) 全大写行当章节（`structure.py`，≤8 词、不以 `.` 结尾），`TABLE I ...` 这类表标题会被误判。
8. **段落不跨页**：`pending` 每页开头重置（`structure.py`），跨页段落被切成两段，后一段记到后一页页码。
9. **占位标题**：新 ingest 先用文件名当标题，那不是 claim，所以解析前 `_reset_placeholder_title` 清空（仅当无 `title` claim 且当前标题等于占位名，`tasks.py`），解析后 `_restore_placeholder_title` 兜底为占位名或 `"untitled"`（`title` NOT NULL）。
10. **文本层缺失不报错**：`extract_pages` 返回全空白页 → `detect_sections` 出空 → `chunk_document` 返回 `[]` → `tasks.py` 抛 `IngestionError("parsing produced no chunks")` → `NO_TEXT_LAYER`（关键字 `errors.py`，判定；端到端见 `tests/test_failure_classification.py`）。
11. **加密**：先试空口令 `reader.decrypt("")`（`pdf.py`），失败抛 `PdfParseError("encrypted PDF: password required")` → `ENCRYPTED_PDF`（关键字 `("encrypted""password")` 见 `errors.py`，分支）。**损坏**：`PdfReadError` / 其他构造异常 → `PdfParseError("invalid PDF: ...")` → `CORRUPT_PDF`（`errors.py`）。
13. **空字节流 → `PdfParseError("empty PDF payload")`**（`pdf.py`）。它**不会**命中 `UNSUPPORTED_TYPE`：`_KEYWORDS_UNSUPPORTED` 里的字面量是 `"empty payload"`（`errors.py`），`"empty PDF payload"` 不包含它，故走 `CORRUPT_PDF` 分支。此条为按代码字符串匹配的推断，未见测试固定 —— 标**未确认**。
14. **`chunk_document` docstring 与实现不符**：docstring 说"`sections` 可以不完整，剩余文本按 `Body` 切"，但代码只在 `sections` 为空或全无段落时才造 `Body`。传入部分覆盖的 `sections` 会丢文本；流水线里 `detect_sections` 覆盖全文，现网不触发 —— 隐性契约，标**未确认（无测试固定）**。
15. **`token_count` 是字符估算**：CJK 字符按 1.25 token/字计权（`_CJK_TOKENS_PER_CHAR`，e5 实测约 1-2 token/字），拉丁仍 4:1，混合文本两段相加（`chunking.py` 的 `estimate_tokens`）。若按 4 字符/token 估算，中文块会低估 ≥4 倍，1800 字符的中文块实际 1800+ token，embedding 尾部被 512 上限静默截断；**存量 chunk 的 `token_count` 与切分边界只在重导入/reindex 后更新**。
16. **没有客户端截断**：`embedding_service.embed_texts` 把 chunk 全文原样发服务端（payload 只有 `texts`/`model`），512 token 上限由服务端 + `MAX_TOKENS` 估算共同兜住。
17. **降级必须留痕**：语义模式嵌入失败不再只写日志 —— 每节经 `_report_fallback`（`chunking.py`）调 `on_degrade("chunking""semantic_fallback", {section, sentences, error})`，由 `degradation_service.Recorder`（`tasks.py`）落 `paper_degradations`；`Recorder` 吞掉记账自身的异常，**记账永远不会让作业失败**（单测 `tests/test_degradations.py`）。
18. **降级行随本次运行自动作废**：chunks 落库后立刻 `degradations.resolve(STAGE_CHUNKING)`（`tasks.py`），本次没报的 code 被盖上 `resolved_at`；因此 `scripts/reindex.py --degraded` 选出来的永远是「现在仍然降级」的论文（真机验证见 `docs/progress/project.md` [#21.7](../progress/project.md#217-t73-降级留痕--服务端推理队列2026-09-30)）。
19. **未启用的 sink 不改变行为**：`on_degrade=None` 时 `_report_fallback` 立即返回，单元测试与既有调用方（`tests/test_chunking_semantic.py`）不加参数即保持原语义。

20. **超长「标题」当正文，且必须留痕**：docling 会把「论文标题 + 作者块」并成一个一级标题，实测有一个 356 字符的 `#` 行。这种行不是章节标题：`pages_and_sections_from_markdown`（判定在）里标题超过 `MAX_SECTION_TITLE_CHARS=200`就**降级为正文**（正文不能丢，标题/作者信息全在里面），并经 `_report_long_heading`报 `on_degrade("chunking""section_title_too_long", {page, title_chars, limit, preview})` → `paper_degradations`。单测 `tests/test_markdown_sections.py`（降级为正文 / 走 sink / `chunk_markdown` 透传）。
20. **解析阶段的降级同样走这个 sink**：`parser_service.degradation_codes()` 把 `degraded_reason` 文本映射成 `docling_unavailable`/`formulas_as_text`/`table_structure_lost`/`reading_order_unverified`，兜底 `parse_degraded`（映射表、上报点，`parse_pdf(on_degrade=...)`）。

21. **交换格式 = docling 导出的 markdown**：`docling` 侧直接产出它，`pypdf` 侧由 `markdown.render_markdown` 归一化到同一份方言（页标记、层级、表格占位、公式词）。**下游切块只认 markdown、不认后端** —— 所以任何一侧改方言都要同时改 `markdown.py` 与另一侧的产出，并有 `tests/test_markdown_fallback.py` 钉形状。
22. **降级侧每页做两次 pypdf 提取**（`layout.py`）：`extract_text(extraction_mode="layout")` 给文本（间距保留成空格串），`extract_text(visitor_operand_after=…)` 给实时坐标。**两者不可兼得** —— layout 模式从不调用 visitor，而 `visitor_text` 的坐标带记忆化矩阵、滞后一拍（两栏页右栏坐标全 `(0,0)`）。代价实测仅 +几秒（6 个文件 10.6 s，含全部几何与重排）。
23. **重排只 permute，绝不增删字符**：`reorder_page_text` 只在文本层换顺序，安全网 `same_content`做**字符多重集**比对（忽略顺序、不忽略字符），不等即拒绝重排、保持原序并告警。这是「降级不丢内容」的硬约束。
24. **页眉页脚剔除只剔位置固定的行**（`layout.py`）：统计各页首/尾 2 行，≥60% 页重复且 <120 字符才剔；不做全文去重（正文里重复的短行不会被误删 —— 与 `structure.py` 那条 running-header 规则不同，见本表第 6 条）。
25. **`##` → `#` 的标题提升**（`markdown.promote_paper_title`）：docling 不产 `title` 标签（`DocItemLabel.TITLE` 会被重映射成 `SECTION_HEADER`，导致论文标题也是二级），所以把首个出现在 `Abstract` / `I.` 之前的 `##` 提为 `#`；6 级以上由 `clamp_heading_levels` 收敛（`####### REFERENCES` 收敛到 6 个 `#`）。
26. **docling 失败口径三分（别混）**：① 5xx / 超时 / 传输错误 / 200-非JSON body → `DoclingUnavailable`（瞬时，按 `DOCLING_MAX_RETRIES` 重试）；② 只有 HTTP 4xx 带 `client_error` 标记（`is_client_error`），**立刻抛、不重试、也不试「去公式」**；③ 200 + `status=failure`（服务端中止转换的返回形态）算**文档级**失败 → 不重发同样选项但仍走去公式重试。带公式那次**只打一枪**（失败模式确定：公式密集论文撞内存/算力上限），所以最坏请求数 `1+(1+retries)`。
27. **缓存命中六条判据，缺一不可**（`parser_service._load_cached_bundle`）：`cache_version` 相符、写入者就是现在要的后端、**当初的解析选项与现在完全一致**（`parse_options()`：OCR / 表格模式 / 公式开关与 preset / 页标记 / 页数上限，写进 `parse-meta.json` 的 `options` —— 配置即身份）、**当初不是降级产物**、markdown 对象还在、docling 服务端版本仍是当初那个（`GET /version` 探针）。**降级产物「写但不重放」** —— 否则一次 502 会被永久固化；版本探针失败（`None`）**不**判失效（「没人应答」不是「升级了」的证据）。选项这条是**故意从严**：一个 dict 管两个后端，docling 专属旋钮动了也会让 pypdf 产物作废 —— 本地重解析很便宜，重放一份与当前配置不符的 markdown 才贵。
28. **部分解析既不读也不写缓存**（`_partial_parse`）：只要调用方给了 `page_range` 或 `PARSER_MAX_PAGES>0` 就完全绕开缓存 —— 拿半篇冒充整篇比慢一点更糟。同时 `PARSER_MAX_PAGES` **只作用于 docling 分支**（变成 `page_range="1-N"` + `degraded_reason` 里的 `pages=1-N` + 账本 `pagination_truncated`）；pypdf 分支不截断（那档便宜，截断反而掩盖问题）。
29. **调 `parse_pdf` / `parse_paper_file` 的单测必须注入 store 与打桩 converter**：`parse_paper_file` 不传 `store` 会回落到 `_default_store()`（真 MinIO）。`tests/test_degradations.py` 的 sink 透传用例必须注内存 store，否则全量单测会往真 bucket 写假产物、攒出孤儿对象（`/api/consistency` 只报 `orphan_objects` 计数、不算 problem）。
30. **降级侧已知会丢字符，遇到就「不动」**：pypdf 的 layout 模式对非常规字形（τ 等）、部分标题渲染会丢字符（实测 `2404.05260` p1 丢 28、`1807.11311` p3 丢 394）—— 这些页 `same_content` 不过，**拒绝重排、保持原序并告警**；25 个真机双栏页里 13 页成功重排。表格 / 公式 / 图片在降级侧一律退级（占位或纯文本），3 列版面只保证「不乱序、不丢内容」（在最宽的中间空格串处切分，前两列可能同处一块）。

31. **解析在指纹升级之后**（`tasks.py` →）：判成重复 / 非主版本的论文在 `_resolve_target_paper` / `_discard_duplicate_paper` 就结束了，**不付一次 docling**。顺序反了会让每次重复上传都白跑一次远程解析 —— 这是接线时唯一不能调换的两步。
32. **markdown → 结构的反推必须与写出侧对称**（；写出侧）：`pages_and_sections_from_markdown` 只读页标记、`#` 层级与注释行，产出 `PageText` 与 `Section` 后交给**同一个** `chunk_document`。往方言里加新语法（新注释占位、新层级记号）必须同时改写出侧与反推侧，否则 chunk 页码/章节会静默偏移（`tests/test_markdown_sections.py` 是回归网）。
33. **两侧都自报版本**：docling 报 `docling-serve <ver> / docling <ver>`（`GET /version`，回落镜像 tag），pypdf 报 `pypdf <ver>`（`parser_service.py`）。空版本落 NULL 而不是空串—— 混库排查靠这个字段，别让「未知」和「没有」混在一起。
34. **同一份 PDF 换后端不能靠重导入**：指纹阶梯里 DOI/arXiv 是**身份**不是字节（见 [08-metadata.md](08-metadata.md)），同一篇论文再导一次会被判重复、**不会**换后端；换后端只有 `POST /api/papers/{id}/reindex`（产物缓存按 backend 分目录，换后端必然未命中 = 真解析）。docling 重索引用缓存重放 61.11 s，pypdf 重索引真跑 56.11 s。
35. **`PARSER_BACKEND` 只影响新解析与 reindex**：存量 chunk 的后端戳不会因改配置而变化（`GET /api/consistency` 的 `parser_backends` 就是拿来看这种混合状态的；它只报不修）。批量换后端的入口是**按戳选**：`scripts/reindex.py --parser-backend pypdf|docling|unknown`（`unknown` = 无戳，即 `refresh_index_metadata.py` 故意不写的存量论文），先 `--dry-run` 看清单；`GET /api/consistency?parser_papers=true` / `check_consistency.py --parser-papers` 给同一份论文 id 清单（`with_parser_papers`，`app/services/consistency_service.py`）。
36. **解析耗时算进 `chunking` 阶段**：`_advance_stage(STAGE_CHUNKING)` 之后才解析（`tasks.py`）—— 看作业进度时别把 docling 的几百秒当成切块慢。

## 6. 配置项（键 → 默认值 → 作用 → 出处文件:行）

**切块参数不在配置系统里**：目标/重叠/上限是模块常量，`tasks.py` 调 `chunk_markdown(bundle)` 时全用默认值，**没有环境变量能改**。

| 常量 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `MAX_TOKENS` | 450 | 单 chunk 硬上限（估算 token） | `app/parsing/chunking.py` |
| `DEFAULT_TARGET_TOKENS` | 400 | 目标长度 | `chunking.py` |
| `DEFAULT_OVERLAP_TOKENS` | 48 | 重叠窗口 | `chunking.py` |
| `CHARS_PER_TOKEN` | 4 | 字符→token 估算系数 | `chunking.py` |
| `MIN_CHUNK_CHARS` | 1 | 小于此长度不出 chunk | `chunking.py` |
| `SEMANTIC_*` / `DEGRADE_*` | 见 [#2](#2-关键文件与函数文件--函数类--作用) | 语义分块与降级词表 | `chunking.py` 的 `SEMANTIC_SIMILARITY_THRESHOLD` |
| `merge_short_sections(target_chars=)` | 1200 | 小节合并阈值（字符） | `structure.py`；调用处（`chunk_markdown` 内）用默认 |
| 标题三阈值 | 14 词 / 110 字符 / 回看 3 行 | 标题与页眉判定 | `structure.py` 的 `_MAX_HEADING_WORDS` |
| `detect_abstract(max_pages=)` | 2 | 摘要扫描页数 | `metadata_service.py` 的 `detect_authors()`；调用 用默认 |
| `detect_year` 范围 | 前 2 页、最高频（同频取大年份） | 年份启发式 | `metadata_service.py` |

环境变量（`app/core/config.py`）：

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | 写进 chunk 与索引文档 | `config.py`；使用 `tasks.py` |
| `EMBEDDING_DIMENSION` | 1024 | 向量维度校验 + 落库 | `config.py`；`embedding_service.py` |
| `EMBEDDING_BATCH_SIZE` | 32 | 单请求文本数 | `config.py`；`embedding_service.py` |
| `EMBEDDING_URL` | `http://localhost:8090` | 服务地址（`/embed`） | `config.py`；`embedding_service.py` |
| `EMBEDDING_TIMEOUT` / `EMBEDDING_MAX_RETRIES` | **300.0** / 2 | 超时与重试（须大于服务端排队时间） | `config.py` |
| `OPENSEARCH_INDEX` / `OPENSEARCH_ALIAS` | `paper_chunks_v3` / `paper_chunks_current` | chunk 文档落点（实际索引见 [01-storage.md](01-storage.md)） | `config.py` |
| `INGEST_MAX_FILE_MB` | 100 | 上游大小闸门（超限在解析之前就失败） | `config.py`；`ingestion_service.py` |
| `INGEST_CONCURRENCY` | 2 | 同时在跑的流水线条数 | `config.py` |
| `CHUNK_MODE` | `length` | 切块边界策略（`length` / `semantic`） | `config.py`；`chunking.py`；`tasks.py` |
| `CHUNK_SEMANTIC_THRESHOLD` | 0.80 | 语义模式：判定低谷的余弦阈值 | `config.py`；`chunking.py` |
| `CHUNK_SEMANTIC_MIN_TOKENS` | 200 | 语义模式：低谷处允许 flush 的最小块长 | `config.py`；`chunking.py` |
| `PARSER_BACKEND` | **`docling`** | `docling`（主后端）/ `pypdf`（降级侧）；非法值启动即报错（`config.py` 的 `PARSER_BACKENDS`） | `config.py`；`parser_service.py` |
| `PARSER_CONCURRENCY` | 1 | docling 转换的在途上限（模块级 `Semaphore`；**别调大** —— docling 是 CPU-bound 且容器有上限，并发只会一起变慢并撞内存天花板） | `config.py`；`parser_service.py` |
| `PARSER_CACHE` | true | 解析产物是否缓存到 MinIO；**部分解析既不读也不写**（第 5.28 节） | `config.py`；`parser_service.py` |
| `PARSER_MAX_PAGES` | 0（不限） | >0 时 **docling 分支**只解析前 N 页（`page_range="1-N"` + `pages=1-N` + 账本 `pagination_truncated`）；pypdf 分支不截断 | `config.py`；`parser_service.py` |

docling 侧（语义与部署值见 `.env.example` 的 docling 块与 `README.md` 第 3.6 节）：

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `DOCLING_URL` | `http://127.0.0.1:8091` | docling-serve 地址（本机部署在 **fnOS NAS**，`.env` 写 `http://<nas-ip>:8091`；缺这个键会回落到死端口 → 100% 降级） | `config.py` |
| `DOCLING_TIMEOUT` | 660.0 | 客户端读超时，**必须大于** `DOCLING_DOCUMENT_TIMEOUT`（否则会把快要跑完的解析扔掉） | `config.py` |
| `DOCLING_DOCUMENT_TIMEOUT` | 600.0 | 服务端 `document_timeout`（默认无期限会把 5 页论文烧 17 分钟；600 s 覆盖实测最差 252 s / 最慢 371 s） | `config.py` |
| `DOCLING_MAX_RETRIES` | 1 | 只作用于「去公式」那次：最坏请求数 `1+(1+retries)`（第 5.26 节） | `config.py` |
| `DOCLING_OCR` | false | **OCR 默认关**（语料是原生数字版；服务端默认开，所以显式关掉 —— 实测 OCR 掉一半以上吞吐） | `config.py` |
| `DOCLING_TABLE_MODE` | `accurate` | `accurate` / `fast` | `config.py` |
| `DOCLING_FORMULA_ENRICHMENT` | **false** | 公式 → LaTeX。**默认关**：最贵的一项（5 页 5.9s→39.2s、最坏 252s、5.9MB 论文整篇 ~310s vs pypdf ~52s）；关时请求发 `do_formula_enrichment=false` 且不发 preset。属于**缓存身份**（第 5.27 节） | `config.py` |
| `DOCLING_FORMULA_PRESET` | `codeformulav2` | **公式开时必须同时给**：单独给会 404（服务端报 `Preset 'default' not found`）；公式关时不下发。同样属于**缓存身份**（第 5.27 节） | `config.py` |
| `DOCLING_PAGE_BREAK` | `<!-- page-break -->` | 页标记字面量（两侧同一份方言；数量恒为 页数−1） | `config.py` |
| `DOCLING_IMAGE_TAG` | 空 | 组件镜像 tag；也是 `GET /version` 不可达时的 parser 版本回落值 | `config.py` |

**文档与代码不一致（以代码为准，「部署值 / 代码默认值」双写）**：`EMBEDDING_MODEL` 代码默认 `BAAI/bge-m3`（`config.py`）、batch 默认 32（`config.py`），部署值见根 `.env`（e5-large / 16）；`CHUNK_TARGET_TOKENS` 这个键**不存在**（只有常量 `DEFAULT_TARGET_TOKENS=400`）。512 token 上限在本仓代码中仍无断言或校验，出处只有模型本身 —— 标**未确认**。

## 7. 测试位置与覆盖

`tests/test_parsing.py`（288 行，26 用例）

| 覆盖 |
|---|
| `normalize_page_text`：单换行保留、空行压成 `\n\n`、CRLF 归一 |
| 章节识别：编号+已知标题顺序、子标题、表格行排除、小写数学片段排除、页码区间覆盖全文、无标题回落 `Body`、重复页眉不成分节、空页入参 |
| 切块：不跨节、页码与源页一致、token 目标与硬上限、长节重叠、无缝隙覆盖、超长段落字符窗、无 sections 时回落 `Body`、空输入、空白页忽略、非法参数 |
| `estimate_tokens` 与规格一致、`Chunk` 默认值（含 `is_overlap is False`）、`Section.label`、控制字符剔除、行结构保留 |

`tests/test_pdf_embedded.py`（213 行，10 用例；用 `PdfWriter` 现场造 PDF）

| 覆盖 |
|---|
| Info 字典字段；XMP 逐字段（`dc:*`/`prism:*`/`pdf:Keywords`）与 `raw["xmp"]` 快照；XMP 覆盖 Info |
| 内嵌文本中的 arXiv id；无元数据 → 空对象而非异常；损坏输入永不抛异常 |
| `as_dict` 只留真值；ISSN/ISBN 落 `raw`；只有 `startingPage` 时 `pages` 退化（`pdf.py`）；模块导出公开 helper |

相邻但直接相关的测试：

- `tests/test_failure_classification.py`：空白 PDF → 全 `is_blank` → `chunk_document == []` → `NO_TEXT_LAYER`（含 "OCR" 字样）；`tests/test_job_progress.py`：monkeypatch `chunk_document` 验证 `PARSING`/`CHUNKING` 阶段与进度顺序。
- `tests/test_degradations.py`：账本读写（幂等 upsert / 计数 / resolve / 复发重开 / stage 词表校验 / 级联删除）、`Recorder` 的 sink 语义与「记账失败不影响作业」、`chunk_document` 的 `on_degrade` 契约（长度模式不报、失败才报、无 sink 照常切块）、`parser_service.degradation_codes` 映射、`GET /api/papers/{id}/degradations`、`scripts/reindex.py` 的三种筛选（`--degraded`、`--parser-backend` 按戳、两者 AND）与 `--dry-run` 只列不写（`ensure_index`/`reindex_paper` 断言未被调用）。
- `tests/test_job_progress.py`：真跑一次 `_run_pipeline`（打桩外部依赖）→ 降级行带 `job_id` 落库，再跑一次干净的 → 该行 `resolved_at` 被盖上。
- `tests/test_consistency.py`（**第 6.1 节 加 8 例**）：`parser_stamp_mismatch` 双向普查（papers 列 vs 索引文档）、`unknown` 桶（未打戳的存量）、按后端分列计数、`by_backend` 子聚合。

解析后端的测试 —— **全部不碰真机**，真机对照一律走 `scripts/`（下表用例数以 `uv run pytest` 输出为准）：

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/test_docling_client.py` | 27 | 失败口径三分（5xx/超时/传输 → `DoclingUnavailable` 按 `DOCLING_MAX_RETRIES` 重试、4xx 不重试、200+`status=failure` 走去公式重试）、最坏请求数 `1+(1+retries)`、`build_request_fields` 字段组合、markdown 页数统计、`GET /version` 形状与回落 |
| `tests/test_layout_columns.py` | 23 | 手写坐标的几何页 + 现场合成的两栏 PDF（`logs/eval/two-column/` 同款构造）；金标准是 `interleaved` / `column-major` 两份输出**逐字节一致**；含 `same_content` 拒绝重排、3 列、全宽元素、重复页眉 |
| `tests/test_markdown_fallback.py` | 24 | 方言形状钉死：页标记、层级映射、表格占位、`PageSpan` 半开区间、`promote_paper_title`、`clamp_heading_levels` |
| `tests/test_parser_service.py` | 18 | 后端选择顺序（显式参数 > `PARSER_BACKEND`）、docling 失败降级并**前置**原因、`PdfParseError` 不捕获、`PARSER_MAX_PAGES` 截断 |
| `tests/test_parser_artifacts.py` | 25 | 假 store + 计数 converter：六条命中判据逐条否定（含**解析选项身份**：公式开关来回翻、页标记/表格模式/OCR/preset 各翻一次都必须重解析）、降级产物不重放、meta 坏掉/指向别的论文只 WARNING、写失败不影响返回 —— **不碰 MinIO** |
| `tests/test_acceptance_parser.py` | 15 | 验收脚本的纯函数（markdown 统计 / diff 摘要），不碰真机 |
| `tests/test_markdown_sections.py` | 16 | **markdown → 结构反推**（第 6.1 节）：页标记切页、`#` 层级 → `Section`（含 `number`/`title`）、编号识别（`III.` / `A.`）、注释行不当标题、跨页段落归属、空/无标记输入 |

## 8. 未做 / 已知缺口

| 缺口 | 依据 |
|---|---|
| **降级后端**（pypdf）无 OCR / 无版面分析（分栏靠 `layout.py` 几何重排、表格只留占位） | `errors.py` 仅提示；`pdf.py` 只走 `extract_text()` 默认顺序。docling 后端有版面顺序/表格/公式（`docs/progress/parser.md` [#5.9](../progress/parser.md#59-t8-双后端验收2026-09-29)：5 份输入真机对照，真表 1/3/4 张、公式 LaTeX 1/13/6 处、标题层级 11 vs pypdf 53） |
| 全大写短行规则会误报章节 | `structure.py`（`TABLE I` 等） |
| 死代码：`_ROMAN_TAIL`、`Chunk.is_overlap`（恒 `False`、无消费方） | `structure.py`；`chunking.py`，全仓无其它引用 |
| `_spans` 不进 `paper_chunks`/OpenSearch | 仅 `_finalize` 用来算页码 |
| 部分覆盖的 `sections` 会丢文本 | `chunking.py` 与该函数 docstring矛盾 |
| 512 token 上限本身无 embedding 侧硬校验（靠预算正确性兜住） | — |
| 句内语义断点仍可能落在超长段的字符窗里 | `chunking.py` 对超 `target_chars` 的 `_Piece` 只能按字符窗硬切（长度模式必然如此） |
| DOI/arXiv 规范化不在本模块 | `pdf.py` 的 `extract_embedded_metadata()`、`metadata_service.py` 原样返回，规范化在标识符层 |
| XMP 只认 `dc`/`prism`/`xmp` 三命名空间 | `pdf.py`，其余 ns 元素被跳过 |
| **按阶段重跑未提供** —— 只能整篇 reindex（PARSING→INDEXING 全跑）；想要的「只补 embedding / 只补索引」需要复用 `_write_embeddings`/`_index_rows`/`_mark_indexed` 写一个 `--stage` 入口。数据模型已经支持断点：`paper_chunks.embedded_at`/`indexed_at` 可空，`GET /api/consistency` 能报出 `missing_index` | 留档见 `docs/progress/project.md` [#21.7](../progress/project.md#217-t73-降级留痕--服务端推理队列2026-09-30)「后续优化方向」；`scripts/reindex.py` 的选谁参数已有 `--missing` / `--degraded[-stage/-code]` / `--parser-backend` |
| 语义模式把同一段文字嵌两遍（句子一遍、chunk 一遍，无复用） | `tasks.py` 与 打同一个 `EMBEDDING_URL`；留档见 `docs/architecture/04-embedding.md` [#8](04-embedding.md#8-未做--已知缺口) 缺口 11 |
| **两个解析码只在禁止降级时出现** | `PARSE_BACKEND_UNAVAILABLE` / `PARSE_FAILED` 已登记（`errors.py`）并有单测（`tests/test_failure_classification.py`）。正常流水线**不产生**它们 —— docling 不可达是**降级到 pypdf** + `degraded_reason` + `paper_degradations` 一行；要用这两个码得先加「strict parse」调用方（当前只有只读探针会撞上） |
| **降级侧 layout 模式会丢字符**：`2404.05260` p1 丢 28 字符、`1807.11311` p3 丢 394 字符（非常规字形/部分标题）→ 该页 `same_content` 不过，**拒绝重排、保持原序 + 告警**（宁可不动不可丢字）；实测 25 个真机双栏页只有 13 页成功重排 |；`docs/progress/parser.md` 第 6.1 节 |
| 降级侧 **3 列版面**只在最宽的中间空格串处切分，前两列可能同处一块（保证不乱序、不丢内容，但不保证理想顺序） | `layout.py`；`docs/progress/parser.md` 第 6.3 节 |
| 页眉页脚剔除是**启发式**（≥60% 页重复 + <120 字符），非常规版式可能漏剔或误剔位置固定的正文行 | `layout.py` 的 `strip_running_lines()`；`docs/progress/parser.md` 第 6.4 节 |
| **docling 侧公式降级没有真机样本**（要人为调小 `DOCLING_DOCUMENT_TIMEOUT` 才能造）；扫描版 PDF（`DOCLING_OCR` 默认关）、>15 页大论文、`PARSER_CONCURRENCY>1` 的行为也都没覆盖 | `docs/progress/parser.md` [#5.9](../progress/parser.md#59-t8-双后端验收2026-09-29)「本期没覆盖」 |
| **存量论文要 reindex 才会换后端**：改 `PARSER_BACKEND` 只影响新导入与 reindex（缓存按 `(paper_id, backend)` 分目录，换后端必然未命中）；混库状态看 `GET /api/consistency` 的 `parser_backends`，批量换后端用 `scripts/reindex.py --parser-backend pypdf\|unknown`（先 `--dry-run`）。**已提供入口，仍缺的是「全量重索引 + 检索侧 A/B」的实测数字** | 第 5.34 节/第 5.35 节 |
| **标题层级规范化 + pypdf 假标题过滤未做**：pypdf 侧的"标题"大量是假阳性（`# IEEE`、页码、全大写短行），反推适配器照单全收；两侧层级语义一致化留后续 | `structure.py`；`docs/progress/parser.md` [#6](../progress/parser.md#6-已知限制如实记录) |
| **反推适配器只认自己写出的方言**：`#` 之外的层级记号（docling 将来若新增标签）不会翻译，遇到即退化成段落 | `markdown.py` 的 `pages_and_sections_from_markdown()` |
| **标题层级规范化 / pypdf 假标题过滤未做**：docling 11 个真层级 vs pypdf 53 个（含 `# IEEE`、`# 2900 Boulevard…`、`# DFF` 等地址/图注假阳性）；切块按标题切边界，假标题 = 假边界 | `docs/examine/解析双后端验收-20260929.md` |
| 最慢一篇（`2606.09129` 371 s）已占 `DOCLING_DOCUMENT_TIMEOUT=600` 的 **62%**，更大更密的论文会先撞上限（撞了降级到 pypdf，不是硬失败） | `docs/progress/parser.md` [#4.3](../progress/parser.md#43-t8-双后端验收2026-09-295-份输入--2-后端) |
| docling 自带 chunker **不采用** | docling-core 2.99.0 依赖实测 + NAS `json_content` 实测 |
| 未确认项 | 空 payload 的 `error_code` 归类（第 5.13 节）、CJK 长度上限（第 5.15 节） |
