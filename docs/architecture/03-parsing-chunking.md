# PDF 解析、结构识别与切块

| 项 | 内容 |
|---|---|
| 状态 | chunking / pdf / structure 的地基依据 commit 54048a3（2026-09-22）；**T7 增补**（语义分块 + 解析产物缓存）按 2026-09-29 工作树核对；**T7.3 增补**（降级留痕：`on_degrade` sink + `paper_degradations`）按 2026-09-30 工作树核对。**docling 侧（T4–T6：`docling_client.py`/`layout.py`/`markdown.py`/`parser_service.py`）尚未并入本文，权威在 `docs/progress/parser.md`** |
| 关键文件 | `app/parsing/pdf.py`（584 行）、`app/parsing/structure.py`（231）、`app/parsing/chunking.py`（627）、`app/services/metadata_service.py`（457）、`app/core/errors.py`（199）、`app/workers/tasks.py`（1113，相关段 443-536 / 877-967 / 1045-1073）；解析后端另见 `app/parsing/{docling_client,layout,markdown}.py`、`app/services/parser_service.py` |
| 相关文档 | `AGENTS.md` §3.6 / §3.7、`docs/architecture/metadata-architecture.md` §5（发现分层） |

## 1. 职责边界（做什么 / 不做什么）

做什么：

| 能力 | 实现位置 |
|---|---|
| 抽取文本层，逐页产出 `PageText`（1-based 页码） | `app/parsing/pdf.py:93` |
| 文本规范化：统一换行、丢控制字符、保留行结构 | `app/parsing/pdf.py:64` |
| 章节识别 + 段落化（硬换行拼回散文） | `app/parsing/structure.py:94`、`:132`、`:189`、`:198` |
| 段落感知切块 + token 估算 + 重叠窗口 | `app/parsing/chunking.py:549` |
| 读 PDF 内嵌元数据（Info 字典 + XMP，含 PRISM） | `app/parsing/pdf.py:404` |
| 首页启发式元数据（标题/作者/摘要/年份/DOI/arXiv） | `app/services/metadata_service.py:340` |
| 失败归因到稳定 `error_code` | `app/core/errors.py:116` |

不做什么：

- **不做 OCR**：文本层缺失没有兜底，`NO_TEXT_LAYER_HINT` 明确写 "OCR is required (not supported yet)"（`errors.py:55`）。
- **不做版面/分栏/表格识别**：只用 `pypdf` 的 `Page.extract_text()` 默认阅读顺序（`pdf.py:55`）。
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
| `_append_paragraphs` / `_join_wrapped_lines` | 189-207 | 按 `\n\n` 切段；`-\n` 断词缝合（`:205`），单换行转空格 |
| `merge_short_sections` | 210-231 | 相邻两节正文均 < `target_chars`（默认 1200）**且次节标题全大写**时并入前节 |

`app/parsing/chunking.py`

| 项 | 行号 | 作用 |
|---|---|---|
| 常量 | 39-44 | `CHARS_PER_TOKEN=4`、`MAX_TOKENS=450`、`DEFAULT_TARGET_TOKENS=400`、`DEFAULT_OVERLAP_TOKENS=48`、`PARAGRAPH_SEPARATOR="\n\n"`、`MIN_CHUNK_CHARS=1` |
| `CHUNK_MODE_*` / `CHUNK_MODES` | 47-49 | `length` / `semantic`；与 `config.CHUNK_MODES` 必须一致（有单测钉住） |
| 语义常量 | 57-64 | `SEMANTIC_SIMILARITY_THRESHOLD=0.80`、`SEMANTIC_DIP_WINDOW=1`、`SEMANTIC_MIN_TOKENS=200`（T7.2） |
| 降级词表 | 69-75 | `DEGRADE_STAGE="chunking"`、`DEGRADE_SEMANTIC_FALLBACK="semantic_fallback"`、`DegradeSink = Callable[[str, str, dict], None]`（**T7.3**） |
| `estimate_tokens` | 104-111 | `max(1, len(text)//4)`，**不是 tiktoken** |
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
| `chunk_document` | 549-616 | 入口：参数校验（含 `semantic_threshold`/`semantic_min_tokens`）→ 无有效节时造 `Body` 节 → 逐节切、全局递增 `chunk_index`；`embed_fn=None` 即长度模式；**T7.3** 新增 `on_degrade`（`:559`）并透传（`:613`） |

元数据与调度

| 函数 | 行号 | 作用 |
|---|---|---|
| `extract_metadata` | `metadata_service.py:340-371` | 层 2 入口，返回 `{title,abstract,year,authors,arxiv_id,doi}` |
| `detect_title` | `metadata_service.py:120-174` | 字号优先（前 40 行中 ≥ 最大字号 `*0.92` 的行拼接）：`:135`、`:141-142` |
| `detect_authors` / `_names_from_line` / `_plausible_author` | `metadata_service.py:189`、`:233`、`:260` | 标题行之后的作者块；无分隔符时按两词一组切分 |
| `detect_abstract` | `metadata_service.py:271-292` | `Abstract` 起，遇 `_SECTION_MARKER`/空行结束；扫描前 2 页 |
| `detect_year` / `_year_from_arxiv_id` | `metadata_service.py:311`、`:300` | arXiv 优先；否则首页 2 页最高频年份（同频取大年份 `:327`） |
| `detect_doi` | `metadata_service.py:331-337` | 首页首个 DOI 形态 token，去尾 `.,;` |
| `heuristic_claim_values` / `embedded_claim_values` | `metadata_service.py:377`、`:405` | 转成合并引擎吃的 `{provenance field: value}` |
| `_backfill_metadata` | `tasks.py:864-927` | 层 1 → 层 2 顺序写 claim |
| `_placeholder_title` / `_reset_placeholder_title` / `_restore_placeholder_title` | `tasks.py:832`、`:851`、`:867` | 文件名占位标题的清除与兜底 |
| `classify_failure` | `errors.py:116-173` | 异常 → `error_code` |

## 3. 数据结构（表/字段/索引，或内存结构）

内存结构（均为 `slots=True` dataclass）：

| 结构 | 字段 | 备注 |
|---|---|---|
| `PageText` | `page:int`、`text:str` | `page` 1-based，直接进 chunk 的 `page_start` |
| `SizedLine` | `text:str`、`size:float` | 字号用于标题判定 |
| `EmbeddedMetadata` | `title/authors/abstract/doi/arxiv_id/venue/volume/issue/pages/publication_date/year/language/keywords/raw` | 全可空；`raw={info, xmp, issns, isbns}`（`pdf.py:512-517`） |
| `Section` | `title`、`page_start`、`page_end`、`number`、`paragraphs:[(page, text)]` | `label` 是 `number + title` |
| `Chunk` | `chunk_index`、`text`、`page_start`、`page_end`、`section`、`section_title`、`token_count`、`char_count`、`is_overlap`、`_spans` | `_spans` 仅供 `_finalize` 推页码；`is_overlap` 恒 `False` 且无消费方 |

落库映射（`tasks.py:962-994`；模型 `app/db/models.py:376-415`）：

| `Chunk` 字段 | `paper_chunks` 列 | 约束/索引 |
|---|---|---|
| `chunk_index` | `chunk_index` | `uq_paper_chunks_paper_index`（唯一 `paper_id+chunk_index`） |
| `page_start`/`page_end` | 同名列 | `ck_paper_chunks_page_range`（`page_end >= page_start`） |
| `section` | `section` | `ix_paper_chunks_paper_section(paper_id, section)` |
| `section_title` | `subsection`（仅当 `section_title != section` 才写，`:985-987`） | 长度 255 |
| `text`/`token_count`/`char_count` | 同名列 | `text` NOT NULL |
| — | `embedding_model`/`embedding_dimension` | 取 `settings`（`tasks.py:954-955`） |

OpenSearch 文档字段（`tasks.py:1031-1076`）：`chunk_id/paper_id/title/authors/year/venue/doi/arxiv_id/tags/section/section_title/page_start/page_end/chunk_index/text/embedding/embedding_model/embedding_dimension`；其中 `section_title = row.subsection or row.section`（`:1057`）。注意过滤字段是索引时快照，改元数据后需 reindex 才生效（`AGENTS.md` §3.6）。

## 4. 调用链（从入口到落地，逐跳）

```
run_ingestion_job(tasks.py:138) / run_reindex_job(:118)
└─ _process_job(:210) → _run_pipeline(...)(:443)
   ├─ _advance_stage(STAGE_PARSING, 45.0)               tasks.py:479（常量 :55/:61）
   ├─ object_storage.download_bytes(object_key)          tasks.py:479
   ├─ extract_pages(data)                                pdf.py:93
   │  └─ PdfReader → 逐页 _page_text(pdf.py:52) → normalize_page_text(pdf.py:64)
   ├─ merge_short_sections(detect_sections(pages))       tasks.py:479
   │  └─ detect_sections(structure.py:132) → _match_heading(:94) → _append_paragraphs(:189)
   ├─ (可选) _resolve_target_paper(...)                  tasks.py:479（非主版本就地结束 :485）
   ├─ _reset_placeholder_title(...)                      tasks.py:479 → :843
   ├─ _backfill_metadata(session, paper, pages, data)    tasks.py:480 → :869
   │  ├─ extract_embedded_metadata(pdf_bytes)            pdf.py:404 → _xmp_packet(pdf.py:278)
   │  ├─ embedded_claim_values(...)                      metadata_service.py:405 → merge_values(:897, confidence 1.0)
   │  ├─ extract_metadata(pages, url, pdf_bytes)         metadata_service.py:340
   │  │  └─ extract_sized_lines(pdf.py:530) → detect_title(:120) → detect_authors(:189)
   │  │     → detect_abstract(:271) → detect_year(:311) → arxiv_id_from_url(:51)/_from_text(:64) → detect_doi(:331)
   │  └─ heuristic_claim_values(...)                     metadata_service.py:377 → merge_values(:922, confidence 0.5)
   ├─ _restore_placeholder_title(...)                    tasks.py:481 → :859
   ├─ _upgrade_fingerprint(..., discard_on_conflict=dedupe) tasks.py:487 → :539
   ├─ degradation_service.Recorder(...)                  tasks.py:475（T7.3 降级 sink）
   ├─ _advance_stage(STAGE_CHUNKING, 60.0)               tasks.py:512
   ├─ chunk_document(..., embed_fn=None|embed_texts,
   │                 on_degrade=degradations)            tasks.py:522-528 → chunking.py:549
   │  └─ 逐节 _chunk_section(:447) → _semantic_pieces(:278) → _report_fallback(:261)
   │     → _split_oversized_piece(:331)/_overlap_suffix(:368)/_finalize(:382)
   │     chunks 为空 → raise IngestionError("parsing produced no chunks")  tasks.py:530
   ├─ _replace_chunks(...)                               tasks.py:532 → :962
   ├─ degradations.resolve(STAGE_CHUNKING)               tasks.py:535（本次没报的降级就此作废）
   └─ embed_texts(:539) → _write_embeddings(:545) → _index_rows(:551) → bulk_index_chunks(:552) → _mark_indexed(:557)
```

失败落库：任何异常由 `_record_failure`（`tasks.py:1091-1110`）在**新事务里** `classify_failure` → `mark_failed(code=...)`，并把 `paper.status` 置 `FAILED`。

## 5. 不变量与踩过的坑

1. **页码 1-based，chunk 页码是区内 span 的 min/max**（`chunking.py:342-344`）；空白页不产生 span（`_section_pieces` 只收非空段落，`:74-77`）。
2. **chunk 绝不跨章节**：每节独立调 `_chunk_section`，`next_index=len(chunks)`（`chunking.py:512`），故 `chunk_index` 文档内全局递增有序。
3. **硬上限 `MAX_TOKENS=450`**：`window_chars = max(1, min(target_chars, max_chars - overlap_chars))`（`chunking.py:378`），给 overlap 预留空间，否则超长段落永远带不上重叠。`tests/test_parsing.py:171`、`:215` 断言 `token_count <= MAX_TOKENS`。
4. **参数校验**（`chunking.py:481-488`）：`target_tokens>0`、`overlap_tokens>=0` 且 `< target_tokens`、`max_tokens>=target_tokens`，否则 `ValueError`。
5. **控制字符必须删**：PostgreSQL `text` 不接受 NUL，历史上整批 arXiv 作业因此失败；其余 C0 会污染发往 OpenSearch 的 JSON（`pdf.py:23-25`、`:72-75`）。
6. **标题误判两处**：(a) running header——同一行在后续 3 行内复现即判页眉并入 `suppressed`，之后**同文本行被整篇跳过**（`structure.py:149-155`），正文里重复的短行也会被吞；(b) 全大写行当章节（`structure.py:120-121`，≤8 词、不以 `.` 结尾），`TABLE I ...` 这类表标题会被误判。
8. **段落不跨页**：`pending` 每页开头重置（`structure.py:146`），跨页段落被切成两段，后一段记到后一页页码。
9. **占位标题**：新 ingest 先用文件名当标题，那不是 claim，所以解析前 `_reset_placeholder_title` 清空（仅当无 `title` claim 且当前标题等于占位名，`tasks.py:846-851`），解析后 `_restore_placeholder_title` 兜底为占位名或 `"untitled"`（`title` NOT NULL，`:859-866`）。
10. **文本层缺失不报错**：`extract_pages` 返回全空白页 → `detect_sections` 出空 → `chunk_document` 返回 `[]` → `tasks.py:506` 抛 `IngestionError("parsing produced no chunks")` → `NO_TEXT_LAYER`（关键字 `errors.py:58-64`，判定 `:188-191`；端到端见 `tests/test_failure_classification.py:200-211`）。
11. **加密**：先试空口令 `reader.decrypt("")`（`pdf.py:109-115`），失败抛 `PdfParseError("encrypted PDF: password required")` → `ENCRYPTED_PDF`（关键字 `("encrypted","password")` 见 `errors.py:57`，分支 `:147-153`）。**损坏**：`PdfReadError` / 其他构造异常 → `PdfParseError("invalid PDF: ...")` → `CORRUPT_PDF`（`errors.py:153`）。
13. **空字节流 → `PdfParseError("empty PDF payload")`**（`pdf.py:100-101`）。它**不会**命中 `UNSUPPORTED_TYPE`：`_KEYWORDS_UNSUPPORTED` 里的字面量是 `"empty payload"`（`errors.py:70`），`"empty PDF payload"` 不包含它，故走 `CORRUPT_PDF` 分支。此条为按代码字符串匹配的推断，未见测试固定 —— 标**未确认**。
14. **`chunk_document` docstring 与实现不符**：docstring 说"`sections` 可以不完整，剩余文本按 `Body` 切"（`chunking.py:470-471`），但代码只在 `sections` 为空或全无段落时才造 `Body`（`:251-259`）。传入部分覆盖的 `sections` 会丢文本；流水线里 `detect_sections` 覆盖全文，现网不触发 —— 隐性契约，标**未确认（无测试固定）**。
15. **`token_count` 是字符估算**：`len(text)//4`（`chunking.py:78-82`），假设 4 字符/token。CJK 约 1 字/token，同一 `MAX_TOKENS=450` 对中文论文实际更松，而索引 `title/text/section_title` 用的正是 `cjk` 分词器（`AGENTS.md` §3.5/§3.6）。代码里没有 CJK 专用估算 —— 标**未确认（无 CJK 长度测试）**。
16. **没有客户端截断**：`embedding_service.embed_texts` 把 chunk 全文原样发服务端（`app/services/embedding_service.py:77-99`，payload 只有 `texts`/`model`），512 token 上限由服务端 + `MAX_TOKENS` 估算共同兜住。
17. **降级必须留痕（T7.3）**：语义模式嵌入失败不再只写日志 —— 每节经 `_report_fallback`（`chunking.py:261-275`）调 `on_degrade("chunking", "semantic_fallback", {section, sentences, error})`，由 `degradation_service.Recorder`（`tasks.py:475-478`）落 `paper_degradations`；`Recorder` 吞掉记账自身的异常，**记账永远不会让作业失败**（`degradation_service.py:247-283`，单测 `tests/test_degradations.py`）。
18. **降级行随本次运行自动作废**：chunks 落库后立刻 `degradations.resolve(STAGE_CHUNKING)`（`tasks.py:535`），本次没报的 code 被盖上 `resolved_at`；因此 `scripts/reindex.py --degraded` 选出来的永远是「现在仍然降级」的论文（真机验证见 `docs/progress/project.md` §21.7）。
19. **未启用的 sink 不改变行为**：`on_degrade=None` 时 `_report_fallback` 立即返回，单元测试与既有调用方（`tests/test_chunking_semantic.py`）不加参数即保持原语义。
20. **解析阶段的降级同样走这个 sink**：`parser_service.degradation_codes()` 把 `degraded_reason` 文本映射成 `docling_unavailable`/`formulas_as_text`/`table_structure_lost`/`reading_order_unverified`，兜底 `parse_degraded`（`parser_service.py:113-190`，`parse_pdf(on_degrade=...)`）。**流水线接上 `parse_paper_file` 是 T8 的事**，本文档只管接口。

## 6. 配置项（键 → 默认值 → 作用 → 出处文件:行）

**切块参数不在配置系统里**：目标/重叠/上限是模块常量，`tasks.py:496` 用 `chunk_document(pages, sections)` 默认值，**没有环境变量能改**。

| 常量 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `MAX_TOKENS` | 450 | 单 chunk 硬上限（估算 token） | `app/parsing/chunking.py:40` |
| `DEFAULT_TARGET_TOKENS` | 400 | 目标长度 | `chunking.py:41` |
| `DEFAULT_OVERLAP_TOKENS` | 48 | 重叠窗口 | `chunking.py:42` |
| `CHARS_PER_TOKEN` | 4 | 字符→token 估算系数 | `chunking.py:39` |
| `MIN_CHUNK_CHARS` | 1 | 小于此长度不出 chunk | `chunking.py:44` |
| `SEMANTIC_*` / `DEGRADE_*` | 见 §2 | 语义分块与降级词表（T7.2 / T7.3） | `chunking.py:57-75` |
| `merge_short_sections(target_chars=)` | 1200 | 小节合并阈值（字符） | `structure.py:211`；调用处 `tasks.py:479` 用默认 |
| 标题三阈值 | 14 词 / 110 字符 / 回看 3 行 | 标题与页眉判定 | `structure.py:57-59` |
| `detect_abstract(max_pages=)` | 2 | 摘要扫描页数 | `metadata_service.py:271`；调用 `:362` 用默认 |
| `detect_year` 范围 | 前 2 页、最高频（同频取大年份） | 年份启发式 | `metadata_service.py:323-327` |

环境变量（`app/core/config.py`）：

| 键 | 默认值 | 作用 | 出处 |
|---|---|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | 写进 chunk 与索引文档 | `config.py:72`；使用 `tasks.py:954`、`:1050` |
| `EMBEDDING_DIMENSION` | 1024 | 向量维度校验 + 落库 | `config.py:73`；`embedding_service.py:67-74` |
| `EMBEDDING_BATCH_SIZE` | 32 | 单请求文本数 | `config.py:74`；`embedding_service.py:98-99` |
| `EMBEDDING_URL` | `http://localhost:8090` | 服务地址（`/embed`） | `config.py:71`；`embedding_service.py:21` |
| `EMBEDDING_TIMEOUT` / `EMBEDDING_MAX_RETRIES` | **300.0** / 2 | 超时与重试（T7.3 由 120 上调，须大于服务端排队时间） | `config.py:75-80` |
| `OPENSEARCH_INDEX` / `OPENSEARCH_ALIAS` | `paper_chunks_v1` / `paper_chunks_current` | chunk 文档落点（实际索引见 `AGENTS.md` §3.5） | `config.py:56-57` |
| `INGEST_MAX_FILE_MB` | 100 | 上游大小闸门（超限在解析之前就失败） | `config.py:119`；`ingestion_service.py:490` |
| `INGEST_CONCURRENCY` | 2 | 同时在跑的流水线条数 | `config.py:125` |
| `CHUNK_MODE` | `length` | 切块边界策略（`length` / `semantic`，T7.2） | `config.py:205`；`chunking.py:47-49`；`tasks.py:512-528` |
| `CHUNK_SEMANTIC_THRESHOLD` | 0.80 | 语义模式：判定低谷的余弦阈值 | `config.py:210`；`chunking.py:57` |
| `CHUNK_SEMANTIC_MIN_TOKENS` | 200 | 语义模式：低谷处允许 flush 的最小块长 | `config.py:215`；`chunking.py:64` |
| `PARSER_BACKEND` | `pypdf` | `pypdf` / `docling`（T8 前保持 `pypdf`） | `config.py:179`；`parser_service.py` |
| `PARSER_CACHE` | true | 解析产物是否缓存到 MinIO（T7.1） | `config.py:185`；`parser_service.py` |

**文档与代码不一致（以代码为准；2026-09-22 已对齐 AGENTS.md 措辞）**：`AGENTS.md` §3.7 原写"模型 `intfloat/multilingual-e5-large`""batch 默认 16""`CHUNK_TARGET_TOKENS≈400`"；代码里 `EMBEDDING_MODEL` 默认 `BAAI/bge-m3`（`config.py:72`）、batch 默认 32（`config.py:74`）、`CHUNK_TARGET_TOKENS` 这个键**不存在**（只有常量 `DEFAULT_TARGET_TOKENS=400`）。差异原因是文档写的是**部署值**（根 `.env`：e5-large / 16），代码写的是**默认值**；现已改成"部署值 X / 代码默认 Y"的写法。512 token 上限在本仓代码中仍无断言或校验，出处只有模型本身 —— 标**未确认**。

## 7. 测试位置与覆盖

`tests/test_parsing.py`（287 行，26 用例）

| 用例行号 | 覆盖 |
|---|---|
| `:35-51` | `normalize_page_text`：单换行保留、空行压成 `\n\n`、CRLF 归一 |
| `:55-127` | 章节识别：编号+已知标题顺序、子标题、表格行排除、小写数学片段排除、页码区间覆盖全文、无标题回落 `Body`、重复页眉不成分节、空页入参 |
| `:136-249` | 切块：不跨节、页码与源页一致、token 目标与硬上限、长节重叠、无缝隙覆盖、超长段落字符窗、无 sections 时回落 `Body`、空输入、空白页忽略、非法参数 |
| `:250-287` | `estimate_tokens` 与规格一致、`Chunk` 默认值（含 `is_overlap is False`）、`Section.label`、控制字符剔除、行结构保留 |

`tests/test_pdf_embedded.py`（212 行，10 用例；用 `PdfWriter` 现场造 PDF）

| 用例行号 | 覆盖 |
|---|---|
| `:54` / `:76` / `:112` | Info 字典字段；XMP 逐字段（`dc:*`/`prism:*`/`pdf:Keywords`）与 `raw["xmp"]` 快照；XMP 覆盖 Info |
| `:139` / `:152` / `:162` | 内嵌文本中的 arXiv id；无元数据 → 空对象而非异常；损坏输入永不抛异常 |
| `:169` / `:179` / `:193` / `:212` | `as_dict` 只留真值；ISSN/ISBN 落 `raw`；只有 `startingPage` 时 `pages` 退化（`pdf.py:486`）；模块导出公开 helper |

相邻但直接相关的测试：

- `tests/test_failure_classification.py:190-211`：空白 PDF → 全 `is_blank` → `chunk_document == []` → `NO_TEXT_LAYER`（含 "OCR" 字样）；`tests/test_job_progress.py:178`：monkeypatch `chunk_document` 验证 `PARSING`/`CHUNKING` 阶段与进度顺序。
- `tests/test_degradations.py`（35 例，**T7.3**）：账本读写（幂等 upsert / 计数 / resolve / 复发重开 / stage 词表校验 / 级联删除）、`Recorder` 的 sink 语义与「记账失败不影响作业」、`chunk_document` 的 `on_degrade` 契约（长度模式不报、失败才报、无 sink 照常切块）、`parser_service.degradation_codes` 映射、`GET /api/papers/{id}/degradations`、`scripts/reindex.py --degraded` 的筛选。
- `tests/test_job_progress.py`（新增 1 例，**T7.3**）：真跑一次 `_run_pipeline`（打桩外部依赖）→ 降级行带 `job_id` 落库，再跑一次干净的 → 该行 `resolved_at` 被盖上。

## 8. 未做 / 已知缺口

| 缺口 | 依据 |
|---|---|
| 无 OCR / 无版面分析（分栏、表格、公式） | `errors.py:54-55` 仅提示；`pdf.py:55` 只走 `extract_text()` 默认顺序 |
| 全大写短行规则会误报章节 | `structure.py:120-121`（`TABLE I` 等） |
| 死代码：`_ROMAN_TAIL`、`Chunk.is_overlap`（恒 `False`、无消费方） | `structure.py:26`；`chunking.py:200`、`:398`，全仓无其它引用 |
| `_spans` 不进 `paper_chunks`/OpenSearch | 仅 `_finalize` 用来算页码 |
| 部分覆盖的 `sections` 会丢文本 | `chunking.py:494-502` 与该函数 docstring（`:516-517`）矛盾 |
| CJK token 估算偏差 + 512 上限无代码侧校验 | `chunking.py:78-82` 固定 4 字符/token；512 只在 `AGENTS.md` §3.7，`embedding_service.py` 无截断 |
| 句内语义断点仍可能落在超长段的字符窗里 | `chunking.py:293-322` 对超 `target_chars` 的 `_Piece` 只能按字符窗硬切（长度模式必然如此） |
| DOI/arXiv 规范化不在本模块 | `pdf.py:496`、`metadata_service.py:331` 原样返回，规范化在标识符层 |
| XMP 只认 `dc`/`prism`/`xmp` 三命名空间 | `pdf.py:141-145`，其余 ns 元素被跳过（`:305-306`） |
| **按阶段重跑尚未提供（T7.3 决策：暂不做）** —— 现在只能整篇 reindex（PARSING→INDEXING 全跑）；想要的「只补 embedding / 只补索引」需要复用 `_write_embeddings`/`_index_rows`/`_mark_indexed` 写一个 `--stage` 入口。数据模型已经支持断点：`paper_chunks.embedded_at`/`indexed_at` 可空，`GET /api/consistency` 能报出 `missing_index` | 留档见 `docs/progress/project.md` §21.7「后续优化方向」；`scripts/reindex.py` 目前只有 `--missing` / `--degraded` |
| 语义模式把同一段文字嵌两遍（句子一遍、chunk 一遍，无复用） | `tasks.py:528` 与 :539 打同一个 `EMBEDDING_URL`；留档见 `docs/architecture/04-embedding.md` §8 缺口 11 |
| 未确认项 | 空 payload 的 `error_code` 归类（§5.13）、CJK 长度上限（§5.15）；模型名/batch/`CHUNK_TARGET_TOKENS` 的文档口径已于 2026-09-22 对齐（§6） |
