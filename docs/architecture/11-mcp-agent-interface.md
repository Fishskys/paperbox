# 11. MCP Agent Interface（工具契约真源）

> **本文是 MCP 模块的唯一契约真源。** 工具增删、参数语义、返回形状、权限与传输安全都以本文为准；
> plan（`.hermes/plans/2026-10-04_175312-mcp-agent-interface.md`）记录的是决策过程与任务分解，实现细节若有冲突以本文为准。
> 前身是 `hermes-integration.md`（2026-09 起草的 5 个只读工具）——那份文档的工具定义已并入本文 §5，原文件只保留"为什么这样调用"的范式说明。

**端点**：`http://<host>:8077/mcp`（Streamable HTTP；带不带尾斜杠都可以 —— 服务端内部会把 `/mcp`
改写为挂载点根路径，**不会**回 307 让客户端重来一次；2026-10-04 真机验收发现并修，见 §2 末尾）
**状态**：v1（工具集冻结，见 §8）　**客户端验收**：Hermes（其余三家给配置片段，标"待验证"）

> **实现进度（2026-10-04，二次更新）**：**6 个读工具全部落地**（`paper_search` / `paper_get` /
> `paper_get_chunks` / `paper_get_context` / `paper_get_file` / `paper_job_status`），
> 写工具 4 个未做。传输层、显式 `TransportSecuritySettings`（无默认、空白名单即失败）、stateless HTTP、
> 宿主机 lifespan 托管 session manager 均已验收。
> **真机验收（30 篇语料）**：`paper_search` 真实排序（int8 交叉编码器在位，3.2s）→ 9 条 citation
> **全部带页码与章节**；`paper_get` 报 15 块 + 7 个字段的来源账本；分页游标不重叠；`paper_get_context`
> 窗口 5 块且恰好 1 块 `primary`；**签名下载 200 / 2.2 MB 真 PDF / 篡改签名 403**；
> REST 与 MCP 同查询 **total 与排序逐位相同**（不变式 2）。
> 落地位置：`app/services/chunk_service.py`（块读取唯一来源，REST `/chunks` 也用）、
> `app/services/search_pipeline.py`（`POST /api/search` 全流程，MCP 复用）、
> `app/services/download_signing.py` + `app/api/downloads.py`（签名链接与流式下载）、`app/mcp/citations.py`。
> 待做：鉴权（T-A3）、写工具（T-A6）、错误码细化（T-A7）、SSRF 闸（T-A11）、验收脚本（T-A13）、
> Hermes 真机任务式验收（T-A14）、四客户端配置片段（T-A15）。

---

## 1. 它是什么（边界）

paperbox 对 agent 暴露的是一个**论文知识服务**：用自然语言查、读带结构化引用的正文、必要时把新论文收进来。
它**不**暴露基础设施细节（OpenSearch DSL、向量维度、桶名、库表结构），也**不**承担 agent 的推理职责。

| 维度 | 定义 |
|---|---|
| 谁用 | 人背后的 agent：Hermes（唯一做真机验收）、codex、claude code、deepseek harness |
| 当什么用 | **检索 + 阅读**（主线）；**导入 + 维护**（副线，默认关闭） |
| 不当什么用 | 文档管理系统（不做版本/批注）；不做 RAG 的"自动拼上下文"（agent 侧的事）；不做 LLM 调用 |
| 交互单位 | 论文（paper）与块（chunk）；**每次结果都带结构化引用**（`paper_id` + `page` + `section` + `chunk_id`） |
| 与 REST 的关系 | MCP 是**新增的一层壳**：直连 `app/services/*`，不新增检索/聚合/元数据规则；同一操作经 REST 或 MCP 结果与错误语义一致 |

### 1.1 权限矩阵

| 能力 | 工具 | 默认 | 开关 | 可逆性 |
|---|---|---|---|---|
| 检索 | `paper_search` | ✅ 开 | — | 只读 |
| 读元数据 | `paper_get` | ✅ 开 | — | 只读 |
| 读正文（按块分页） | `paper_get_chunks` | ✅ 开 | — | 只读 |
| 读正文（围绕命中块扩展） | `paper_get_context` | ✅ 开 | — | 只读 |
| 取原件（短期签名链接） | `paper_get_file` | ✅ 开 | — | 只读 |
| 查作业进度 | `paper_job_status` | ✅ 开 | — | 只读 |
| 导入（URL / 服务端目录） | `paper_import` | ⛔ 关 | `MCP_WRITE_ENABLED` | 可逆（可删） |
| 重建切块与向量 | `paper_reindex` | ⛔ 关 | `MCP_ALLOW_REINDEX` | 计算昂贵，数据可重建 |
| 删除论文 | `paper_delete` | ⛔ 关 | `MCP_ALLOW_DELETE` | **软删**（可追溯） |
| 改元数据 | `paper_update_metadata` | ⛔ 关 | `MCP_ALLOW_METADATA_WRITE` | 可回滚（字段级账本） |

- 三个细分开关**都受总闸 `MCP_WRITE_ENABLED` 约束**；总闸关 = 写工具全部不可见。
- **关掉的工具不出现在 `tools/list`** —— agent 不该看到一个它不能用工具（不是"能看见但报错"）。
- **key 不是权限系统**：`key → agent 名` 只用于身份与审计（§4.3），权限一律由上面的全局开关决定。

---

## 2. 传输与部署契约

| 项 | 契约 |
|---|---|
| 端点 | `/mcp`（Streamable HTTP），与 REST **同进程、同端口、同鉴权** |
| 传输选项 | `stateless_http=True`（每次请求独立 transport，无会话状态）、`json_response=True`（单个 JSON 响应，不用 SSE 流）、`streamable_http_path="/"`（挂载前缀即完整路径） |
| 挂载 | `app.mount("/mcp", mcp_app)`，放在 REST 路由**之后** |
| **会话生命周期** | **父应用（FastAPI）的 lifespan 必须 `async with mcp.session_manager.run()`** —— 被 Mount 的子应用 lifespan **不会执行**；漏了会"启动成功、首个请求才失败" |
| **传输安全** | `streamable_http_app(transport_security=TransportSecuritySettings(...))` **必传**；**禁止依赖 SDK 默认的 Host 行为** |
| Host 白名单 | `MCP_ALLOWED_HOSTS`（**无默认值**）：必须列出 agent 访问用的 LAN IP 与主机名（`host` 与 `host:*` 各一条）。白名单**外**一律 **421 Misdirected Request** |
| 白名单缺失 | `MCP_ENABLED=true` 且白名单为空 → **启动报错退出**（不许静默只认 localhost） |
| 启动日志 | 打印**生效白名单**，运维一眼能对 |
| 鉴权 | **仅** `Authorization: Bearer <key>`（无 query-string key） |
| 失败码 | 白名单外 = **421**（纯文本响应，**不是** JSON-RPC 错误；客户端会报泛化 transport error，排查看服务端日志） |
| 开关 | `MCP_ENABLED`（默认 `false`）：关掉时 `/mcp` 404，REST 完全不受影响 |

> **为什么把这两条写成"必须"**：SDK 的默认值只接受 `127.0.0.1` / `localhost` / `[::1]` 的 Host。
> 局域网共享场景下，不显式配白名单的结果是"服务看着正常、agent 一个请求都进不来"，而 421 的提示信息极不显眼。

---

## 3. 统一返回契约

### 3.1 成功信封（所有工具一致）

```jsonc
{
  "data":      { /* 工具各自的结构化结果 */ },
  "meta":      { "tool": "paper_search", "agent": "hermes", "toolset": "v1", "took_ms": 5481 },
  "warnings":  ["truncated: 8000 chars", "degradation: reading_order_unverified"],
  "citations": [ /* Citation[]，无引用时为空数组 */ ]
}
```

- MCP 侧同时提供：`structuredContent` = 上面这个对象；**以及**一段人类可读的文本摘要。
- `warnings[]` 承载"结果被截断 / 该篇有解析降级 / 参数被夹紧"这类提示；**空数组而非缺字段**。

### 3.2 `Citation`（一等公民）

| 字段 | 类型 | 说明 |
|---|---|---|
| `paper_id` | string | 论文 id |
| `title` | string | 论文标题 |
| `page` | int \| null | 页码（来自索引，用于"该结论见第 7 页"） |
| `section` | string \| null | 章节（优先 `section_title`） |
| `chunk_id` | string | 块 id（可回查上下文） |
| `quote` | string \| null | 该块前 ≤200 字符的原文片段；**不参与排序** |

`paper_search`、`paper_get_context`、`paper_get_chunks` **必须**填充 `citations[]`。

### 3.3 错误信封（`isError: true`）

```jsonc
{
  "error": {
    "code": "NOT_FOUND | UNAUTHORIZED | FORBIDDEN | INVALID_ARGUMENT | CONFLICT |
             DUPLICATE_FINGERPRINT | NO_TEXT_LAYER | ENCRYPTED_PDF | CORRUPT_PDF | DOWNLOAD_FAILED |
             OVERSIZED | UNSUPPORTED_TYPE | PARSE_BACKEND_UNAVAILABLE | PARSE_FAILED |
             EMBEDDING_FAILED | INDEX_FAILED | STORAGE_FAILED | INTERRUPTED | INTERNAL |
             SERVICE_UNAVAILABLE | WRITE_DISABLED | SSRF_BLOCKED | TIMEOUT",
    "message": "人类可读一句话",
    "retryable": false,
    "retry_after": 2,
    "hint": "agent 下一步该做什么（可选）"
  }
}
```

- `code` **复用现有语义**：14 个作业 `error_code`（`app/core/errors.py`）+ 8 个解析降级码 + HTTP 状态含义。
  **本模块只新增两个码**：`WRITE_DISABLED`（权限关闭，`hint` 指向对应环境变量名）、`SSRF_BLOCKED`（入站 URL 被安全闸拦下）。
- **实际送达形态（实现事实，2026-10-04 验证）**：MCP SDK 的失败通道只有文本 —— 工具抛
  `mcp.server.mcpserver.exceptions.ToolError` 时，客户端拿到 `isError: true` 且
  `content[0].text = "Error executing tool <name>: {\"error\": {...}}"`（SDK 加前缀，JSON 由我们提供，可解析）；
  抛**其它**异常会被 SDK 当崩溃，客户端只见 `Error executing tool <name>`（无细节）、服务端 ERROR + traceback。
  所以工具层**一律**抛 `app.mcp.errors.ToolFailure`（它继承 SDK 的 `ToolError`），审计日志里同时记 `code`。
- `retryable=true` **必须**给 `retry_after`（取自服务端 `Retry-After` 或推理队列语义）。
- `INVALID_ARGUMENT` 的典型：`max_chars` 超上限（**报错不裁剪**，message 给出允许的最大值）、非法过滤器取值。

---

## 4. 鉴权、身份与审计

### 4.1 鉴权

| 项 | 契约 |
|---|---|
| 唯一路径 | `Authorization: Bearer <key>` |
| query-string key | **不支持**（v1 明确不做：key 进 URL 会落进代理/网关日志与浏览器历史） |
| 凭证来源 | `PAPER_API_KEYS`（多 key）→ 命中则得 agent 名；否则回落到 `PAPER_API_KEY`（agent 名 = `default`）。`MCP_ENABLED=true` 而两者都空 → **启动报错**（不许静默全 401）|
| 失败 | 缺凭证 → 401（带 `WWW-Authenticate: Bearer`）；凭证不匹配 → 403（与 REST 现有语义一致） |
| 实现 | `app/mcp/auth.py` + `app/main.py` 的 `McpAuthMiddleware`（**纯 ASGI**，不是 BaseHTTPMiddleware —— 端点会流式返回，缓冲型中间件会破坏它）。**不用 SDK 自带的 `AuthSettings`/`TokenVerifier`**：那套是 OAuth 资源服务器形态（强制 `issuer_url`/`resource_server_url`、宣告 RFC 9728 发现端点），静态 key 部署下客户端会去走一个永远走不通的 OAuth 流程 |
| 与 421 的**顺序** | 鉴权在传输之前：**未鉴权的请求一律 401，即使 Host 不在白名单**（不让没通过鉴权的调用方探测本机接受哪些 Host 名）；白名单检查对**已鉴权**的请求照常生效（421，硬要求 2 的反面测试保留）。两种顺序都有测试钉住 |
| 身份传播 | 中间件把 `AgentIdentity` 放进 contextvar，工具层用它填 `Envelope.meta.agent` 与审计行 `agent`（真机验证：`"agent": "hermes"`）|

### 4.2 多 key 格式

```
PAPER_API_KEYS=hermes:<key1>;codex:<key2>;claude-code:<key3>
```

`;` 分隔；每项 `名字:key`；名字限 `[A-Za-z0-9_-]`；key 内不得含 `:` 或 `;`。
同一 key 同时出现在 `PAPER_API_KEYS` 与 `PAPER_API_KEY` 时，以 `PAPER_API_KEYS` 的名字为准并记 WARNING。

### 4.3 审计

每次工具调用写一条结构化日志（**不落库**）：

```jsonc
{"event":"mcp_call","agent":"hermes","tool":"paper_delete","paper_id":"...",
 "args_digest":{"dry_run":false},"outcome":"ok","code":null,
 "affected":{"chunks":37,"objects":2},"took_ms":412,"transport":"http"}
```

- 脱敏：不记 key、不记正文全文（只记字符数/块数）；`query` 只记前 200 字符。
- 检索类工具仍落 `search_queries`（现有表）；写工具**必须**记 `affected`。

---

## 5. 工具契约 v1（10 个）

> 输入/输出全部是 **Pydantic 模型**（`app/mcp/models.py` + 复用 `app/schemas/search.py::SearchFilters`），
> `tools/list` 的 JSON Schema 由类型自动生成：**不允许裸 `object`**（单测快照钉住）。
> 每个工具的 description 必须写：何时该用 / 何时不该用 / 一个可复制的参数示例。

### 5.1 读工具（默认可用）

#### `paper_search`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `query` | string | — | **必填**，自由文本（中英文） |
| `mode` | enum | `hybrid` | `keyword` / `semantic` / `hybrid` |
| `top_k` | int | `10` | 1–50 |
| `filters` | `SearchFilters` | 无 | 见 §5.3 |
| `rerank` | bool | **`true`** | 交叉编码器重排。**代价**：`top_k=10` 时 p50 ≈5.5 s（关掉 ≈1.3 s）——快速扫一遍再决定时可传 `false` |
| `facets` | bool | `false` | 同时返回各过滤键在当前条件下的可选值与论文数（agent 摸底用） |
| `backend` | string | 跟随部署 | `native`（默认）/ `python`，仅影响 `hybrid` |

**返回** `data`：`results[]`（每篇含 `paper_id`/`title`/`authors`/`year`/`venue`/`score`/`relevance`/`evidence[]`）、
`total`（命中论文数真值）、`candidates`（喂进聚合的块数）、`rerank{...}`、`rewrite{...}`、`facets`。
**`citations[]`**：逐篇 evidence 展开。

#### `paper_get`

参数：`paper_id`（必填）。
返回 `data`：论文元数据（`arxiv_id`/`doi`/`venue`/`venue_year`/`paper_type`/`authors`/`year`…）+ 字段来源账本摘要 +
`degradations[]`（该篇的解析降级记录；有降级时**同时**进 `warnings[]`）。

#### `paper_get_chunks`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `paper_id` | string | — | **必填** |
| `offset` | int | `0` | 从第几块开始（分页游标） |
| `limit` | int | `10` | 每页块数 |
| `max_chars` | int | `8000` | 本次返回正文上限；**> `MCP_MAX_CHARS_CEILING` 直接报 `INVALID_ARGUMENT`** |

返回 `data`：`chunks[]`（`chunk_id`/`chunk_index`/`page`/`page_end`/`section`/`subsection`/`text`/`chars`/`token_count`/`primary`/`truncated`）
+ `total`/`returned`/`offset`/`limit` + `truncated` + `next_offset`。
**`next_offset` 是"接着读的游标"**：超预算被截断时给出，论文还有未读块时也给出（`None` = 这篇你已看到底）。

#### `paper_get_context`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `chunk_id` | string | — | **必填**（通常来自检索命中的 evidence） |
| `before` / `after` | int | `1` / `1` | 取前后各几块（同篇内按顺序） |
| `max_chars` | int | `8000` | 同上 |

返回 `data`：`chunks[]`（含目标块，按顺序）+ `paper_id`；`citations[]` 覆盖这些块。

#### `paper_get_file`

参数：`paper_id`（必填）。
返回 `data`：`download_url`（**短期签名**，默认 300 s）、`expires_at`、`filename`、`bytes`、（可选）`page_count`。
**契约**：URL 不含长期凭据；**原件经应用代理**（不暴露对象存储）；过期或签名被改 → 拒绝。

#### `paper_job_status`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `job_id` | string | — | **必填** |
| `wait_seconds` | int | `0` | >0 时轮询等待至完成或超时 |

返回 `data`：`job_id`/`paper_id`/`stage`/`progress`/`error_code`/`error_message`/时间戳。
阶段：`RECEIVED` → `QUEUED` → `DOWNLOADING` → `STORED` → `PARSING` → `CHUNKING` → `EMBEDDING` → `INDEXING` → `COMPLETED`；失败为 `FAILED`。
超时**不算失败**：返回当前 `stage` + `hint`。

### 5.2 写工具（默认不可见）

| 工具 | 参数 | 默认 | 行为 |
|---|---|---|---|
| `paper_import` | `source`（必填）、`source_type`=`url`\|`local_path`、`wait_seconds`=`120`、`dry_run`=`false` | — | 建导入作业；**短暂等待**：完成返回 `paper_id`，超时返回 `job_id`。`url` **必过 SSRF 闸**（§6） |
| `paper_reindex` | `paper_id`（必填）、**`dry_run`=`true`**、`wait_seconds`=`120` | — | `dry_run=true` 只返回影响面预览（现有块数、是否已有作业在跑）**不执行** |
| `paper_delete` | `paper_id`（必填）、**`dry_run`=`true`** | — | `dry_run=true` 返回影响面（论文、块数、对象数）；显式 `false` 才执行（**软删**） |
| `paper_update_metadata` | `paper_id`（必填）、`fields: MetadataPatch`（必填）、`dry_run`=`false` | — | 改元数据，返回"字段旧值→新值"；回滚走 REST |

- 写工具**只支持 URL 与服务端路径**两种导入源：MCP 不传文件字节（要传文件走 REST `/api/papers/ingest/files`）。
- `local_path` 指的是**服务端上的一个 PDF 文件路径**（不是目录 —— 目录批量导入走 REST `/ingest/dir`，
  它有逐文件报告和自己的 `dry_run`），必须落在 `INGEST_LOCAL_ROOTS` 白名单内（默认空 = 该能力关闭）。
- **开关决定"存不存在"**：`MCP_WRITE_ENABLED` 关 → 四个写工具**都不注册**；分别打开
  `MCP_ALLOW_DELETE`/`MCP_ALLOW_REINDEX`/`MCP_ALLOW_METADATA_WRITE` 才注册对应工具
  （`paper_import` 只受总开关管）。没注册的工具不出现在 `tools/list`，按名字猜也调不到。
- **危险操作默认是预览**：`paper_delete` / `paper_reindex` 的 `dry_run` 默认 `true`，且
  `dry_run=true` 时**零次**改动型 service 调用（单测用"调用即抛错"的替身钉住）。
- **`paper_import` 的 sha256 去重**：与 REST 同一套流水线，导入一个**内容已存在**的 PDF 会命中已有论文
  （返回那篇的 `paper_id`，不是新建）。⚠️ 因此"导入→删除"式的验收脚本必须先快照语料，
  只删自己新建的那篇（2026-10-04 真踩过：夹具与语料里的 arXiv 1706.03762 逐字节相同，
  一次误删把语料论文删了，靠"重放原件 + 重建索引"复原）。

### 5.3 `filters`（复用 `SearchFilters`）

`year_from` / `year_to` / `authors[]` / `venue[]` / `venue_year[]` / `paper_type[]` / `doi` / `arxiv_id` /
`identifier[]`（`scheme:value`，未知 scheme → `INVALID_ARGUMENT`）/ `tag[]` /
`ieee_terms[]` / `author_terms[]` / `dynamic_index_terms[]` / `source_tags[]`

### 5.4 典型调用链

```
paper_search(query="低功耗 SRAM 漏电", mode="hybrid")          # rerank 默认开
  → citations[0] = {paper_id, page:7, section:"III-B", chunk_id}
     agent 可直接写："该结论见第 7 页 III-B 节"
  → 想读上下文：paper_get_context(chunk_id=...)
  → 想通读：paper_get_chunks(paper_id=..., offset=0, limit=10)（翻页）
  → 想核原文：paper_get_file(paper_id=...) 拿短期下载链接
```

---

## 6. 入站 URL 的安全闸（SSRF）

`paper_import(url=...)` 与 REST 的 URL 导入**共用**同一个守卫（`app/services/net_guard.py`）：

| 规则 | 内容 |
|---|---|
| scheme | 只允许 `http` / `https` |
| 主机解析 | 解析出的**每个** A/AAAA 记录都要通过校验（防 DNS 轮换绕过） |
| 默认拒绝 | 回环（`127/8`、`::1`）、私网（`10/8`、`172.16/12`、`192.168/16`、`fc00::/7`）、link-local（`169.254/16`、`fe80::/10`，含云元数据 `169.254.169.254`）、`0.0.0.0`/`::`、多播/广播 |
| 重定向 | **每一跳重新校验** |
| 白名单 | `INGEST_ALLOW_PRIVATE_HOSTS`（默认**空**）：主机名或 CIDR。**名字**条目整体信任（不再校验它解析出的地址 —— 否则名字白名单在 DNS 一变就失效）；**CIDR** 条目只放行地址 |
| 落地 | `app/services/net_guard.py`；`ingestion_service.download_pdf` 用 `net_guard.open_stream`（`follow_redirects=False` 手工逐跳校验）；REST `/ingest` 与 MCP `paper_import` 都在**建作业前**先过闸（失败：REST 400 / MCP `SSRF_BLOCKED`）。重定向到内网、或被 DNS 改成内网，都会在下载那一步再被拦（此时按既有码记 `DOWNLOAD_FAILED`，因为作业错误码是 API 面、不新增） |
| 失败 | `SSRF_BLOCKED`（`retryable=false`，`hint` 指向该白名单变量） |

> ⚠️ 这是对 **REST 既有行为**的加固：加闸之后，"从内网地址导入 PDF"必须显式写白名单。

---

## 7. 不变式（实现必须守，改这块前先读）

1. **不绕过 service 层**：工具直接调 `app/services/*`，不自己实现检索/聚合/元数据规则。
2. **REST 与 MCP 契约等价**：同一操作经两条路径结果与错误语义一致。
3. **只读工具永远可用**：写开关全关时，agent 仍能完整完成"检索 + 阅读 + 引用"。
4. **任何写操作都被审计**：日志能回答"哪个 agent、何时、对哪篇、做了什么、影响面多大"。
5. **不泄漏凭据**：审计与返回体不出现 key、不出现 embedding；下载链接不含长期凭据。
6. **内容预算恒定**：单次正文有上限（`MCP_MAX_CHARS`，默认 8000），超出**显式标注**并给翻页；**超上限的显式请求报错不裁剪**。
7. **无会话状态**：每次调用从请求取身份与参数；服务端不依赖 MCP 会话。
8. **零数据库迁移**：本模块不改 schema。
9. **入站 URL 一律过 SSRF 闸**（§6）。
10. **类型即契约**：输入/输出都是 Pydantic 模型，`tools/list` 的 schema 自动生成并进单测快照。
11. **传输安全必须显式**：`transport_security` 必传；启动日志打印生效白名单。
12. **白名单缺失即失败**：`MCP_ENABLED=true` 且 `MCP_ALLOWED_HOSTS` 空 → 启动报错退出。

---

## 8. 版本与变更规约

- `MCP_TOOLSET=v1`（默认）：工具集与参数形状**冻结**；`tools/list` 每个工具带 `_meta.toolset="v1"`。
- **加工具/改参数** = 改版本号（`v2`）并在文档里记录迁移；**破坏性变更必须重跑两个任务式验收**（§9）。
- 工具下线：先在 `v1` 标 deprecated，再在 `v2` 移除。

---

## 9. 验收（Hermes）

两个任务式验收（无人工提示）：

1. "**找出 3 篇关于 X 的论文，带页码引用**"。
2. "**读取指定论文的 Method 部分，概括关键步骤并给出对应页码**"。

> **验收记录（2026-10-04，Hermes 真机，两个任务都通过）**：证据与原始日志见
> `docs/examine/mcp-hermes-acceptance-20261004/`（含调用链与答复摘录）。任务①用「面向代码的大语言模型」
> （库里 ≥3 篇）通过；任务②用《Attention Is All You Need》的 Method 部分通过（§3.1 p.3 / §3.2.1 p.4 /
> §3.3 p.5 / §3.5 p.6，逐节带页码）。五个会话日志里 `curl` / `/api` 兜底命中数**均为 0**。
> **语料覆盖的诚实结论**：前三次尝试的主题（低功耗 SRAM 漏电 / III-V 环栅纳米线 / LLM 模拟电路设计）
> 库里只有 0 / 1 / 2 篇，Hermes 如实说明「不足 3 篇」并列举非匹配候选、没有编造——所以「3 篇」是
> **语料规模问题**而非接口问题。

两段都要能看到：工具调用链（`paper_search` → `paper_get` → `paper_get_chunks`/`paper_get_context`）、
带页码/章节的引用、`citations[]` 被真正使用；且**没有**出现 agent 退回 curl/REST 兜底。

---

## 10. 客户端接入

> **状态口径**：只有 **Hermes 做了真机任务式验收**（§9）。其余客户端的片段按各自 CLI 的**实际形态**写成，
> 状态逐条标注；「配置已就绪」≠「端到端已验证」。

### Hermes（✅ 端到端验收通过）

```bash
# 落点：~/.hermes/config.yaml（用 CLI 写，别手改 YAML）
hermes config set mcp_servers.paperbox.url "http://<paperbox 的 LAN IP>:8077/mcp"
hermes config set mcp_servers.paperbox.headers.Authorization "Bearer <该 agent 的 key>"
hermes config set mcp_servers.paperbox.timeout 300          # ≥ MCP_WAIT_SECONDS
hermes config set mcp_servers.paperbox.connect_timeout 30
hermes mcp test paperbox                                    # 期望：Connected + 列出工具
```

工具在 Hermes 里是 `mcp__paperbox__<tool>`（例：`mcp__paperbox__paper_search`）。
**改完必须重启 Hermes**（无热加载；网关 host 的会话重启后会自动恢复）。

### codex CLI（🟡 配置已就绪，端到端未验证）

```bash
# token 从环境变量读，不进配置文件
export PAPERBOX_MCP_TOKEN="<该 agent 的 key>"     # Windows 持久化：setx PAPERBOX_MCP_TOKEN ...
codex mcp add paperbox --url http://<IP>:8077/mcp --bearer-token-env-var PAPERBOX_MCP_TOKEN
codex mcp list      # 期望：paperbox / streamable_http / enabled / Bearer token
codex mcp get paperbox
```

本机实测（2026-10-04，codex-cli 0.153.4）：`codex mcp add` 成功、`codex mcp get paperbox` 显示
`transport: streamable_http` + `bearer_token_env_var: PAPERBOX_MCP_TOKEN` + `enabled: true` ✓。
**端到端调用仍未能验证**，卡在 codex 自己的模型后端上（**与 paperbox 无关**，两次原因依次是）：
① CC Switch 代理 `127.0.0.1:15721` 没在监听 → `502 Bad Gateway`；
② 代理起起来后，**逐个模型探测全部 `HTTP 402 INSUFFICIENT_BALANCE`（TokenRhythm 余额不足）**
（fl 探的 `deepseek-flash`/`deepseek-chat`/`deepseek-v3`/`kimi-k2`/`glm-4.6`/`gpt-5`/`claude-sonnet-4-5` 无一例外）。
⇒ codex 连模型请求都发不出去，谈不上调 MCP。**额度或 key 在 CC Switch 侧解决后按上面的片段即可用**
（注意：同一台机器上 Hermes 跑 `TokenRhythm/deepseek-flash` 是正常的 —— 两边很可能不是同一把 key/账号）。

### Claude Code（⚪ 未验证，本机未安装）

```bash
# CLI 方式（HTTP 传输 + 自定义头）
claude mcp add --transport http paperbox http://<IP>:8077/mcp \
  --header "Authorization: Bearer <该 agent 的 key>"
```

```jsonc
// 或项目内 .mcp.json（可提交给协作者，但别把 key 提交上去）
{ "mcpServers": { "paperbox": {
    "type": "http", "url": "http://<IP>:8077/mcp",
    "headers": { "Authorization": "Bearer <key>" } } } }
```

本机**没有安装** Claude Code，上述片段按官方 MCP 配置形态书写但**未经真机验证**；
若连接失败，先按 §2（421 = Host 不在白名单）与 §4（401/403 = key 问题）自查。

### 其他 MCP 客户端 / 自研 harness（⚪ 未验证）

任何支持 **Streamable HTTP** 的 MCP 客户端都行，只要满足两点：请求带 `Authorization: Bearer <key>`，
且它的 `Host` 在 `MCP_ALLOWED_HOSTS` 里。最小握手（不需要任何 SDK）：

```bash
curl -sS -X POST "http://<IP>:8077/mcp" \
  -H "Authorization: Bearer <key>" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":
       {"protocolVersion":"2025-06-18","capabilities":{},
        "clientInfo":{"name":"my-harness","version":"0"}}}'
```

Python 侧直接 `pip install mcp` 后用 `mcp.client.streamable_http.streamablehttp_client(url, headers=...)`
建会话；这条路径与协议本身**未在本项目做真机验证**（Hermes 用的是它自带的客户端）。

### 排障速查（跨客户端通用）

| 现象 | 原因 | 处理 |
|---|---|---|
| **421 Misdirected Request**（裸文本 `Invalid Host header`） | 客户端的 `Host` 不在 `MCP_ALLOWED_HOSTS` | 把**该客户端访问用的 IP/主机名**（`host` 与 `host:*` 两种写法）加进 `MCP_ALLOWED_HOSTS` 并重启 |
| **401** 且带 `WWW-Authenticate: Bearer` | 没带 `Authorization` 头（或缺 key） | 客户端配置里补 header；`?key=` 不支持 |
| **403** | key 不匹配 | 对齐 `PAPER_API_KEYS` / `PAPER_API_KEY`；确认没多空格 |
| 服务端启动就报 `MCP_ALLOWED_HOSTS is empty` / `needs a credential` | `MCP_ENABLED=true` 但白名单空或没有任何 key | 补 `MCP_ALLOWED_HOSTS` 与 `PAPER_API_KEYS`（或 `PAPER_API_KEY`） |
| 工具列表里少了写工具 | `MCP_WRITE_ENABLED` 关 | 要写能力就开总闸 + 对应分开关并重启 |
| 现象是"连不上"而不是明确报错 | 客户端没装 MCP 支持（例：Hermes 缺 `mcp` 包会被静默禁用） | 装依赖、重启，再 `hermes mcp test paperbox` |
| 超时 | 客户端 per-tool timeout < `MCP_WAIT_SECONDS` | 把客户端 timeout 提到 ≥ 等待上限（Hermes 用 `timeout: 300`） |
| `localhost` 连不上 | Windows 先解析到 IPv6 回环 | `url` 一律写 IP |

---

## 11. 排障速查

| 现象 | 最可能的原因 |
|---|---|
| 启不来，日志说白名单为空 | `MCP_ENABLED=true` 但 `MCP_ALLOWED_HOSTS` 没配（§2） |
| 启动正常，但所有请求 **421** | 客户端用的 Host 不在白名单里（加 IP **和** 主机名，`host` 与 `host:*` 各一条） |
| 连接被拒但看不出原因 | 421 是纯文本、不是 JSON-RPC 错误；看**服务端**日志里那一行 host 警告 |
| 首个请求必失败、后续正常 | 父应用 lifespan 没进 `mcp.session_manager.run()` |
| 长导入报超时 | 客户端 `timeout` < `MCP_WAIT_SECONDS`；或用 `paper_job_status` 续查 |
| 看不到写工具 | 写开关默认全关（`MCP_WRITE_ENABLED` 及三个细分开关） |
