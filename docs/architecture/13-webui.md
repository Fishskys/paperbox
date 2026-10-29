# WebUI

## 运行边界

`frontend/` 是与 API 独立运行的 React + TypeScript + Vite 应用，采用 Ant Design 组件与 Lucide 图标。
开发时 Vite 提供页面及 `/api/*`、`/health` 同源代理，不新增 Python BFF，不调用 Python service 模块。
API 单独启动，前端进程不初始化数据库、后台队列或 MCP；停止前端不影响已提交的后端任务。

## 当前页面

左侧导航、右侧工作区，支持折叠侧栏、小屏抽屉及浅色/深色/跟随系统外观。
运行状态页调用 `/health`，仅在进入或手动刷新时探测，原样区分正常、异常、禁用和未知状态。

## 凭证与错误

连接设置中的 API key 只保留在当前页面内存中，刷新后失效；通过 `Authorization: Bearer` 请求头发送。
不得放入 URL、`VITE_*` 环境变量或浏览器持久化存储。外观选择单独存入本地存储。
请求层集中处理网络故障、401/403 及 `Retry-After`，不会把接口失败渲染为空列表。
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
```

Node.js 要求 `20.19+`（20 系列）、`22.13+`（22 系列）或 `24+`，依赖固定于 `package-lock.json`。
开发默认端口 5173；端口已占用时显式传入 `npm run dev -- --port 5175`。
`PAPERBOX_API_TARGET` 可由进程环境或前端 `.env.local` 配置；API 启停方式保持独立。
`vite preview` 只用于本地检查构建产物，不用于正式部署。
浏览器测试使用拦截响应，不写真实论文库；单元测试验证请求与错误契约。
