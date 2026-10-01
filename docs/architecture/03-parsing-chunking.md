# PDF 解析、结构识别与切块

| 项 | 内容 |
|---|---|
| 状态 | chunking / pdf / structure 的地基依据 commit 54048a3（2026-09-22）；**T7 增补**（语义分块 + 解析产物缓存）按 2026-09-29 工作树核对；**T7.3 增补**（降级留痕：`on_degrade` sink + `paper_degradations`）按 2026-09-30 工作树核对。**docling 侧已并入本文**（T4–T8；按 2026-09-29 工作树、`2c3d514` 之后核对）：§1–§8 每一节都含 docling / pypdf **两侧**条目。本文只写**结构与不变量** —— 逐段实现说明、实测数字与人工判读仍在 `docs/progress/parser.md`（§4.3 数字 / §5.1–§5.9 实现与验收）与 `docs/examine/解析双后端验收-20260929.md` |
| 关键文件 | `app/parsing/pdf.py`（584 行）、`app/parsing/structure.py`（318）、`app/parsing/chunking.py`（616）、`app/parsing/layout.py`（824，T5 降级侧几何）、`app/parsing/markdown.py`（362，共用方言）、`app/parsing/docling_client.py`（581，T4 客户端）、`app/services/parser_service.py`（657，T6+T7.1）、`app/services/degradation_service.py`（账本 sink）、`app/services/metadata_service.py`（457）、`app/core/errors.py`（223）、`app/workers/tasks.py`（1132，相关段 443-560（流水线）/ 962-1077（落库与索引）） |
| 相关文档 | `AGENTS.md` §3.6 / §3.7 / **§3.10（降级留痕）/ §3.11（解析后端契约）**、`docs/architecture/metadata-architecture.md` §5（发现分层）、**`docs/progress/parser.md`（解析线 T0–T10 全量记录）+ `docs/examine/解析双后端验收-20260929.md`（T8 人工判读）** |

## 1. 职责边界（做什么 / 不做什么）

做什么：

| 能力 | 实现位置 |
|---|---|
| 抽取文本层，逐页产出 `PageText`（1-based 页码） | `app/parsing/pdf.py:93` |
| 文本规范化：统一换行、丢控制字符、保留行结构 | `app/parsing/pdf.py:64` |
| 章节识别 + 段落化（硬换行拼回散文） | `app/parsing/structure.py:94`、`:132`、`:276`、`:285` |
| 远端 docling 解析：版面顺序 + 标题层级 + 真表格 + 公式 LaTeX → markdown（**T4**） | `app/parsing/docling_client.py:354` |
| 后端选择 / 降级留痕 / 解析产物缓存（**T6 + T7.1**） | `app/services/parser_service.py:135`（纯函数）、`:308`（带缓存） |
| 降级侧的几何重排与页眉页脚剔除（双栏：先左栏后右栏；**T5**） | `app/parsing/layout.py:651`、`:702` |
| 两后端**共用**的 markdown 方言（页标记 / 标题层级 / 归一化；**T5**） | `app/parsing/markdown.py:175`、`:416`、`:441` |
| 降级账本 sink（`parsing`/`chunking`/… 阶段词表；**T7.3**） | `app/services/degradation_service.py:81`、`:226` |
| 段落感知切块 + token 估算 + 重叠窗口 | `app/parsing/chunking.py:550` |
| 解析结果**反推**成 `PageText`+`Section`，把两后端收进同一条切块路（**§6.1**） | `app/parsing/markdown.py:272`、`:311`、`app/parsing/chunking.py:618` |
| 读 PDF 内嵌元数据（Info 字典 + XMP，含 PRISM） | `app/parsing/pdf.py:404` |
| 首页启发式元数据（标题/作者/摘要/年份/DOI/arXiv） | `app/services/metadata_service.py:342` |
| 失败归因到稳定 `error_code` | `app/core/errors.py:127` |

不做什么：

- **不做 OCR**：文本层缺失没有兜底，`NO_TEXT_LAYER_HINT` 明确写 "OCR is required (not supported yet)"（`errors.py:66`）。
- **不做「一个后端一套切块」**：两后端都把结果交给同一份 markdown 方言，`chunk_markdown`（`chunking.py:618`）反推出 sections 后走同一个 `chunk_document`（`:550`）。换 `PARSER_BACKEND` 换的是**文本来源**（顺序 / 标题 / 表格 / 公式），不是切块策略。
- **版面能力分两侧**：**默认后端是 `docling`**（2026-09-30 起，远端版面模型给阅读顺序 / 标题层级 / 真表格 / 公式），降级侧 `pypdf` 不做版面分析 —— 只用 `Page.extract_text()` 的默认顺序（`pdf.py:55`），分栏靠 `layout.py` 的**几何重排**补救、表格只留占位。两条路都**不做 OCR**（`DOCLING_OCR` 默认关），**公式默认也不做**（`DOCLING_FORMULA_ENRICHMENT` 默认关，2026-09-30 起：它是最贵的一项，实测 5 页 5.9s→39.2s、最坏一篇 252s）。
- **不做网络元数据**：发现分层 3-6 层（外部导入、DOI 内容协商、平台 API、模糊反查）不在本模块（`docs/architecture/metadata-architecture.md:157`）。
- **不写库、不切索引、不算向量**：本模块只返回内存对象；落 `paper_chunks`、调 embedding、bulk 到 OpenSearch 都在 `app/workers/tasks.py`。
- **不做标识符规范化**：DOI 原样返回并保留大小写（`pdf.py:496`；`tests/test_pdf_embedded.py:108` 断言保留 `10.1109/JSSC...`）。

## 2. 关键文件与函数（文件 → 函数/类 → 作用，带行号）

`app/parsing/pdf.py`

| 函数/类 | 行号 | 作用 |
|---|---|---|
| `_CONTROL_CHAR_TABLE` | 26-29 | 待删控制字符：C0 全删（除 `\t`/`\n`），另加 0x7F |
| `PdfParseError` | 32 | 字节流读不成 PDF 时的唯一异常出口 |
| `PageText` / `is_blank` | 36-49 | `page`(1-based) + `text`；`is_blank` = `not text.strip()` |
| `_page_text` | 52-61 | 单页抽取；单页异常吞掉记 warning 返回 `""` |
| `normalize_page_text` | 64-90 | CRLF→LF、translate 控制字符、连续空行压成 `\n\n`、逐行 strip |
| `extract_pages` | 93-129 | `PdfReader(BytesIO)` → 逐页 `PageText`；空 payload/损坏/加密/页树异常均转 `PdfParseError` |
| `_XMP_NS` / `_XMP_ACCESSORS` / `_XMP_LOCAL_NAMES` | 141-145 / 160-168 / 169-186 | 只认 `dc`/`prism`/`xmp`；本地名→pypdf accessor 兜底表；手解 16 个本地名（含 `doi`、`publicationName`、`startingPage`） |
| `_DOI_IN_TEXT` / `_ARXIV_IN_TEXT` / `_ISBN_IN_TEXT` / `_ISSN_IN_TEXT` | 147-153 | 从元数据文本捞标识符 |
| `EmbeddedMetadata` / `as_dict` / `is_empty` | 189-256 | 14 个可空字段 + `raw`；`as_dict` 只留非空值 |
| `_xmp_packet` | 278-329 | `ElementTree.fromstring(xmp.stream.get_data())` → `simple`（普通值）/ `alt`（`rdf:Alt` 按 `xml:lang` 分桶） |
| `_alt_value` / `_simple_value` | 332-350 | 语言优先序 `x-default` → `en-US` → `en` → 任意非空 |
| `_split_authors` / `_split_keywords` | 353-366 / 369-373 | 作者列表保原样、字符串按 `;`/`and`/`&`/换行切（**不按逗号**）；关键词按 `,`/`;` 切 |
| `_first_match` / `_year_from_date` / `_strip_version` | 376-401 | 首个正则命中（有捕获组取组 1，去尾 `.,;`）；年份取 `19xx/20xx`；arXiv 去 `vN` |
| `extract_embedded_metadata` | 404-519 | 层 1 主入口，**从不抛异常**，失败退化为空对象 |
| `SizedLine` / `extract_sized_lines` | 522-584 | `extract_text(visitor_text=...)` 取每段字号，按换行 flush 成 `(text, size)`，行字号取片段最大值（`:572`） |

`app/parsing/structure.py`

| 函数/常量 | 行号 | 作用 |
|---|---|---|
| `SECTION_BODY` | 21 | 兜底章节名 `"Body"` |
| `_NUMBERED_HEADING` | 23-25 | `^(数字[.数字]*\|[IVXivx]+(-字母)?)[.)]?\s+标题` |
| `_ROMAN_TAIL` | 26 | **已定义但全仓无引用（死代码）** |
| `_ALL_CAPS` | 27 | `^[A-Z][A-Z0-9 \-&/,:'()]+$` |
| `KNOWN_HEADINGS` | 30-55 | 25 个别名 → 规范名（`abstract`、`related work`、`references`、`appendix`…） |
| `_MAX_HEADING_WORDS` / `_MAX_HEADING_CHARS` / `_MAX_PAGE_HEADING_LOOKAHEAD` | 57-59 | 14 词 / 110 字符 / 回看 3 行 |
| `_NUMERIC_TOKEN` / `_looks_like_table_row` | 62-71 | 标题候选里数字 token 占比 ≥50% 判为表格行 |
| `Section` / `label` | 74-87 | `title/page_start/page_end/number/paragraphs:[(page,text)]`；`label = "number title"` |
| `_match_heading` | 94-122 | 三条识别规则的唯一入口 |
| `_heading_is_page_header` | 125-129 | 同一行在后续 3 行内复现 → 判 running header 并加入 `suppressed` |
| `detect_sections` | 132-186 | 逐页逐行状态机，`pending` 累积，遇标题闭合上节；末尾丢掉零段落占位节 |
| `_append_paragraphs` / `_join_wrapped_lines` | 276-284 / 285-295 | 按 `\n\n` 切段；`-\n` 断词缝合（`:290`），单换行转空格 |
| `PageBlock` / `page_blocks` | 190-204 / 207-275 | **T5 新增**：把一页切成「标题 / 段落 / 表格行块」，交给 `markdown.render_markdown` 渲染；`_append_paragraphs` 的老行为不变 |
| `merge_short_sections` | 297-319 | 相邻两节正文均 < `target_chars`（默认 1200，`:298`）**且次节标题全大写**时并入前节 |

`app/parsing/chunking.py`

| 项 | 行号 | 作用 |
|---|---|---|
| 常量 | 39-44 | `CHARS_PER_TOKEN=4`、`MAX_TOKENS=450`、`DEFAULT_TARGET_TOKENS=400`、`DEFAULT_OVERLAP_TOKENS=48`、`PARAGRAPH_SEPARATOR="\n\n"`、`MIN_CHUNK_CHARS=1` |
| `CHUNK_MODE_*` / `CHUNK_MODES` | 47-49 | `length` / `semantic`；与 `config.CHUNK_MODES` 必须一致（有单测钉住） |
| 语义常量 | 57-64 | `SEMANTIC_SIMILARITY_THRESHOLD=0.80`、`SEMANTIC_DIP_WINDOW=1`、`SEMANTIC_MIN_TOKENS=200`（T7.2） |
| 降级词表 | 69-75 / 105-107 | `DEGRADE_STAGE="chunking"`、`DEGRADE_SEMANTIC_FALLBACK="semantic_fallback"`、**`DEGRADE_HEADING_TOO_LONG="section_title_too_long"`（2026-09-30）**、`DegradeSink = Callable[[str, str, dict], None]`（**T7.3**） |
| `MAX_SECTION_TITLE_CHARS`（`:66`）/ `DEGRADE_HEADING_TOO_LONG`（`:68`）/ `_report_long_heading`（`:293`） | 66 / 68 / 293 | **2026-09-30**：`markdown.py` 里超过 200 字符的「标题」按正文处理（见不变量 20） |
| `estimate_tokens` | 104-108 | `max(1, len(text)//4)`，**不是 tiktoken** |
| `split_sentences` | 115-161 | 句子切分（保留终止符；`et al.`/`Fig. 5`/`J. Smith`/`Eq. 3` 不误断；换行与 CJK `。！？` 独立成句） |
| `cosine_similarity` / `similarity_dips` | 164-179 / 182-212 | 相邻句子余弦；**低于阈值且为邻域局部最小**才判为断点 |
| `Chunk` / `_Piece` / `_Pending` | 215-228 / 231-242 / 245-249 | 见 §3；`_Piece.break_before` 标记「此处有语义低谷」 |
| `_section_pieces` | 252-258 | 段落 → `_Piece`（长度模式的输入） |
| `_report_fallback` | 261-275 | **T7.3**：把一次降级交给 `on_degrade(stage, code, {section, sentences, ...})`；没有 sink 时是 no-op |
| `_semantic_pieces` | 278-372 | 语义模式的输入：句子嵌入 → 低谷处收束 + 段落空行保留；嵌入失败/向量数不符 → `None`（回落长度模式 + WARNING + `_report_fallback`，两处调用点 `:317`、`:335`） |
| `_split_oversized_piece` | 375-409 | 单段落超 `max_chars` 时按字符窗切，优先 `. ! ? \n`，其次空格；只给首个窗口保留 `break_before` |
| `_overlap_suffix` | 412-423 | 取尾部 `overlap*4` 字符，再从首个 `\n`/`. `/` ` 之后取起 |
| `_finalize` | 426-444 | 生成 `Chunk`；页码取 span 页面的 min/max |
| `_chunk_section` | 447-546 | 单节装配主循环；超 `target_chars` **或** 命中 `break_before` 且已 ≥ `semantic_min_tokens` 即 flush 并带 overlap 前缀重开；**T7.3** 接受 `on_degrade`（`:457`）并透传给 `_semantic_pieces`（`:479`） |
| `chunk_markdown` | 618-654 | **流水线入口（§6.1）**：`ParseBundle` → `pages_and_sections_from_markdown` → `merge_short_sections` → `chunk_document`（`embed_fn`/`on_degrade` 原样透传） |
| `chunk_document` | 550-616 | 入口：参数校验（含 `semantic_threshold`/`semantic_min_tokens`）→ 无有效节时造 `Body` 节 → 逐节切、全局递增 `chunk_index`；`embed_fn=None` 即长度模式；**T7.3** 新增 `on_degrade`（`:559`）并透传（`:613`） |

元数据与调度

| 函数 | 行号 | 作用 |
|---|---|---|
| `extract_metadata` | `metadata_service.py:342-373` | 层 2 入口，返回 `{title,abstract,year,authors,arxiv_id,doi}` |
| `detect_title` | `metadata_service.py:122-176` | 字号优先（前 40 行中 ≥ 最大字号 `*0.92` 的行拼接）：`:137`、`:143-144` |
| `detect_authors` / `_names_from_line` / `_plausible_author` | `metadata_service.py:191`、`:235`、`:262` | 标题行之后的作者块；无分隔符时按两词一组切分 |
| `detect_abstract` | `metadata_service.py:273-294` | `Abstract` 起，遇 `_SECTION_MARKER`/空行结束；扫描前 2 页 |
| `detect_year` / `_year_from_arxiv_id` | `metadata_service.py:313`、`:302` | arXiv 优先；否则首页 2 页最高频年份（同频取大年份 `:329`） |
| `detect_doi` | `metadata_service.py:333-339` | 首页首个 DOI 形态 token，去尾 `.,;` |
| `heuristic_claim_values` / `embedded_claim_values` | `metadata_service.py:379`、`:441` | 转成合并引擎吃的 `{provenance field: value}` |
| `_backfill_metadata` | `tasks.py:916-981` | 层 1 → 层 2 顺序写 claim |
| `_placeholder_title` / `_reset_placeholder_title` / `_restore_placeholder_title` | `tasks.py:888`、`:894`、`:910` | 文件名占位标题的清除与兜底 |
| `classify_failure` | `errors.py:127-197` | 异常 → `error_code`；两个解析码（`DoclingUnavailable`→`PARSE_BACKEND_UNAVAILABLE` :158、`DoclingFailed`→`PARSE_FAILED` :165）只在「明确要求 docling 且不许降级」时出现 |

`app/parsing/docling_client.py`（T4，581 行 —— docling-serve 客户端）

| 项 | 行号 | 作用 |
|---|---|---|
| `CONVERT_PATH` / `VERSION_PATH` | 43-44 | `/v1/convert/file`、`/version` |
| `HEADING_HIERARCHY_OPTIONS` | 52 | 请求固定带 `do_pdf_heading_hierarchy=true`（T3 决策 1；v1.35.0 的 markdown `heading_hierarchy_options` 是 no-op） |
| `STATIC_FIELDS` | 62 | 其余固定选项（`to_formats=md`；OCR / 表格 / 公式开关取自配置） |
| `FORMULA_FALLBACK_REASON` | 78 | 常量 `"formulas=text"` |
| `DoclingError` / `DoclingUnavailable` / `DoclingFailed` | 81 / 85 / 92 | 三类失败；`is_client_error`（`:100`）只对 HTTP 4xx 为真 |
| `DoclingResult` | 116-134 | 一次成功转换：`markdown/page_count/parser_version/processing_time/raw_json/formula_fallback/notes`；`degraded_reason` 由 `formula_fallback` 推出（`:129`） |
| `page_count_from_markdown` | 141-154 | 数 `<!-- page-break -->` 得页数（与 pypdf 侧同一判据） |
| `build_request_fields` | 156-200 | 组装 multipart 字段（`page_range` 来自 `PARSER_MAX_PAGES`） |
| `parse_conversion_payload` | 242-282 | 解析响应；`status=failure` 视作文档级失败 |
| `resolve_parser_version` / `version_from_server` / `_version_fallback` | 284 / 300 / 336 | `GET /version`（毫秒级，缓存探针用）；不可用则回落包版本 |
| `is_transient_failure` | 341-352 | 5xx / 超时 / 传输错误 → 可重试 |
| `convert_markdown` | 354-495 | **唯一对外入口**：带公式一次 → 失败去公式重试一次 → 仍失败按瞬时/不可回旋分类抛错 |
| `_convert_once` | 497-581 | 单次 HTTP 调用（重试与超时口径都在这里） |

`app/parsing/layout.py`（T5，824 行 —— 降级侧的几何重排）

| 项 | 行号 | 作用 |
|---|---|---|
| `ORDER_UNVERIFIED_REASON` | 40 | 常量 `"reading order not verified"` |
| `TextFragment` | 79-91 | `(text, x, y, size, page)`；`right` 是估算右边界（`_ADVANCE_RATIO`） |
| `PageLayout` | 95-103 | 一页的坐标 + **layout 模式文本**（重排的真来源） |
| `ColumnLayout` / `LayoutReport` / `RunningLinesResult` | 107 / 121 / 135 | 单页判定 / 页级报告 / 页眉页脚剔除结果 |
| `extract_page_layouts` | 223-299 | **两次 pypdf 提取**：layout 模式文本 + `visitor_operand_after` 实时坐标（见 §5.22） |
| `_shared_baselines` / `_detect_split` | 340 / 368-404 | 栏缝判定（宽度 25%–75%、两侧 ≥2 fragment、共享基线 ≥2 行） |
| `reorder_fragments` | 430-506 | 几何层重排 + 输出 `breaks`（段边界） |
| `_column_split` / `_split_layout_line` | 535-563 / 565-577 | 文本层按**最宽的中间空格串**切栏 |
| `reorder_page_text` | 579-617 | 文本层重排（段内先左后右，段间以空行分隔） |
| `same_content` | 619-629 | **安全网**：字符多重集比对，不等即拒绝重排 |
| `looks_multi_column` | 631-649 | 怀疑双栏但没修好 → 记 `reading order not verified` |
| `reorder_pages` | 651-696 | 逐页重排入口，返回 `LayoutReport` |
| `strip_running_lines` | 702-795 | 页眉页脚剔除（首/尾 2 行、≥60% 页重复、<120 字符） |
| `prepare_pages` | 797-824 | 重排 + 剔除的组合入口（`render_markdown` 调它） |

`app/parsing/markdown.py`（T5 + §6.1 适配器，497 行 —— 两后端共用的方言 + 反向适配）

| 项 | 行号 | 作用 |
|---|---|---|
| `PAGE_BREAK_DEFAULT` / `TABLE_FALLBACK_MARKER` | 47 / 50 | `<!-- page-break -->`、`<!-- table (structure unavailable in fallback) -->` |
| `DEGRADED_NO_FORMULA` / `DEGRADED_TABLE` / `DEGRADED_ORDER` | 53-55 | 三个降级词：`no formula latex` / `table structure` / `reading order not verified` |
| `PageSpan` | 62-73 | `page` + **半开**区间 `[char_start, char_end)`（`markdown[a:b]` 恰是一页） |
| `ParseBundle` | 77-95 | 解析器统一返回：`markdown/page_count/spans/backend/parser_version/degraded_reason/timings/headings/raw_json/cache_hit`（**字段冻结**，`AGENTS.md` §3.11） |
| `normalize_markdown` | 98-132 | LF 化、压空行、`html.unescape`（T8 后 `&gt;` 已反转义） |
| `heading_level` | 134-149 | `Section.number` / `title` → markdown 标题层级 |
| `page_spans_from_markdown` | 159-184 | 按页标记切 `PageSpan`，**两侧共用** |
| `clamp_heading_levels` | 186-200 | 超 6 级收敛（`####### REFERENCES` → 6 个 `#`） |
| `promote_paper_title` | 202-234 | 首个 `Abstract` / `I.` 之前的 `##` 提为 `#`（补 docling 不产 `title` 标签的短板） |
| `prepare_docling_markdown` | 236-243 | docling 导出的 markdown → 方言（归一化 + 层级钳制 + 标题提升） |
| `_render_block` / `render_markdown` | 380-395 / 396-476 | pypdf 侧：`PageBlock` → markdown（表格退级为占位、按 `Section` 映射层级） |
| `_HEADING_LINE` / `_NUMERIC_HEADING` / `_LETTER_HEADING` / `_COMMENT_LINE` | 243 / 247-251 / 252 / 255 | 识别标题行、`III.`/`A.` 编号与注释行（页标记与占位符不当标题） |
| `split_heading_number` | 258-277 | `III. METHOD` → `("III", "METHOD")`；无编号时返回 `(None, title)` |
| `pages_and_sections_from_markdown` | 279-368 | **§6.1 适配器**：按页标记切 `PageText`、按 `#` 层级切 `Section`、丢注释行 → 喂 `chunk_document` |

`app/services/parser_service.py`（T6 + T7.1，699 行）

| 项 | 行号 | 作用 |
|---|---|---|
| `DOCLING_FALLBACK_PREFIX` / `DOCLING_BACKEND` | 53-54 | `"docling unavailable"` 前缀 / 后端名 |
| `PARSE_CACHE_VERSION` / `PARSE_*_ARTIFACT` | 60-63 | 缓存版本 + 三个产物名（`document.md` / `document.json` / `parse-meta.json`） |
| `DEGRADE_STAGE` / `CODE_*` | 67-76 | 阶段名 `parsing` + 五个 code：`docling_unavailable` / `formulas_as_text` / `table_structure_lost` / `reading_order_unverified` / `pagination_truncated`，兜底 `parse_degraded` |
| `degradation_codes` | 95-105 | **唯一**的「原因文本 → 账本 code」映射表（新增原因必须在这里登记） |
| `_report_degradation` | 106-134 | 把 bundle 的降级交给 `on_degrade` |
| `ArtifactStore` | 119-133 | 缓存只需要的两个方法（`download_bytes` / `upload_bytes`）：生产用 MinIO、单测注内存实现 |
| `parse_pdf` | 135-195 | **纯函数入口**：后端选择 → docling / pypdf → 降级 → 报账（无 DB / MinIO / 任务状态） |
| `_max_pages_range` / `_partial_parse` | 196 / 206 | 截断区间、部分解析判据（既不读也不写缓存） |
| `parse_options` | 211-232 | **解析选项指纹**（§5.27 判据 3）：`{page_break, max_pages, ocr, table_mode, formula, formula_preset}` —— 请求发什么它就记什么，写进 `parse-meta.json` 的 `options`；两个后端共用一个 dict（故意从严） |
| `_resolve_backend` | 233-241 | 后端名解析（显式参数 > `PARSER_BACKEND`） |
| `_parse_with_docling` / `_docling_convert` / `_parse_with_pypdf` | 242 / 276 / 283 | 两侧实现；docling 转换受 `Semaphore(PARSER_CONCURRENCY)` 保护 |
| `_merge_reasons` | 299-307 | 降级理由拼接（docling 原因**在前**） |
| `parse_paper_file` | 308-410 | 带 MinIO 产物缓存的外层入口：先查缓存，未命中才解析并写产物 |
| `_default_store` | 411-417 | **生产回落点**：不传 `store` 就用真 MinIO（单测必须注入，见 §5.29） |
| `_artifact_key` / `_key_belongs_to` | 418 / 425-430 | 产物键位 + 防串到别的论文 |
| `_docling_version_probe` | 431-435 | 缓存命中判据第 5 条的 `GET /version` 探针 |
| `_load_cached_bundle` | 436-563 | **六条**判据 + 重放（`cache_hit=True`、`cache_load_s`） |
| `_store_bundle` | 564-632 | 写三个产物（meta 里带 `options`）；**meta 最后写**，半截写入 = 未命中 |
| `_decode_meta` / `_cached_json` / `_timings` / `_headings` / `_positive_int` / `_log_cache_problem` | 633 / 649 / 664 / 674 / 686 / 690-698 | meta 解析与容错（坏 meta、失败读取一律 WARNING，不抛） |

`app/services/degradation_service.py`（T7.3，342 行 —— 降级账本，契约见 `AGENTS.md` §3.10）

| 项 | 行号 | 作用 |
|---|---|---|
| `STAGE_PARSING` / `STAGE_CHUNKING` / `STAGE_EMBEDDING` / `STAGE_INDEXING` / `STAGES` | 41-52 | 阶段词表（`record()` 拒绝未知阶段） |
| `record` | 81-131 | 幂等 upsert：`UNIQUE(paper_id, stage, code)`，重复上报只 +`occurrences`，复发清 `resolved_at` |
| `resolve_stage` | 133-168 | 阶段完成时把本次**没报**的 code 盖上 `resolved_at` |
| `list_for_paper` / `open_degradations` / `paper_ids_with_open_degradations` | 170 / 186 / 209 | 查询入口（`GET /api/papers/{id}/degradations`、`reindex.py --degraded`） |
| `Recorder` | 226-315 | 流水线用的 sink：`__call__`（`:247`，吞掉自身异常）、`seen`（`:290`）、`resolve`（`:294`） |
| `stages_for_report` | 317-343 | 报告聚合 |

## 3. 数据结构（表/字段/索引，或内存结构）

内存结构（均为 `slots=True` dataclass）：

| 结构 | 字段 | 备注 |
|---|---|---|
| `PageText` | `page:int`、`text:str` | `page` 1-based，直接进 chunk 的 `page_start` |
| `SizedLine` | `text:str`、`size:float` | 字号用于标题判定 |
| `EmbeddedMetadata` | `title/authors/abstract/doi/arxiv_id/venue/volume/issue/pages/publication_date/year/language/keywords/raw` | 全可空；`raw={info, xmp, issns, isbns}`（`pdf.py:512-517`） |
| `Section` | `title`、`page_start`、`page_end`、`number`、`paragraphs:[(page, text)]` | `label` 是 `number + title` |
| `Chunk` | `chunk_index`、`text`、`page_start`、`page_end`、`section`、`section_title`、`token_count`、`char_count`、`is_overlap`、`_spans` | `_spans` 仅供 `_finalize` 推页码；`is_overlap` 恒 `False` 且无消费方 |
| `TextFragment` | `text`、`x`、`y`、`size`、`page` | 降级侧几何判定的输入；`right` 是估算右边界（`layout.py:88-91`） |
| `PageLayout` | `page`、`width`、`fragments`、`text` | `text` 是 pypdf layout 模式文本 —— 重排的真来源（`layout.py:95-103`） |
| `ColumnLayout` | `lines`、`columns`、`split_x`、`applied`、`warning`、`breaks` | `breaks` = 调用方必须插段落边界的位置（栏边界或全宽元素）（`layout.py:107-117`） |
| `LayoutReport` | `pages`、`columns`、`reordered_pages`、`unverified_pages`、`warnings`、`failed` | `unverified_pages` 就是记 `reading order not verified` 的依据（`layout.py:121-131`） |
| `RunningLinesResult` | `pages`、`dropped`、`page_numbers` | 页眉页脚剔除结果（`layout.py:135-140`） |
| `PageBlock` | `kind`（`heading`/`paragraph`/`table`）、`page`、`text`、`lines`、`number`、`title` | pypdf 侧渲染 markdown 的中间结构（`structure.py:190-204`） |
| `PageSpan` | `page`、`char_start`、`char_end` | **半开**区间（`markdown.py:78-89`）；chunking 按它反查页码 |
| `ParseBundle` | `markdown`、`page_count`、`spans`、`backend`、`parser_version`、`degraded_reason`、`timings`、`headings`、`raw_json`、`cache_hit` | 解析器统一返回（`markdown.py:93-111`）；**字段已冻结** |
| `DoclingResult` | `markdown`、`page_count`、`parser_version`、`processing_time`、`raw_json`、`formula_fallback`、`notes` | 客户端层结果（`docling_client.py:116-134`）；`degraded_reason` 由 `formula_fallback` 推出 |

落库映射（`tasks.py:1066-1098`；模型 `app/db/models.py:384-429`）：

| `Chunk` 字段 | `paper_chunks` 列 | 约束/索引 |
|---|---|---|
| `chunk_index` | `chunk_index` | `uq_paper_chunks_paper_index`（唯一 `paper_id+chunk_index`） |
| `page_start`/`page_end` | 同名列 | `ck_paper_chunks_page_range`（`page_end >= page_start`） |
| `section` | `section` | `ix_paper_chunks_paper_section(paper_id, section)` |
| `section_title` | `subsection`（仅当 `section_title != section` 才写，`:985-987`） | **`text`**（2026-09-30 起；原 `varchar(255)`，见不变量 20） |
| `text`/`token_count`/`char_count` | 同名列 | `text` NOT NULL |
| — | `embedding_model`/`embedding_dimension` | 取 `settings`（`tasks.py:1090-1091`） |

OpenSearch 文档字段（`tasks.py:1135-1186`）：`chunk_id/paper_id/title/authors/year/venue/doi/arxiv_id/tags/section/section_title/page_start/page_end/chunk_index/text/embedding/embedding_model/embedding_dimension/**parser_backend/parser_version**`；其中 `section_title = row.subsection or row.section`（`:1161`）。注意过滤字段是索引时快照，改元数据后需 reindex 才生效（`AGENTS.md` §3.6）。

## 4. 调用链（从入口到落地，逐跳）

```
run_ingestion_job(tasks.py:141) / run_reindex_job(:121)
└─ _process_job(:213) → _run_pipeline(...)(:446)
   ├─ _advance_stage(STAGE_PARSING, 45.0)               tasks.py:482
   ├─ object_storage.download_bytes(object_key)          tasks.py:484
   ├─ extract_pages(data)                                tasks.py:489 → pdf.py:93
   │  └─ PdfReader → 逐页 _page_text(pdf.py:52) → normalize_page_text(pdf.py:64)
   │     ← **只喂元数据启发式**（§6.1 决策②）：文档文本的真实来源是下面的解析后端
   ├─ (可选) _resolve_target_paper(...)                  tasks.py:493（非主版本就地结束 :500-501）
   ├─ _reset_placeholder_title(...)                      tasks.py:503 → :894
   ├─ _backfill_metadata(session, paper, pages, data)    tasks.py:500 → :916
   │  ├─ extract_embedded_metadata(pdf_bytes)            pdf.py:404 → _xmp_packet(pdf.py:278)
   │  ├─ embedded_claim_values(...)                      metadata_service.py:441 → merge_values(`tasks.py:960`, confidence 1.0)
   │  ├─ extract_metadata(pages, url, pdf_bytes)         metadata_service.py:342
   │  │  └─ extract_sized_lines(pdf.py:530) → detect_title(:120) → detect_authors(:189)
   │  │     → detect_abstract(:271) → detect_year(:311) → arxiv_id_from_url(:51)/_from_text(:64) → detect_doi(:331)
   │  └─ heuristic_claim_values(...)                     metadata_service.py:379 → merge_values(`tasks.py:985`, confidence 0.5)
   ├─ _restore_placeholder_title(...)                    tasks.py:505 → :910
   ├─ _upgrade_fingerprint(..., discard_on_conflict=dedupe) tasks.py:511 → :590
   │  ← 解析**挪到这一步之后**：判成重复/非主版本的论文不再白付一次 docling（§6.1）
   ├─ degradation_service.Recorder(...)                  tasks.py:478（T7.3 降级 sink）
   ├─ _advance_stage(STAGE_CHUNKING, 60.0)               tasks.py:518
   ├─ parser_service.parse_paper_file(paper.id, data, …)  tasks.py:524 → parser_service.py:308
   │  ├─ _load_cached_bundle(:440)         六条判据全中即重放（`cache_hit=True`），不再解析
   │  └─ parse_pdf(:138)                   纯函数：无 DB / MinIO / 任务状态
   │     ├─ _resolve_backend(:214)         backend = 显式参数 > `PARSER_BACKEND`（默认 docling）
   │     ├─ backend=docling → _parse_with_docling(:223)
   │     │  ├─ _docling_convert(:257)      `Semaphore(PARSER_CONCURRENCY)`
   │     │  │  └─ docling_client.convert_markdown(docling_client.py:354) → _convert_once(:497)
   │     │  │     （带公式一次 → 失败去公式重试一次 → 仍失败按瞬时/不可回旋分类）
   │     │  └─ prepare_docling_markdown(markdown.py:416) + page_spans_from_markdown(:175)
   │     ├─ backend=pypdf → _parse_with_pypdf(:274)
   │     │  ├─ layout.extract_page_layouts(layout.py:223)   两次提取（layout 文本 + 实时坐标）
   │     │  ├─ layout.reorder_pages(:651) → strip_running_lines(:702)   顺序 + 页眉页脚
   │     │  ├─ structure.page_blocks(structure.py:207)      标题 / 段落 / 表格行块
   │     │  ├─ markdown.render_markdown(markdown.py:441)     写出共用方言
   │     │  └─ parser_version 打 `pypdf <版本>`（两侧都自报版本，混库才看得见）
   │     ├─ 任何 DoclingError → _merge_reasons(:290) 前置 "docling unavailable …" → 落 pypdf
   │     └─ _report_degradation(:119)      on_degrade("parsing", code, detail) → paper_degradations
   ├─ paper.parser_backend / .parser_version = bundle 戳  tasks.py:532-535（**混库可见**，§3 的列）
   ├─ degradations.resolve(STAGE_PARSING)                tasks.py:536（本次没报的 parsing 降级就此作废）
   ├─ chunk_markdown(bundle, …, embed_fn=None|embed_texts,
   │                 on_degrade=degradations)            tasks.py:546-553 → chunking.py:618
   │  └─ pages_and_sections_from_markdown(markdown.py:311) 反推 pages + sections
   │     → merge_short_sections(structure.py:297) → chunk_document(chunking.py:550)
   │        → 逐节 _chunk_section(:448) → _semantic_pieces(:279) → _report_fallback(:262)
   │           → _split_oversized_piece(:376)/_overlap_suffix(:413)/_finalize(:427)
   │     chunks 为空 → raise IngestionError("parsing produced no chunks")  tasks.py:555
   ├─ _replace_chunks(...)                               tasks.py:556 → :1066
   ├─ degradations.resolve(STAGE_CHUNKING)               tasks.py:559（本次没报的降级就此作废）
   └─ embed_texts(:563) → _write_embeddings(:569) → _index_rows(:575) → bulk_index_chunks(:576) → _mark_indexed(:581)
```

**解析后端的唯一入口（T6–T8，2026-09-30 接线）**：上面这条链里的解析段就是全部 —— 线上不再有第二条文本来源。
`extract_pages`（`tasks.py:489`）**只**服务元数据启发式，产出不再进切块。换 `PARSER_BACKEND` 立刻改变
线上文本（默认已是 `docling`）；两后端产出同一份 markdown 方言，`chunk_markdown` 用 `markdown.py:311`
把方言反推回 `PageText`+`Section`，所以**下游只认 markdown，不认后端**。

````text
切后端对存量论文的两种做法（2026-09-30 真机验证）：
  POST /api/papers/{id}/reindex   → 用**当前** PARSER_BACKEND 重解析这一篇（缓存按 backend 分目录，
                                    换后端必然未命中 = 真跑一次远程解析；同后端且同选项才重放）
  重新导入同一份 PDF               → 会被判成重复（指纹 = DOI/arXiv 身份，不是字节），**不会**换后端
````

失败落库：任何异常由 `_record_failure`（`tasks.py:1201-1220`）在**新事务里** `classify_failure` → `mark_failed(code=...)`，并把 `paper.status` 置 `FAILED`。

## 5. 不变量与踩过的坑

1. **页码 1-based，chunk 页码是区内 span 的 min/max**（`chunking.py:431-433`）；空白页不产生 span（`_section_pieces` 只收非空段落，`:253-259`）。
2. **chunk 绝不跨章节**：每节独立调 `_chunk_section`，`next_index=len(chunks)`（`chunking.py:610`），故 `chunk_index` 文档内全局递增有序。
3. **硬上限 `MAX_TOKENS=450`**：`window_chars = max(1, min(target_chars, max_chars - overlap_chars))`（`chunking.py:468`），给 overlap 预留空间，否则超长段落永远带不上重叠。`tests/test_parsing.py:171`、`:216` 断言 `token_count <= MAX_TOKENS`。
4. **参数校验**（`chunking.py:577-590`）：`target_tokens>0`、`overlap_tokens>=0` 且 `< target_tokens`、`max_tokens>=target_tokens`，否则 `ValueError`。
5. **控制字符必须删**：PostgreSQL `text` 不接受 NUL，历史上整批 arXiv 作业因此失败；其余 C0 会污染发往 OpenSearch 的 JSON（`pdf.py:23-25`、`:72-75`）。
6. **标题误判两处**：(a) running header——同一行在后续 3 行内复现即判页眉并入 `suppressed`，之后**同文本行被整篇跳过**（`structure.py:149-155`），正文里重复的短行也会被吞；(b) 全大写行当章节（`structure.py:120-121`，≤8 词、不以 `.` 结尾），`TABLE I ...` 这类表标题会被误判。
8. **段落不跨页**：`pending` 每页开头重置（`structure.py:146`），跨页段落被切成两段，后一段记到后一页页码。
9. **占位标题**：新 ingest 先用文件名当标题，那不是 claim，所以解析前 `_reset_placeholder_title` 清空（仅当无 `title` claim 且当前标题等于占位名，`tasks.py:894-909`），解析后 `_restore_placeholder_title` 兜底为占位名或 `"untitled"`（`title` NOT NULL，`:883-890`）。
10. **文本层缺失不报错**：`extract_pages` 返回全空白页 → `detect_sections` 出空 → `chunk_document` 返回 `[]` → `tasks.py:554-555` 抛 `IngestionError("parsing produced no chunks")` → `NO_TEXT_LAYER`（关键字 `errors.py:69-75`，判定 `:215-218`；端到端见 `tests/test_failure_classification.py:230-241`）。
11. **加密**：先试空口令 `reader.decrypt("")`（`pdf.py:109-115`），失败抛 `PdfParseError("encrypted PDF: password required")` → `ENCRYPTED_PDF`（关键字 `("encrypted","password")` 见 `errors.py:68`，分支 `:171-177`）。**损坏**：`PdfReadError` / 其他构造异常 → `PdfParseError("invalid PDF: ...")` → `CORRUPT_PDF`（`errors.py:177`）。
13. **空字节流 → `PdfParseError("empty PDF payload")`**（`pdf.py:100-101`）。它**不会**命中 `UNSUPPORTED_TYPE`：`_KEYWORDS_UNSUPPORTED` 里的字面量是 `"empty payload"`（`errors.py:81`），`"empty PDF payload"` 不包含它，故走 `CORRUPT_PDF` 分支。此条为按代码字符串匹配的推断，未见测试固定 —— 标**未确认**。
14. **`chunk_document` docstring 与实现不符**：docstring 说"`sections` 可以不完整，剩余文本按 `Body` 切"（`chunking.py:564-565`），但代码只在 `sections` 为空或全无段落时才造 `Body`（`:592-600`）。传入部分覆盖的 `sections` 会丢文本；流水线里 `detect_sections` 覆盖全文，现网不触发 —— 隐性契约，标**未确认（无测试固定）**。
15. **`token_count` 是字符估算**：`len(text)//4`（`chunking.py:105-109`），假设 4 字符/token。CJK 约 1 字/token，同一 `MAX_TOKENS=450` 对中文论文实际更松，而索引 `title/text/section_title` 用的正是 `cjk` 分词器（`AGENTS.md` §3.5/§3.6）。代码里没有 CJK 专用估算 —— 标**未确认（无 CJK 长度测试）**。
16. **没有客户端截断**：`embedding_service.embed_texts` 把 chunk 全文原样发服务端（`app/services/embedding_service.py:77-99`，payload 只有 `texts`/`model`），512 token 上限由服务端 + `MAX_TOKENS` 估算共同兜住。
17. **降级必须留痕（T7.3）**：语义模式嵌入失败不再只写日志 —— 每节经 `_report_fallback`（`chunking.py:262-276`）调 `on_degrade("chunking", "semantic_fallback", {section, sentences, error})`，由 `degradation_service.Recorder`（`tasks.py:478-480`）落 `paper_degradations`；`Recorder` 吞掉记账自身的异常，**记账永远不会让作业失败**（`degradation_service.py:247-283`，单测 `tests/test_degradations.py`）。
18. **降级行随本次运行自动作废**：chunks 落库后立刻 `degradations.resolve(STAGE_CHUNKING)`（`tasks.py:559`），本次没报的 code 被盖上 `resolved_at`；因此 `scripts/reindex.py --degraded` 选出来的永远是「现在仍然降级」的论文（真机验证见 `docs/progress/project.md` §21.7）。
19. **未启用的 sink 不改变行为**：`on_degrade=None` 时 `_report_fallback` 立即返回，单元测试与既有调用方（`tests/test_chunking_semantic.py`）不加参数即保持原语义。

20. **超长「标题」当正文，且必须留痕（2026-09-30，真机踩出来的）**：docling 会把「论文标题 + 作者块」并成一个一级标题，实测有一个 356 字符的 `#` 行。这种行不是章节标题：`pages_and_sections_from_markdown`（`markdown.py:311`，判定在 `:376`）里标题超过 `MAX_SECTION_TITLE_CHARS=200`（`:66`）就**降级为正文**（正文不能丢，标题/作者信息全在里面），并经 `_report_long_heading`（`:293-307`）报 `on_degrade("chunking", "section_title_too_long", {page, title_chars, limit, preview})` → `paper_degradations`。根因是 `paper_chunks.section`/`subsection` 当时是 `varchar(255)`：`2205.00360` 与 `2604.07387` 就是这样**整篇导入失败**（`value too long for type character varying(255)`）。两处一起改：列宽迁移 `8d3f5c1b7a20_chunk_section_text`（→ `text`）+ 适配器护栏；缺任何一半都不够（护栏挡住新数据，迁移修好历史库）。单测 `tests/test_markdown_sections.py`（降级为正文 / 走 sink / `chunk_markdown` 透传）。
20. **解析阶段的降级同样走这个 sink**：`parser_service.degradation_codes()` 把 `degraded_reason` 文本映射成 `docling_unavailable`/`formulas_as_text`/`table_structure_lost`/`reading_order_unverified`，兜底 `parse_degraded`（`parser_service.py:95-104` 映射表、`:106-117` 上报点，`parse_pdf(on_degrade=...)`）。**T8 的验收已完成，但流水线仍未接 `parse_paper_file`**（见 §4「解析后端的两条路」），切换属 plan §6.1。

21. **交换格式 = docling 导出的 markdown**（T3 决策 1–5）：`docling` 侧直接产出它，`pypdf` 侧由 `markdown.render_markdown` 归一化到同一份方言（页标记、层级、表格占位、公式词）。**下游切块只认 markdown、不认后端** —— 所以任何一侧改方言都要同时改 `markdown.py` 与另一侧的产出，并有 `tests/test_markdown_fallback.py` 钉形状。
22. **降级侧每页做两次 pypdf 提取**（`layout.py:223-299`）：`extract_text(extraction_mode="layout")` 给文本（间距保留成空格串），`extract_text(visitor_operand_after=…)` 给实时坐标。**两者不可兼得** —— layout 模式从不调用 visitor，而 `visitor_text` 的坐标带记忆化矩阵、滞后一拍（两栏页右栏坐标全 `(0,0)`）。代价实测仅 +几秒（6 个文件 10.6 s，含全部几何与重排）。
23. **重排只 permute，绝不增删字符**：`reorder_page_text` 只在文本层换顺序，安全网 `same_content`（`layout.py:619-629`）做**字符多重集**比对（忽略顺序、不忽略字符），不等即拒绝重排、保持原序并告警。这是「降级不丢内容」的硬约束。
24. **页眉页脚剔除只剔位置固定的行**（`layout.py:702-795`）：统计各页首/尾 2 行，≥60% 页重复且 <120 字符才剔；不做全文去重（正文里重复的短行不会被误删 —— 与 `structure.py` 那条 running-header 规则不同，见本表第 6 条）。
25. **`##` → `#` 的标题提升**（`markdown.promote_paper_title:202-234`）：docling 不产 `title` 标签（`DocItemLabel.TITLE` 会被重映射成 `SECTION_HEADER`，导致论文标题也是二级），所以把首个出现在 `Abstract` / `I.` 之前的 `##` 提为 `#`；6 级以上由 `clamp_heading_levels:186-200` 收敛（`####### REFERENCES` 收敛到 6 个 `#`）。
26. **docling 失败口径三分（T4 定案，别混）**：① 5xx / 超时 / 传输错误 / 200-非JSON body → `DoclingUnavailable`（瞬时，按 `DOCLING_MAX_RETRIES` 重试）；② 只有 HTTP 4xx 带 `client_error` 标记（`is_client_error:100`），**立刻抛、不重试、也不试「去公式」**；③ 200 + `status=failure`（服务端中止转换的返回形态）算**文档级**失败 → 不重发同样选项但仍走去公式重试。带公式那次**只打一枪**（失败模式确定：公式密集论文撞内存/算力上限），所以最坏请求数 `1+(1+retries)`。
27. **缓存命中六条判据，缺一不可**（`parser_service._load_cached_bundle`）：`cache_version` 相符、写入者就是现在要的后端、**当初的解析选项与现在完全一致**（`parse_options()`：OCR / 表格模式 / 公式开关与 preset / 页标记 / 页数上限，写进 `parse-meta.json` 的 `options` —— 配置即身份，2026-09-30 增）、**当初不是降级产物**、markdown 对象还在、docling 服务端版本仍是当初那个（`GET /version` 探针）。**降级产物「写但不重放」** —— 否则一次 502 会被永久固化；版本探针失败（`None`）**不**判失效（「没人应答」不是「升级了」的证据）。选项这条是**故意从严**：一个 dict 管两个后端，docling 专属旋钮动了也会让 pypdf 产物作废 —— 本地重解析很便宜，重放一份与当前配置不符的 markdown 才贵。
28. **部分解析既不读也不写缓存**（`_partial_parse:204`）：只要调用方给了 `page_range` 或 `PARSER_MAX_PAGES>0` 就完全绕开缓存 —— 拿半篇冒充整篇比慢一点更糟。同时 `PARSER_MAX_PAGES` **只作用于 docling 分支**（变成 `page_range="1-N"` + `degraded_reason` 里的 `pages=1-N` + 账本 `pagination_truncated`）；pypdf 分支不截断（那档便宜，截断反而掩盖问题）。
29. **调 `parse_pdf` / `parse_paper_file` 的单测必须注入 store 与打桩 converter**：`parse_paper_file` 不传 `store` 会回落到 `_default_store()`（真 MinIO）。2026-09-29 踩过：`tests/test_degradations.py` 的 sink 透传用例漏了 `store`，每次全量单测都往真 bucket 写一对 6 字节假产物，攒出 16 个孤儿对象（`/api/consistency` 只报 `orphan_objects` 计数、不算 problem，所以一直没暴露）。现在该测试注内存 store，孤儿已清零。
30. **降级侧已知会丢字符，遇到就「不动」**：pypdf 的 layout 模式对非常规字形（τ 等）、部分标题渲染会丢字符（实测 `2404.05260` p1 丢 28、`1807.11311` p3 丢 394）—— 这些页 `same_content` 不过，**拒绝重排、保持原序并告警**；25 个真机双栏页里 13 页成功重排。表格 / 公式 / 图片在降级侧一律退级（占位或纯文本），3 列版面只保证「不乱序、不丢内容」（在最宽的中间空格串处切分，前两列可能同处一块）。

31. **解析在指纹升级之后**（`tasks.py:511` → `:524`）：判成重复 / 非主版本的论文在 `_resolve_target_paper` / `_discard_duplicate_paper` 就结束了，**不付一次 docling**。顺序反了会让每次重复上传都白跑一次远程解析 —— 这是接线时唯一不能调换的两步。
32. **markdown → 结构的反推必须与写出侧对称**（`markdown.py:311`；写出侧 `:441`）：`pages_and_sections_from_markdown` 只读页标记、`#` 层级与注释行，产出 `PageText` 与 `Section` 后交给**同一个** `chunk_document`。往方言里加新语法（新注释占位、新层级记号）必须同时改写出侧与反推侧，否则 chunk 页码/章节会静默偏移（`tests/test_markdown_sections.py` 是回归网）。
33. **两侧都自报版本**：docling 报 `docling-serve <ver> / docling <ver>`（`GET /version`，回落镜像 tag），pypdf 报 `pypdf <ver>`（`parser_service.py:283`）。空版本落 NULL 而不是空串（`:567`）—— 混库排查靠这个字段，别让「未知」和「没有」混在一起。
34. **同一份 PDF 换后端不能靠重导入**：指纹阶梯里 DOI/arXiv 是**身份**不是字节（`AGENTS.md` §3.9），同一篇论文再导一次会被判重复、**不会**换后端；换后端只有 `POST /api/papers/{id}/reindex`（产物缓存按 backend 分目录，换后端必然未命中 = 真解析）。2026-09-30 真机：docling 重索引用缓存重放 61.11 s，pypdf 重索引真跑 56.11 s。
35. **`PARSER_BACKEND` 只影响新解析与 reindex**：存量 chunk 的后端戳不会因改配置而变化（`GET /api/consistency` 的 `parser_backends` 就是拿来看这种混合状态的；它只报不修）。批量换后端的入口是**按戳选**：`scripts/reindex.py --parser-backend pypdf|docling|unknown`（`unknown` = 无戳，即 `refresh_index_metadata.py` 故意不写的存量论文），先 `--dry-run` 看清单；`GET /api/consistency?parser_papers=true` / `check_consistency.py --parser-papers` 给同一份论文 id 清单（`with_parser_papers`，`app/services/consistency_service.py:492`）。
36. **解析耗时算进 `chunking` 阶段**：`_advance_stage(STAGE_CHUNKING)` 之后才解析（`tasks.py:518` `:524`）—— 看作业进度时别把 docling 的几百秒当成切块慢。

## 6. 配置项（键 → 默认值 → 作用 → 出处文件:行）

**切块参数不在配置系统里**：目标/重叠/上限是模块常量，`tasks.py:546` 调 `chunk_markdown(bundle)` 时全用默认值，**没有环境变量能改**。

| 常量 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `MAX_TOKENS` | 450 | 单 chunk 硬上限（估算 token） | `app/parsing/chunking.py:41` |
| `DEFAULT_TARGET_TOKENS` | 400 | 目标长度 | `chunking.py:42` |
| `DEFAULT_OVERLAP_TOKENS` | 48 | 重叠窗口 | `chunking.py:43` |
| `CHARS_PER_TOKEN` | 4 | 字符→token 估算系数 | `chunking.py:40` |
| `MIN_CHUNK_CHARS` | 1 | 小于此长度不出 chunk | `chunking.py:45` |
| `SEMANTIC_*` / `DEGRADE_*` | 见 §2 | 语义分块与降级词表（T7.2 / T7.3） | `chunking.py:58-76` |
| `merge_short_sections(target_chars=)` | 1200 | 小节合并阈值（字符） | `structure.py:297`；调用处 `chunking.py:648`（`chunk_markdown` 内）用默认 |
| 标题三阈值 | 14 词 / 110 字符 / 回看 3 行 | 标题与页眉判定 | `structure.py:57-59` |
| `detect_abstract(max_pages=)` | 2 | 摘要扫描页数 | `metadata_service.py:273`；调用 `:364` 用默认 |
| `detect_year` 范围 | 前 2 页、最高频（同频取大年份） | 年份启发式 | `metadata_service.py:325-329` |

环境变量（`app/core/config.py`）：

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | 写进 chunk 与索引文档 | `config.py:80`；使用 `tasks.py:1090`、`:1089` |
| `EMBEDDING_DIMENSION` | 1024 | 向量维度校验 + 落库 | `config.py:81`；`embedding_service.py:67-74` |
| `EMBEDDING_BATCH_SIZE` | 32 | 单请求文本数 | `config.py:82`；`embedding_service.py:98-99` |
| `EMBEDDING_URL` | `http://localhost:8090` | 服务地址（`/embed`） | `config.py:79`；`embedding_service.py:21` |
| `EMBEDDING_TIMEOUT` / `EMBEDDING_MAX_RETRIES` | **300.0** / 2 | 超时与重试（T7.3 由 120 上调，须大于服务端排队时间） | `config.py:87-88` |
| `OPENSEARCH_INDEX` / `OPENSEARCH_ALIAS` | `paper_chunks_v3` / `paper_chunks_current` | chunk 文档落点（实际索引见 `AGENTS.md` §3.5） | `config.py:68-69` |
| `INGEST_MAX_FILE_MB` | 100 | 上游大小闸门（超限在解析之前就失败） | `config.py:146`；`ingestion_service.py:490` |
| `INGEST_CONCURRENCY` | 2 | 同时在跑的流水线条数 | `config.py:152` |
| `CHUNK_MODE` | `length` | 切块边界策略（`length` / `semantic`，T7.2） | `config.py:221`；`chunking.py:48-50`；`tasks.py:518-553` |
| `CHUNK_SEMANTIC_THRESHOLD` | 0.80 | 语义模式：判定低谷的余弦阈值 | `config.py:226`；`chunking.py:58` |
| `CHUNK_SEMANTIC_MIN_TOKENS` | 200 | 语义模式：低谷处允许 flush 的最小块长 | `config.py:231`；`chunking.py:65` |
| `PARSER_BACKEND` | **`docling`** | `docling`（主后端）/ `pypdf`（降级侧）；非法值启动即报错（`config.py:24` 的 `PARSER_BACKENDS`）。2026-09-30 接线时翻默认 | `config.py:203`；`parser_service.py:233` |
| `PARSER_CONCURRENCY` | 1 | docling 转换的在途上限（模块级 `Semaphore`；**别调大** —— docling 是 CPU-bound 且容器有上限，并发只会一起变慢并撞内存天花板） | `config.py:207`；`parser_service.py:276` |
| `PARSER_CACHE` | true | 解析产物是否缓存到 MinIO（T7.1）；**部分解析既不读也不写**（§5.28） | `config.py:209`；`parser_service.py:308` |
| `PARSER_MAX_PAGES` | 0（不限） | >0 时 **docling 分支**只解析前 N 页（`page_range="1-N"` + `pages=1-N` + 账本 `pagination_truncated`）；pypdf 分支不截断 | `config.py:212`；`parser_service.py:196` |

docling 侧（T4；语义与部署值见 `.env.example` 的 docling 块与 `README.md` §3.6）：

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `DOCLING_URL` | `http://127.0.0.1:8091` | docling-serve 地址（本机部署在 **fnOS NAS**，`.env` 写 `http://192.168.31.53:8091`；缺这个键会回落到死端口 → 100% 降级） | `config.py:241` |
| `DOCLING_TIMEOUT` | 660.0 | 客户端读超时，**必须大于** `DOCLING_DOCUMENT_TIMEOUT`（否则会把快要跑完的解析扔掉） | `config.py:245` |
| `DOCLING_DOCUMENT_TIMEOUT` | 600.0 | 服务端 `document_timeout`（默认无期限会把 5 页论文烧 17 分钟；600 s 覆盖实测最差 252 s / T8 最慢 371 s） | `config.py:249` |
| `DOCLING_MAX_RETRIES` | 1 | 只作用于「去公式」那次：最坏请求数 `1+(1+retries)`（§5.26） | `config.py:254` |
| `DOCLING_OCR` | false | **OCR 默认关**（语料是原生数字版；服务端默认开，所以显式关掉 —— 实测 OCR 掉一半以上吞吐） | `config.py:257` |
| `DOCLING_TABLE_MODE` | `accurate` | `accurate` / `fast` | `config.py:259` |
| `DOCLING_FORMULA_ENRICHMENT` | **false** | 公式 → LaTeX（T3 决策 5）。**默认关**（2026-09-30）：最贵的一项（5 页 5.9s→39.2s、最坏 252s、5.9MB 论文整篇 ~310s vs pypdf ~52s）；关时请求发 `do_formula_enrichment=false` 且不发 preset。属于**缓存身份**（§5.27） | `config.py:269` |
| `DOCLING_FORMULA_PRESET` | `codeformulav2` | **公式开时必须同时给**：单独给会 404（服务端报 `Preset 'default' not found`）；公式关时不下发。同样属于**缓存身份**（§5.27） | `config.py:275` |
| `DOCLING_PAGE_BREAK` | `<!-- page-break -->` | 页标记字面量（两侧同一份方言；数量恒为 页数−1） | `config.py:280` |
| `DOCLING_IMAGE_TAG` | 空 | 组件镜像 tag；也是 `GET /version` 不可达时的 parser 版本回落值 | `config.py:285` |

**文档与代码不一致（以代码为准；2026-09-22 已对齐 AGENTS.md 措辞）**：`AGENTS.md` §3.7 原写"模型 `intfloat/multilingual-e5-large`""batch 默认 16""`CHUNK_TARGET_TOKENS≈400`"；代码里 `EMBEDDING_MODEL` 默认 `BAAI/bge-m3`（`config.py:82`）、batch 默认 32（`config.py:80`）、`CHUNK_TARGET_TOKENS` 这个键**不存在**（只有常量 `DEFAULT_TARGET_TOKENS=400`）。差异原因是文档写的是**部署值**（根 `.env`：e5-large / 16），代码写的是**默认值**；现已改成"部署值 X / 代码默认 Y"的写法。512 token 上限在本仓代码中仍无断言或校验，出处只有模型本身 —— 标**未确认**。

## 7. 测试位置与覆盖

`tests/test_parsing.py`（288 行，26 用例）

| 用例行号 | 覆盖 |
|---|---|
| `:35-51` | `normalize_page_text`：单换行保留、空行压成 `\n\n`、CRLF 归一 |
| `:55-127` | 章节识别：编号+已知标题顺序、子标题、表格行排除、小写数学片段排除、页码区间覆盖全文、无标题回落 `Body`、重复页眉不成分节、空页入参 |
| `:136-249` | 切块：不跨节、页码与源页一致、token 目标与硬上限、长节重叠、无缝隙覆盖、超长段落字符窗、无 sections 时回落 `Body`、空输入、空白页忽略、非法参数 |
| `:250-287` | `estimate_tokens` 与规格一致、`Chunk` 默认值（含 `is_overlap is False`）、`Section.label`、控制字符剔除、行结构保留 |

`tests/test_pdf_embedded.py`（213 行，10 用例；用 `PdfWriter` 现场造 PDF）

| 用例行号 | 覆盖 |
|---|---|
| `:54` / `:76` / `:112` | Info 字典字段；XMP 逐字段（`dc:*`/`prism:*`/`pdf:Keywords`）与 `raw["xmp"]` 快照；XMP 覆盖 Info |
| `:139` / `:152` / `:162` | 内嵌文本中的 arXiv id；无元数据 → 空对象而非异常；损坏输入永不抛异常 |
| `:169` / `:179` / `:193` / `:212` | `as_dict` 只留真值；ISSN/ISBN 落 `raw`；只有 `startingPage` 时 `pages` 退化（`pdf.py:486`）；模块导出公开 helper |

相邻但直接相关的测试：

- `tests/test_failure_classification.py:221-244`：空白 PDF → 全 `is_blank` → `chunk_document == []` → `NO_TEXT_LAYER`（含 "OCR" 字样）；`tests/test_job_progress.py:180`：monkeypatch `chunk_document` 验证 `PARSING`/`CHUNKING` 阶段与进度顺序。
- `tests/test_degradations.py`（**T7.3**）：账本读写（幂等 upsert / 计数 / resolve / 复发重开 / stage 词表校验 / 级联删除）、`Recorder` 的 sink 语义与「记账失败不影响作业」、`chunk_document` 的 `on_degrade` 契约（长度模式不报、失败才报、无 sink 照常切块）、`parser_service.degradation_codes` 映射、`GET /api/papers/{id}/degradations`、`scripts/reindex.py` 的三种筛选（`--degraded`、`--parser-backend` 按戳、两者 AND）与 `--dry-run` 只列不写（`ensure_index`/`reindex_paper` 断言未被调用）。
- `tests/test_job_progress.py`（新增 1 例，**T7.3**）：真跑一次 `_run_pipeline`（打桩外部依赖）→ 降级行带 `job_id` 落库，再跑一次干净的 → 该行 `resolved_at` 被盖上。接线后该文件已改用 `chunk_markdown` 桩（`_run_pipeline` 的解析入口变了）。
- `tests/test_consistency.py`（**§6.1 加 8 例**）：`parser_stamp_mismatch` 双向普查（papers 列 vs 索引文档）、`unknown` 桶（未打戳的存量）、按后端分列计数、`by_backend` 子聚合。

解析后端（T4–T8）的测试 —— **全部不碰真机**，真机对照一律走 `scripts/`（下表用例数为 2026-09-29 工作树实测，以 `uv run pytest` 输出为准）：

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `tests/test_docling_client.py` | 27 | 失败口径三分（5xx/超时/传输 → `DoclingUnavailable` 按 `DOCLING_MAX_RETRIES` 重试、4xx 不重试、200+`status=failure` 走去公式重试）、最坏请求数 `1+(1+retries)`、`build_request_fields` 字段组合、markdown 页数统计、`GET /version` 形状与回落 |
| `tests/test_layout_columns.py` | 23 | 手写坐标的几何页 + 现场合成的两栏 PDF（`logs/eval/two-column/` 同款构造）；金标准是 `interleaved` / `column-major` 两份输出**逐字节一致**；含 `same_content` 拒绝重排、3 列、全宽元素、重复页眉 |
| `tests/test_markdown_fallback.py` | 24 | 方言形状钉死：页标记、层级映射、表格占位、`PageSpan` 半开区间、`promote_paper_title`、`clamp_heading_levels` |
| `tests/test_parser_service.py` | 18 | 后端选择顺序（显式参数 > `PARSER_BACKEND`）、docling 失败降级并**前置**原因、`PdfParseError` 不捕获、`PARSER_MAX_PAGES` 截断（T8 修） |
| `tests/test_parser_artifacts.py` | 25 | 假 store + 计数 converter：六条命中判据逐条否定（含**解析选项身份**：公式开关来回翻、页标记/表格模式/OCR/preset 各翻一次都必须重解析）、降级产物不重放、meta 坏掉/指向别的论文只 WARNING、写失败不影响返回 —— **不碰 MinIO** |
| `tests/test_acceptance_parser.py` | 15 | 验收脚本的纯函数（markdown 统计 / diff 摘要），不碰真机 |
| `tests/test_markdown_sections.py` | 16 | **markdown → 结构反推**（§6.1）：页标记切页、`#` 层级 → `Section`（含 `number`/`title`）、编号识别（`III.` / `A.`）、注释行不当标题、跨页段落归属、空/无标记输入 |

## 8. 未做 / 已知缺口

| 缺口 | 依据 |
|---|---|
| **降级后端**（pypdf）无 OCR / 无版面分析（分栏靠 `layout.py` 几何重排、表格只留占位） | `errors.py:65-66` 仅提示；`pdf.py:55` 只走 `extract_text()` 默认顺序。docling 后端有版面顺序/表格/公式（`docs/progress/parser.md` §5.9：5 份输入真机对照，真表 1/3/4 张、公式 LaTeX 1/13/6 处、标题层级 11 vs pypdf 53） |
| 全大写短行规则会误报章节 | `structure.py:120-121`（`TABLE I` 等） |
| 死代码：`_ROMAN_TAIL`、`Chunk.is_overlap`（恒 `False`、无消费方） | `structure.py:26`；`chunking.py:201`、`:398`，全仓无其它引用 |
| `_spans` 不进 `paper_chunks`/OpenSearch | 仅 `_finalize` 用来算页码 |
| 部分覆盖的 `sections` 会丢文本 | `chunking.py:495-503` 与该函数 docstring（`:517-518`）矛盾 |
| CJK token 估算偏差 + 512 上限无代码侧校验 | `chunking.py:79-83` 固定 4 字符/token；512 只在 `AGENTS.md` §3.7，`embedding_service.py` 无截断 |
| 句内语义断点仍可能落在超长段的字符窗里 | `chunking.py:294-323` 对超 `target_chars` 的 `_Piece` 只能按字符窗硬切（长度模式必然如此） |
| DOI/arXiv 规范化不在本模块 | `pdf.py:496`、`metadata_service.py:333` 原样返回，规范化在标识符层 |
| XMP 只认 `dc`/`prism`/`xmp` 三命名空间 | `pdf.py:141-145`，其余 ns 元素被跳过（`:305-306`） |
| **按阶段重跑尚未提供（T7.3 决策：暂不做）** —— 现在只能整篇 reindex（PARSING→INDEXING 全跑）；想要的「只补 embedding / 只补索引」需要复用 `_write_embeddings`/`_index_rows`/`_mark_indexed` 写一个 `--stage` 入口。数据模型已经支持断点：`paper_chunks.embedded_at`/`indexed_at` 可空，`GET /api/consistency` 能报出 `missing_index` | 留档见 `docs/progress/project.md` §21.7「后续优化方向」；`scripts/reindex.py` 的选谁参数已有 `--missing` / `--degraded[-stage/-code]` / `--parser-backend` |
| 语义模式把同一段文字嵌两遍（句子一遍、chunk 一遍，无复用） | `tasks.py:552` 与 :563 打同一个 `EMBEDDING_URL`；留档见 `docs/architecture/04-embedding.md` §8 缺口 11 |
| **两个解析码只在禁止降级时出现** | `PARSE_BACKEND_UNAVAILABLE` / `PARSE_FAILED` 已登记（`errors.py:48-63`）并有单测（`tests/test_failure_classification.py:85-101`）。正常流水线**不产生**它们 —— docling 不可达是**降级到 pypdf** + `degraded_reason` + `paper_degradations` 一行；要用这两个码得先加「strict parse」调用方（当前只有只读探针会撞上） |
| **降级侧 layout 模式会丢字符**：`2404.05260` p1 丢 28 字符、`1807.11311` p3 丢 394 字符（非常规字形/部分标题）→ 该页 `same_content` 不过，**拒绝重排、保持原序 + 告警**（宁可不动不可丢字）；实测 25 个真机双栏页只有 13 页成功重排 | `layout.py:619-629`；`docs/progress/parser.md` §6.1 |
| 降级侧 **3 列版面**只在最宽的中间空格串处切分，前两列可能同处一块（保证不乱序、不丢内容，但不保证理想顺序） | `layout.py:535-577`；`docs/progress/parser.md` §6.3 |
| 页眉页脚剔除是**启发式**（≥60% 页重复 + <120 字符），非常规版式可能漏剔或误剔位置固定的正文行 | `layout.py:702-795`；`docs/progress/parser.md` §6.4 |
| **docling 侧公式降级没有真机样本**（要人为调小 `DOCLING_DOCUMENT_TIMEOUT` 才能造）；扫描版 PDF（`DOCLING_OCR` 默认关）、>15 页大论文、`PARSER_CONCURRENCY>1` 的行为也都没覆盖 | `docs/progress/parser.md` §5.9「本期没覆盖」 |
| ~~`PARSER_BACKEND` 未翻默认 / 流水线未接线~~ **2026-09-30 已完成**：默认翻了 `docling`，流水线走 `parse_paper_file` + `chunk_markdown`，解析戳入库入索引 | `tasks.py:524`、`:546`；`config.py:203`；本文件 §4 |
| **存量论文要 reindex 才会换后端**：改 `PARSER_BACKEND` 只影响新导入与 reindex（缓存按 `(paper_id, backend)` 分目录，换后端必然未命中）；混库状态看 `GET /api/consistency` 的 `parser_backends`，批量换后端用 `scripts/reindex.py --parser-backend pypdf\|unknown`（先 `--dry-run`）。**已提供入口，仍缺的是「全量重索引 + 检索侧 A/B」的实测数字**（plan §6.1 ③） | §5.34/§5.35 |
| **标题层级规范化 + pypdf 假标题过滤未做**：pypdf 侧的"标题"大量是假阳性（`# IEEE`、页码、全大写短行），反推适配器照单全收；两侧层级语义一致化留后续 | `structure.py:120-121`；`docs/progress/parser.md` §6 |
| **反推适配器只认自己写出的方言**：`#` 之外的层级记号（docling 将来若新增标签）不会翻译，遇到即退化成段落 | `markdown.py:311-413` |
| **标题层级规范化 / pypdf 假标题过滤未做**：T8 判读 docling 11 个真层级 vs pypdf 53 个（含 `# IEEE`、`# 2900 Boulevard…`、`# DFF` 等地址/图注假阳性）；切块按标题切边界，假标题 = 假边界 | `docs/examine/解析双后端验收-20260929.md` |
| 最慢一篇（`2606.09129` 371 s）已占 `DOCLING_DOCUMENT_TIMEOUT=600` 的 **62%**，更大更密的论文会先撞上限（撞了降级到 pypdf，不是硬失败）。**主人 2026-09-29 定：时限先不动** | `docs/progress/parser.md` §4.3 |
| docling 自带 chunker（T7 的**方案 A，已评估、不采用**） | plan §6.2（留档：docling-core 2.99.0 依赖实测 + NAS `json_content` 实测） |
| 未确认项 | 空 payload 的 `error_code` 归类（§5.13）、CJK 长度上限（§5.15）；模型名/batch/`CHUNK_TARGET_TOKENS` 的文档口径已于 2026-09-22 对齐（§6） |
