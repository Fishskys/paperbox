# 解析与切块

## 1. 职责边界

默认解析后端是远端 `docling-serve`；本地降级后端是 `pymupdf`。两者交付相同 markdown 方言、页码跨度和降级记录，随后共用切块、向量化和检索流程。降级解析不承诺 Docling 的表格、公式或复杂版式质量，也不执行 OCR。

`app/parsing/pdf.py` 提供元数据读取与页面文本 DTO；`app/services/parser_service.py` 负责后端选择和解析产物缓存；路由、导入任务不各自实现 PDF 读取。

## 2. 本地 PDF 处理

MuPDF 的调用通过 `app/parsing/mupdf.py` 在独立 Python 子进程执行。队列在线程池中运行，不能把 native document/page 共享到这些线程。`PDF_PROCESS_CONCURRENCY` 默认 2，`PDF_PROCESS_TIMEOUT` 默认 120 秒且包含容量等待；超时终止并回收子进程，映射为 `PdfParseError`。空文件、损坏和密码不可用同样遵守领域错误契约。

`extract_pages` 返回从 1 起的 `PageText`；控制字符清理共用 `app/parsing/text.py`。Info 原始键和 XMP 原始包先读取，再经 `embedded_metadata_from_sources` 应用优先级与身份声明规则。自定义 IEEE 标识不能因为只读取标准字段而丢失；PDF 创建时间不是发表日期。

## 3. 阅读顺序与层次

`native_layout.prepare_pages` 先在源坐标中清理重复页边内容，再排序完整文字行。跨页重复、位置、字号与页码序列共同决定删除，首页标题与正文中的同名期刊保留。删除字符与保留字符必须和原文本守恒，否则保留原页并记录未确认。

仅当足够多的长正文行支持左右两栏时按左栏、右栏排序。跨栏完整行作为上下区域边界，标题和署名行不会拆成零散 spans。段落保留 native 文本块边界与明显行距；无法可靠处理的旋转文字保留阅读顺序未确认标记。

主标题用 `#`，章节用 `##`，数字小节和 IEEE 字母小节向下分级，最多六级；依据编号、字号和字体提示，不将任意大写图内标签当成标题。`PageText.heading_levels` 和 `PageBlock.level` 传递这些提示。复杂表格、图中文字与公式仍可能干扰顺序，本地路径是可用性降级，不以复杂版式全正确为前提。

作者由 `app/services/pdf_byline.py` 从首页标题到摘要/引言之间提取，按视觉行排序，处理上标、membership、机构与邮箱。无可靠署名时允许空值，不阻塞导入；既有人工与结构化来源的优先级不变。

## 4. 共享 markdown 与切块

页边界为 `<!-- page-break -->`；`page_spans_from_markdown` 计算半开字符区间。`normalize_markdown`、章节反推和 `chunk_document` 在两种后端间共用。表格无法还原时返回结构不可用占位，公式没有 LaTeX 时记入降级账本，不能把纯文本宣称为语义表格或公式。

## 5. 后端与缓存

`PARSER_BACKEND=docling` 为默认，`pymupdf` 选择本地后端。旧值 `pypdf` 是兼容入口，实际新产物记录 `pymupdf` 和其真实版本；历史数据库与索引中的 `pypdf` 来源不改写，仍可查询并按条件重索引。

`parse_pdf` 是纯解析入口，不写数据库或对象存储。`parse_paper_file` 按原 PDF hash、后端、选项和 `PARSE_CACHE_VERSION` 验证缓存；当前缓存版本为 4，旧产物不冒充新算法输出。变更后端只影响新解析与显式重索引，不自动重写全库。Docling 客户端错误和服务不可达分别记录 `docling_rejected`、`docling_unavailable`，本地回退仍保留原错误原因。

## 6. 配置与验证

配置见 [.env.example](../../.env.example) 与 [用户手册](../../UserManual.md)。依赖清单与锁文件由 uv 管理；生产使用 PyMuPDF，pypdf 仅为 dev 测试造 PDF。构建与运行无需本地 Docling Python 模型。

测试覆盖列优先级、跨栏内容、段落和标题层次、页码证据、嵌入标识、损坏/加密 PDF、降级、缓存与历史来源。真实论文验收只读源文件；单测不连接真实 PostgreSQL、OpenSearch、MinIO 或 Docling。
