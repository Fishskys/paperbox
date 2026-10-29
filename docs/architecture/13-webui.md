# WebUI

## 运行边界

`frontend/` 是与 API 独立运行的 React + TypeScript + Vite 应用，采用 Ant Design 组件与 Lucide 图标。
开发时 Vite 提供页面及 `/api/*`、`/health` 同源代理，不新增 Python BFF，不调用 Python service 模块。
API 单独启动，前端进程不初始化数据库、后台队列或 MCP；停止前端不影响已提交的后端任务。

## 当前页面

左侧导航、右侧工作区，支持折叠侧栏、小屏抽屉及浅色/深色/跟随系统外观。
运行状态页调用 `/health`，仅在进入或手动刷新时探测，原样区分正常、异常、禁用和未知状态。

论文检索包括论文搜索和本机检索记录，查询、模式及过滤条件保存在 URL。
搜索结果按论文列出证据，只有接口提供真实页码时才定位原文；返回搜索可恢复查询和滚动位置。
阅读器从搜索、论文库或最近阅读进入时，返回操作使用原浏览器历史，保留前进能力，不追加重复来源页。
论文阅读包括论文库、最近阅读和 PDF 详情；论文库由 `/api/papers` 服务端分页，表格上方和下方均提供同步页码与输入跳转，保留当前筛选。
PDF 阅读器使用 React-PDF / PDF.js，按原文尺寸纵向连续排列页面，仅渲染视口附近的页面画布。
滚动同步当前页码、URL 与最近阅读位置，页码输入和前后页按钮滚动定位；支持文本选择、缩放、适应宽度、详情和原文下载。
PDF worker、CMap、字体及 WASM 随本机构建产物提供，不依赖 CDN。
检索历史最多 50 条、最近阅读最多 30 条，均仅存在浏览器本地，不代表服务端个人账户记录。

论文导入包含新建导入、元数据导入与导入任务：支持多 PDF 选择/拖放、链接提交、两路并发上传及 429 退避。
元数据导入路由为 `/imports/metadata`，支持 JSON / JSONL / NDJSON 文件，沿用后端 IEEE / CSL / 通用格式识别。
页面通过 `POST /api/metadata/import` 先预览再正式导入，使用 `import_file` 来源，显示匹配、待补原文题录、歧义、失败与冲突报告。
更换文件会清除旧预览，未预览不能正式导入；请求最长等待两分钟，不自动重试写入，网络结果不明时要求先检查论文库或重新预览。
元数据接口同步返回报告，不产生导入队列任务；报告只存在当前页面，字段冲突裁决与来源人工归属不在此页面提供。
PDF 上传自动尝试最多三次，只重试明确拒绝的 429；网络结果不明时提示先检查任务，避免盲目重复提交。
上传队列由工作区持有，应用内切换页面继续传输；刷新或关闭在途页面会触发浏览器离开提醒，不承诺浏览器外后台上传。
“停止待传”只停止等待项与后续重试，已发送请求继续收取结果，已接收的后台任务不取消。
任务列表保留真实阶段和进度；仅在页面可见且存在进行中任务时轮询，失败后保留最后一次报告并允许手动重试。
当前 `JobOut` 不包含类型字段，界面不根据阶段猜测导入、重建或图片任务类型。
系统管理还提供手动只读数据检查，报告含 `errors` 时标记结果不完整，不自动修复数据。

## 凭证与错误

连接设置中的 API key 只保留在当前页面内存中，刷新后失效；通过 `Authorization: Bearer` 请求头发送。
本机开发也可在被 Git 忽略的 `frontend/.env.local` 设置 `PAPERBOX_API_KEY`，由 Vite 开发/预览代理在服务端携带，无需在浏览器中输入。
自动携带仅限本机回环连接、回环 Host 和同源请求；跨源请求或远程客户端不使用本地密钥。浏览器显式提供的凭证优先，错误凭证不会被替换。
该配置只适用于受信任的本机开发，不进入浏览器资源或 Nginx 镜像；独立部署与远程访问仍通过连接设置提供凭证。
密钥不得放入 URL、`VITE_*` 环境变量或浏览器持久化存储。外观选择单独存入本地存储。
请求层集中处理网络故障、401/403 及 `Retry-After`，不会把接口失败渲染为空列表。
无 JSON 响应的 5xx 视为代理或连接故障，显示 API 不可达，不因代理仍在线而显示后端正常。
代理目标配置为 `PAPERBOX_API_TARGET`，默认 `http://127.0.0.1:8077`，此配置不含凭证。

## 开发与检查

在 `frontend/` 执行：

```bash
npm ci
npm run dev
npm run typecheck
npm run lint
npm test
npm run build
npx playwright install chromium
npm run test:e2e
npx playwright test --config playwright.production.config.ts
```

Node.js 要求 `20.19+`（20 系列）、`22.13+`（22 系列）或 `24+`，依赖固定于 `package-lock.json`。
开发默认端口 5173；端口已占用时显式传入 `npm run dev -- --port 5175`。
`PAPERBOX_API_TARGET` 可由进程环境或前端 `.env.local` 配置；API 启停方式保持独立。
`vite preview` 只用于本地检查构建产物，不用于正式部署。
浏览器测试使用拦截响应，不写真实论文库；单元测试验证请求与错误契约。
模拟接口测试不能替代已运行 API 的验收。后端代码更新后需要执行数据库迁移并重启 API，Vite 热更新不会更新 Python 进程。
在仓库根执行 `node scripts/check_webui_metadata.mjs`，可用真实浏览器将
`tests/metadata/ieee_metadata_example.jsonl` 和对应 JSON 文件提交给本地 API 做预览，检查记录数、解析失败及论文总数不变。
该脚本只执行预览，不点击正式导入；默认访问 5173，可通过 `WEBUI_URL` 指定其他地址，需要该页面已具备写权限凭证。

## 独立部署

根 compose 中 `webui` 属于可选 profile，没有 `depends_on`，不会自动启动或停止 API：

```bash
docker compose --profile webui up -d --build webui
docker compose --profile webui stop webui
```

默认访问 `http://localhost:8089`。根 `.env` 的 `WEBUI_PORT` 控制端口，
`WEBUI_API_UPSTREAM` 默认 `http://api:8077`，适合 API 与 WebUI 同在该 compose 网络。
API 运行在宿主机时改成 `http://host.docker.internal:8077`。
前端镜像采用 Node 构建、Nginx 运行，运行阶段不需要 Python、Node 或论文存储依赖。
API 镜像构建上下文排除 `frontend/`，不会携带前端依赖或构建产物。
Nginx 提供 SPA 深层路径回退、静态资源缓存和 API 反向代理，保留客户端 Authorization。
API 域名在请求时解析，因此 API 尚未启动时页面仍可打开，对应业务请求返回网关错误。
默认 DNS 读取容器 `/etc/resolv.conf`；特殊网络可用容器环境变量 `PAPERBOX_DNS_RESOLVER` 覆盖。
反向代理的单请求上传上限为 200 MB，实际接收仍受 paperbox 自身的文件大小与队列限制。

在仓库根执行 `node scripts/check_webui.mjs` 可对已运行服务执行只读浏览器验收，
默认地址 `http://127.0.0.1:5173`，可通过 `WEBUI_URL` 覆盖；用于匿名可读的部署，需要库中已有可读 PDF。
截图写入 `logs/codex/`，覆盖桌面、小屏和真实 PDF 画布；该脚本不创建或修改后端数据。
