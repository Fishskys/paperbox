# 升级指引（0.5.x → 0.7.0）

本文件面向**已有部署的升级**。全新部署见 [`README.md`](README.md) 与
[`UserManual.md` §1](UserManual.md#1-详细部署教程)；本文只讲“从旧版本换到 0.7.0
需要多做/改写什么”。

> **为什么单独写这份**：0.5.2 → 0.7.0 之间有几处**会让人踩坑的破坏性变更**（API key
> 标识改 `prefix`、吊销即删除的迁移、`.env` 索引名仍钉在旧代），以及一个**只在旧索引上
> 才会触发的对账器 bug**（0.7.0 已修）。这些分散在各版本 CHANGELOG 里，按升级顺序集中
> 成一份，照着做即可。

---

## 0. 一页速查（最短路径）

```bash
# 1) 备份（schema 迁移不可逆）
docker exec paperbox-postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup-$(date +%F).sql

# 2) 停服（迁移期间别让旧代码碰新 schema）
#    停掉 uvicorn / 应用容器即可，依赖容器（pg/opensearch/minio/embedding）不用停

# 3) 换新代码，装依赖，迁移
uv sync
uv run alembic upgrade head

# 4) 关键：核对/修正 .env
#    - OPENSEARCH_INDEX 若要升到新映射，先看第 3 节（不要盲目改成 v3）
#    - PAPER_API_KEYS 的条目必须是 prefix:key（见第 2 节），否则引导钥匙不生效

# 5) 起服务，逐项自检
uv run python scripts/healthcheck.py          # 六依赖 + 应用 + 索引 + 检索管道
uv run python scripts/manage_keys.py list      # 钥匙还在、prefix 未变即正常
uv run python scripts/check_consistency.py     # 三端只读对账，problems=0 最好（尤其看 errors）
```

**务必按顺序**：`0)` 备份 → `1)` 停服 → `2)` 迁移 → `3)` 起服务。代码与 schema 必须
一起换：旧代码 + 新库、新代码 + 旧库都会报错（前者选列失败、后者插入缺列）。

---

## 1. 数据库迁移（`alembic upgrade head`）

0.5.2 → 0.7.0 之间有 3 个迁移，其中一个是**破坏性**的，不能跳过：

| 迁移 | 内容 | 影响 |
|---|---|---|
| `abc44f6` → 迁移 `9c1d4b7a2e01` | 删除 `api_keys.name`、`prefix` 上限 16 → 32 | 见第 2 节（key 标识改为 prefix） |
| `23ff052` → 迁移 `8b4f0c2d6a17` | **吊销即删除**：去掉 `revoked_at` 列 | ⚠️ 迁移会**先删除所有 `revoked_at IS NOT NULL` 的行**，否则旧版“已吊销但留档”的钥匙会在升级后**复活可认证**。升级日志会打印 `api_keys: removed N retired key(s) …`，看到它属正常 |
| `407581f` | 修上面那个迁移“会复活旧钥匙”的缺陷 | — |

> 若你的库是从很老版本一路升上来，迁移链会自动补齐中间缺失项；先 `alembic current`
> 看一眼起点，再 `alembic upgrade head`。

---

## 2. 破坏性变更：API key 标识改为 `prefix`（0.5.3 起）

- **一把钥匙只有一个标识符：`prefix`**（`api_keys.name` 已删）。日志归因（`[key=<prefix>]`）
  与 MCP 审计（`meta.agent`）都用它。
- **`.env` 的 `PAPER_API_KEYS` 条目格式变为 `prefix:key`**（旧格式 `名字:key` 里的“名字”
  本来就是标识，现在直接当 `prefix`）。例：

  ```ini
  # 旧（<=0.5.1）
  PAPER_API_KEYS=hermes:<key1>;codex:<key2>
  # 新（>=0.5.3）—— 左半就是 prefix，别改右半的 key
  PAPER_API_KEYS=hermes:<key1>;codex:<key2>
  ```

  形状没变，但语义变了：左半会被当作该钥匙的 `prefix` 写入 `api_keys` 表。**既有钥匙的
  `prefix` 值不变**，客户端无需改动；只要你原来的“名字”就是想要的 `prefix`，`.env` 不用动。
- `scripts/manage_keys.py`：`create` **去掉了 `--name`**（只填 `--prefix`）；对已存在的
  `prefix` 再 `create` 会先展示库里那行并询问是否**替换**（同事务删旧行 + 签发新钥匙，
  旧密钥立即失效）；非交互调用默认拒绝，需 `--yes`。
- `revoke` 改为**硬删除**该行（不可恢复；钥匙真正退役的动作是“重发”）。

**升级后核对**：`uv run python scripts/manage_keys.py list` —— 钥匙还在、`prefix` 未变即正常。

---

## 3. 检索索引：别让你的部署“停在旧一代映射”上

这是**本次最容易忽略、也最要命**的一节。0.7.0 修复的三端对账器 bug 只在**旧索引**上才会
触发，而升级代码**不会**自动重建索引。

### 3.1 现象与根因

- 旧索引（例如 `paper_chunks_v2`，`dynamic: true` 时代建的）里，`parser_backend` /
  `embedding_model` 被动态映射成了 **`text`**（附一个 `.keyword` 子字段）。
- 旧版对账器 `GET /api/consistency` / `scripts/check_consistency.py` 直接对**裸字段**做
  `terms` 聚合，`text` 不可聚合 → OpenSearch 返回 **400**（“Text fields are not optimised
  for … aggregations … use a keyword field instead”）。
- 旧版把这次失败**吞成“索引侧为空”**，于是**每一篇论文都被判成 `missing_index`**，
  `documents_os=0`，满屏假漂移。
- 0.7.0 已修：按**实际 mapping** 选可聚合字段（`keyword` 用裸字段，`text` 用
  `.keyword` 子字段），并且**读失败不再当空**（记为 `errors`、跳过索引侧比较，不伪造
  `missing_index`）。所以 0.7.0 的对账器在旧索引上也能跑出**正确结论**。

### 3.2 升级后怎么做（二选一或都做）

- **只要对账结论正确**：直接升到 0.7.0 即可，对账器已能兼容旧索引，`errors` 应为空、
  `problems` 反映真实情况。
- **想彻底摆脱旧映射（推荐）**：重建到当前映射（`keyword` 字段 + `dynamic: "strict"`）。
  当前代码默认 `OPENSEARCH_INDEX=paper_chunks_v3`，但**你的 `.env` 若显式钉着旧名
  （`paper_chunks_v2`），升级不会改它**——这是关键：升级后先确认
  `grep OPENSEARCH_INDEX .env`，别让应用继续指着旧索引。

### 3.3 重建到新映射的操作与**前置风险**

```bash
# 0) 先把 .env 的 OPENSEARCH_INDEX 改成目标名（例如 paper_chunks_v3），否则
#    create_index.py 无参会去建 .env 里那个旧名
# 1) 预演：把旧索引复制到一个临时索引，验证在 strict 映射下不会有“未声明字段”
uv run python scripts/create_index.py --index paper_chunks_v3_tmp --migrate-from paper_chunks_v2
#    这一步会：比对 embedding 维度（不同则拒绝，需整库重嵌，不能 copy）→
#    服务端 _reindex 拷贝文档（不重算向量）→ 文档数必须逐一对上，否则非零退出且不动别名
# 2) 确认临时索引文档数正确后，正式切到 paper_chunks_v3（同样的命令，目标名换成正式名）
uv run python scripts/create_index.py --index paper_chunks_v3 --migrate-from paper_chunks_v2
#    旧索引默认保留，可回滚；确认无误后再手动 drop
```

⚠️ **strict 重索引的一个坑**：旧索引是 `dynamic: true`，可能自动长出了当前 `build_mapping()`
里**没声明**的字段。把文档复制进 `dynamic: "strict"` 的 `paper_chunks_v3` 会因“未声明字段”
直接报 400。所以**第 1 步的临时索引预演必须通过**再切正式名；若报未声明字段，先把它补进
`app/search/mappings.py` 的 `build_mapping()` 再重试。

> 若 embedding 维度/模型也变了，`--migrate-from` 会拒绝 copy（向量不同维不可贴），此时
> 必须走整库重嵌（`POST /api/papers/reindex`，小时级），不是本文档范畴。

---

## 4. 其它会感知到的变化

| 版本 | 变化 | 你要做什么 |
|---|---|---|
| 0.5.2 | 读接口默认免鉴权（`AUTH_READ_ACCESS=public`），写接口仍要 `write`/`admin` | 如需读也要凭证，设 `AUTH_READ_ACCESS=key` |
| 0.5.2 | 退役签名下载路由 `GET /api/downloads/{id}`，改走 `GET /api/papers/{id}/file` | 有客户端在用旧路由的需改 |
| 0.5.2 | MCP `paper_get_file` 返回不带凭证的直链，不再发签名短链 | — |
| 0.6.0 | `/health` 增 `degraded` / `dependencies`；依赖探针“只按需”（无定时器）；应用容器**不再声明 compose healthcheck** | 健康看 `/health` 或 `scripts/healthcheck.py`，别再靠 `docker ps` 的 healthy 字样 |
| 0.6.0 | `PARSE_CACHE_VERSION` 2→3 | 旧解析缓存失效，老论文首次会重新解析一遍（无害，慢一次） |
| 0.6.1 | 修精排 ORT 线程（容器线程 41→34） | 若用仓库自带 embedding/rerank 镜像，**重建镜像**才生效 |
| 0.6.2 | docling 改官方镜像 `quay.io/docling-project/docling-serve-cpu:v1.35.0` | 若用自带 docling 镜像：换仓库 compose 或改镜像地址；公式/代码识别需另挂 CodeFormulaV2 |
| 0.6.2 | 删除 `infra/docling/` 构建目录；compose 头部不再引用不入库的 `docker-compose.local.yml` | 以仓库 compose 内注释为准 |
| 0.6.2 | `POST /api/search` 默认排除参考文献（`filters.include_references`，默认 `false`） | 引文类查询需要时置 `true` |
| 0.7.0 | 三端对账器按 mapping 选可聚合字段、读失败不再当空（本次修复） | 见第 3 节；升级后 `check_consistency.py` 的 `errors` 应为空 |

---

## 5. 升级后自检清单

- [ ] `alembic upgrade head` 退出码 0；日志若有 `api_keys: removed N retired key(s) …` 属正常。
- [ ] `manage_keys.py list`：钥匙还在、`prefix` 未变。
- [ ] `grep OPENSEARCH_INDEX .env`：确认它指向你**期望**的那一代索引（别停在旧名）。
- [ ] `healthcheck.py`：六依赖 + 应用 + 索引 + 检索管道全绿（`degraded=false`；`docling`
      报 `disabled` 只说明 `DOCLING_URL` 为空，不算降级）。
- [ ] `check_consistency.py`：**先看 `errors` 是否为空**；再看 `problems`。
      - 旧索引 + 0.7.0：`errors` 应为空、`problems` 为真实值（不再是满屏 `missing_index`）。
      - 若 `errors` 里有 `opensearch: …`，说明索引根本没读通（地址/权限/索引名），先解决它，
        此时报告里的 `documents_os` 不代表真实文档数为 0。
- [ ] 抽查一次检索与一次上传导入，确认写链路正常。

---

## 6. 回滚

- **代码/schema**：迁移不可逆（尤其 `8b4f0c2d6a17` 删了 `revoked_at`），回滚靠 `0)` 的备份
  还原数据库 + 换回旧代码。
- **索引**：`create_index.py` 切别名时**旧索引默认保留**，把别名切回旧索引即可回滚。
