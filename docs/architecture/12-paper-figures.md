# 论文图片

## 范围与质量

有文本层的论文 PDF 使用 PDFFigures2 定位 figure 和原文图注，PyMuPDF 在独立进程中导出区域 PDF、PNG 和缩略图。不做扫描识别，不修改正文、元数据、向量和原始 PDF。Docling 仍为正文主解析器；没有启用 PDFium 或 Docling 图片定位。

自动定位可能漏图、混入正文或裁掉图内元素。图片统一为 `match_status=needs_review`，有候选图片时批次为 `partial` 并带 `FIGURE_REVIEW_REQUIRED`。无候选且无警告为 `completed`，只表示处理结束，不能证明原文没有图片。目前不声明达到计划中的准确率门槛。

`native` 是保留矢量和嵌入图像的 PDF 页面视图裁切，不是脱敏文件；原页隐藏区域内容可能仍在文件内。PNG 默认 300 DPI，根据区域内位图分辨率提高至最多 600 DPI；缩略图最长边默认 480。超过预览像素预算保留原生 PDF 和缩略图，`preview_url=null` 并返回警告。PNG 不能称作原始位图无损导出。

## 接口

以下路径以 `/api/papers/{paper_id}/figures` 为前缀，遵循项目已有读权限策略，修改操作要求 write。OpenAPI 类型定义在 `app/schemas/figure.py`。

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | 空路径 | `limit` 1–200、`offset`；返回 generation、状态、图注、页码、坐标、原生与预览 URL、警告 |
| GET | `/download` | 整篇 ZIP，`variant=native/preview/thumbnail/all`，默认 native；可用 generation_id 固定批次 |
| GET | `/{figure_id}` | 当前批次可见图片详情 |
| GET | `/{figure_id}/file` | `variant=native/preview/thumbnail`，默认 preview；预览和缩略图 `part` 从 1 开始；native 返回整份多页 PDF，忽略 part；`disposition=inline/attachment` |
| POST | `/extract` | 可选 JSON `{"force":false}`；新任务或已有在途任务返回 202；源文件与选项未变的当前结果返回 200；force 创建新批次 |
| DELETE | 空路径 | 隐藏全部图片，取消在途任务并清理所有图片批次；支持 `dry_run=true` |
| DELETE | `/{figure_id}` | 隐藏并清理当前批次单图，取消在途重提取；同一图重复删除幂等；支持 dry_run |

文件返回 MIME、Content-Length、ETag 和 UTF-8 文件名，支持 If-None-Match。每次访问重新检查可见性，包括条件 GET。URL 是相对 API 路径，客户端沿用现有凭据，不暴露 MinIO bucket/key、本机路径。

ZIP 返回 `application/zip` 附件，包含所选格式全部 parts、每幅图的 `caption.txt` 和总清单 `manifest.json`。图片目录带 UUID，重复图号不覆盖。清单保留图号、图注、页码、质量警告和包内文件映射，不暴露对象 key。缺少所选预览时写明警告，已提取但无可见图片时返回仅有清单的 ZIP。尚未提取返回 409 `FIGURES_NOT_EXTRACTED`，其他无当前批次状态返回 409 `FIGURE_ARCHIVE_NOT_READY`。

打包逐个流式读取并核验大小与 SHA256，使用内存上限 8 MiB 的临时缓冲，超过后落临时文件；总包大小受 `FIGURE_MAX_TEMP_BYTES` 限制，超限 413。文件缺失、损坏或存储失败分别返回 404、409、503，不返回半个 ZIP。发送前再次验证批次和删除 revision，变更返回 409。ZIP 不写回 MinIO，下载结束或中断时清理临时文件；使用 no-store，不支持单文件接口的 ETag/304。

列表 `generation_id` 固定分页批次，切换后返回 409 `FIGURE_GENERATION_CHANGED`。状态区分 `not_extracted`、`queued/running`、`completed/partial`、`failed/cancelled`、`deleted`、`stale`；有旧结果时继续提供旧结果及在途 job_id。整批删除后可显式重新提取。编号保留来源值，不拿 ordinal 冒充图号；明确标为 Continued 的相邻续图按 parts 保留多页。

无 PDF 返回 409 `FIGURE_PDF_REQUIRED`；缺少 JAR/Java 返回 503 `FIGURE_BACKEND_UNAVAILABLE`；后台队列未启动返回 503 `FIGURE_QUEUE_UNAVAILABLE`，不在请求内计算。其他错误由 `/api/jobs/{id}` 查询，失败后经 `POST /api/jobs/{id}/retry` 重试，仍走图片 runner。

## 数据与发布

`FigureExtractionRun.id` 同时是 generation id，记录源文件 id/hash、选项指纹、工具版本、状态与清理标记；`PaperFigure` 保存图号、图注和 parts；`PaperFigureAsset` 保存角色、key、大小/hash 和像素信息。`Paper.figure_revision` 是删除与发布的并发闸门。部分唯一索引保证每篇最多一个在途批次和一个当前批次。

```text
papers/<paper_id>/figures/<run_id>/manifest.json
papers/<paper_id>/figures/<run_id>/<figure_id>/figure.pdf
papers/<paper_id>/figures/<run_id>/<figure_id>/part-001-preview.png
papers/<paper_id>/figures/<run_id>/<figure_id>/part-001-thumbnail.png
```

先记录任务，再上传独立批次，全部成功后锁论文，确认 revision、源文件、论文存活和任务状态，最后在一次数据库事务中切换当前批次。失败保留旧批次，未发布对象按精确批次前缀清理。单图删除有持久化 cleanup_pending；在途生产者确认结束后才清理其批次，避免清理后又上传。图片清理不碰原 PDF 和 extracted 正文缓存。

对账器以数据库登记的 current/active/cleanup_pending 批次认领图片前缀，计入 `totals.figure_objects`；未登记批次仍是孤儿。该计数不校验每个图片文件的完整性。论文删除先使 revision 失效并取消提取，再删除对象、软删论文。

## 执行与部署

共用 `IngestQueue`，图片另受 `FIGURE_EXTRACT_CONCURRENCY` 限制，等待图片配额的任务不占住全部导入 worker。原生 PDF/JVM 在独立进程组中运行，超时和取消时杀掉进程组。源文件与输出有字节预算，预览渲染前检查像素预算。cleanup_pending 在任务结束、删除、替换和重启恢复时重试。

默认 `FIGURE_AUTO_EXTRACT=false`，读接口不触发提取；开启后正文索引成功时尝试排队，失败不改变导入成功状态。没有启动时自动全库回填。`uv run python scripts/extract_figures.py <paper_id> ...` 默认 dry-run，`--execute` 才请求服务，`--force` 强制重提取，凭据从 `PAPER_API_KEY` 读取。

配置与默认值见 `.env.example` 的 `PDFFIGURES2_*`、`FIGURE_*`。本机需要 Java 21，执行 `uv run python scripts/build_pdffigures2.py` 构建 JAR；Docker 多阶段构建随附 JAR/JRE。上游固定 commit `3d7ad46753d4a315cccd1c2bcab398380e88c534`，仅升级构建用 Scala/sbt 并移除失效发布插件，未修改提取算法。PDFFigures2 为 Apache-2.0，PyMuPDF 为 AGPL/商业许可，分发时保留依赖许可材料。

升级先备份并停应用，执行 `uv sync --locked` 与 `uv run alembic upgrade head`，再冷启动，不自动重提取存量论文。降级到 `c6e1a4b92f70` 会删除图片账本，应先通过 API 清理图片对象，否则对象失去归属。原 PDF 和正文不受本迁移影响。

`scripts/acceptance_figures.py` 自建临时 PostgreSQL/MinIO 容器，验证迁移往返、真实应用启动、真实 JVM/原生导出、列表/预览/下载/条件请求、重提取/删除/再次提取，退出时删除容器与自造数据。单测仅用 SQLite 和存储替身，验证用户行为及原生 PDF 内容。
