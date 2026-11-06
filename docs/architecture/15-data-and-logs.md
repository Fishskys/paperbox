# 数据与日志

系统管理中的数据与日志仅向管理员开放。运行状态沿用 `/health` 的依赖探测与缓存策略；进程内存和系统日志通过独立管理员接口读取。页面和日志导出都不触发清理。

## 内存采集

`GET /api/admin/observability/memory` 返回采样时间、后端当前进程和六项依赖的内存测量。Linux 使用 `/proc`，本地容器通过 Podman 的 JSON 进程列表或本地 Docker 引擎的容器 PID 定位，汇总容器主进程及其子进程。采集不使用 shell，不提升权限、不连接远程容器引擎、不自动挂载容器套接字。

RSS 是驻留物理内存，进程合计可能重复计算共享页；仅当所有已采集进程均可读取 `smaps_rollup` 时返回 PSS。权限不足、进程退出和远程依赖返回明确的不可用或部分采集状态，不以零代替。embedding 与 rerank 可共享同一容器，数值不能再次相加当作总内存。

`SYSTEM_CONTAINER_RUNTIME` 可选 `auto`、`podman`、`docker`、`disabled`；`SYSTEM_CONTAINER_NAMES` 用 JSON 映射依赖名到本地容器名，默认与基础设施 compose 一致。每条容器命令受 `SYSTEM_METRICS_TIMEOUT_S` 限制。API 部署在无法访问宿主机进程或容器运行时的容器中时，依赖内存会显示不可用。

## 日志查询与导出

`GET /api/admin/observability/logs` 返回最近日志中的筛选结果；`GET /api/admin/observability/logs/export` 导出同一筛选条件下整个扫描窗口内的 JSON 报告，而非仅当前页。两者均需要 admin，支持：

| 参数 | 语义 |
| --- | --- |
| `level` | 精确级别：DEBUG、INFO、WARNING、ERROR、CRITICAL |
| `logger` | 模块名包含匹配，不区分大小写 |
| `q` | 消息、模块、请求 ID 和身份标识的关键词匹配，不区分大小写 |
| `request_id` | 请求 ID 精确匹配 |
| `since`、`until` | 包含边界的起止时间，必须带时区，开始不能晚于结束 |
| `offset`、`limit` | 分页位置与每页数量，默认 50、最多 200；导出忽略分页 |

日志来源固定为 `LOG_FILE` 及其编号轮转文件，必须位于 `logs/app/`。禁止用户指定路径；跳过符号链接与非普通文件。未配置文件日志时返回不可用原因。文本记录和 JSON 记录均可读取，异常堆栈归属前一条记录；结果按时间倒序排列。

`SYSTEM_LOG_SCAN_BYTES` 默认 8 MiB、最多 64 MiB，所有轮转文件共享一个最近字节预算。`total` 仅代表该窗口内命中的数量；`truncated` 表示窗口外仍有内容或文件不可读取，筛选和导出都明确展示该限制。翻页及导出重新读取文件，新增或轮转期间不承诺固定快照。

日志返回前屏蔽配置中的密钥、密码、连接字符串凭证及 Bearer、常见 token 字段。日志浏览不能代替日志写入端避免记录敏感数据的要求。导出保留扫描范围、截断标记、采样时间及完整多行消息。
