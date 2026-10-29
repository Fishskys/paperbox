import {
  App as AntApp,
  Button,
  ConfigProvider,
  Drawer,
  Form,
  Input,
  Modal,
  Radio,
  Tooltip,
  theme,
} from "antd";
import zhCN from "antd/locale/zh_CN";
import {
  BookOpen,
  ChevronLeft,
  ChevronRight,
  Circle,
  Menu,
  Settings,
  ShieldCheck,
  Search,
  Library,
  Upload,
} from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import {
  BrowserRouter,
  Link,
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { setCredential, useConnection, useCredentialRevision } from "./api";
import { AccessProvider, RequireAccess, useAccess } from "./access";
import Status from "./pages/Status";
import { SidebarContext } from "./workspace-context";
import { UploadProvider } from "./upload-context";

const SearchPage = lazy(() => import("./pages/Search"));
const LibraryPage = lazy(() => import("./pages/Library"));
const HistoryPage = lazy(() => import("./pages/History"));
const ReaderPage = lazy(() => import("./pages/Reader"));
const ImportsPage = lazy(() => import("./pages/Imports"));
const MetadataImportPage = lazy(() => import("./pages/MetadataImport"));
const JobsPage = lazy(() => import("./pages/Jobs"));
const DataCheckPage = lazy(() => import("./pages/DataCheck"));

const groups = [
  {
    label: "检索",
    capability: "read" as const,
    icon: Search,
    items: [
      { label: "论文搜索", path: "/search" },
      { label: "检索记录", path: "/search/history" },
    ],
  },
  {
    label: "阅读",
    capability: "read" as const,
    icon: Library,
    items: [
      { label: "论文库", path: "/library" },
      { label: "最近阅读", path: "/reading/recent" },
    ],
  },
  {
    label: "导入",
    capability: "write" as const,
    icon: Upload,
    items: [
      { label: "论文导入", path: "/imports/new" },
      { label: "元数据导入", path: "/imports/metadata" },
      { label: "导入任务", path: "/imports/jobs" },
    ],
  },
  {
    label: "系统管理",
    capability: "admin" as const,
    icon: ShieldCheck,
    items: [
      { label: "运行状态", path: "/system/status" },
      { label: "数据检查", path: "/system/data" },
    ],
  },
];

function Workspace() {
  const { context, can } = useAccess();
  const revision = useCredentialRevision();
  const [collapsed, setCollapsed] = useState(false);
  const [mobile, setMobile] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [appearance, setAppearance] = useState(
    () => localStorage.getItem("paperbox:theme") || "light",
  );
  const [systemDark, setSystemDark] = useState(
    window.matchMedia("(prefers-color-scheme: dark)").matches,
  );
  const [apiKey, setApiKey] = useState("");
  const [keyChanged, setKeyChanged] = useState(false);
  const connection = useConnection();
  const location = useLocation();
  const dark = appearance === "dark" || (appearance === "system" && systemDark);
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const update = () => setSystemDark(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  }, [dark]);
  const navigation = (
    <>
      <Link to="/" className="brand" aria-label="Paperbox 首页">
        <span className="brand-icon">
          <BookOpen size={23} />
        </span>
        <span className="brand-copy">
          Paperbox<small>论文工作台</small>
        </span>
      </Link>
      <div className="nav-groups">
        {groups
          .filter((group) => can(group.capability))
          .map((group) => (
            <div
              className={`nav-group ${location.pathname.startsWith("/reading/papers/") && group.label === "阅读" ? "is-current" : ""}`}
              key={group.label}
            >
              <div className="nav-group-title">
                <group.icon size={16} />
                <span>{group.label}</span>
              </div>
              {group.items.map((item) => (
                <Tooltip
                  title={collapsed ? item.label : ""}
                  placement="right"
                  key={item.path}
                >
                  <NavLink
                    to={item.path}
                    end
                    className="nav-item"
                    onClick={() => setMobile(false)}
                  >
                    <span className="nav-dot" />
                    <span>{item.label}</span>
                  </NavLink>
                </Tooltip>
              ))}
            </div>
          ))}
      </div>
      <div className="sidebar-bottom">
        {can("admin") ? (
          <Link
            className="connection"
            to="/system/status"
            onClick={() => setMobile(false)}
          >
            <Circle
              size={9}
              fill="currentColor"
              className={`connection-${connection}`}
            />
            <span>
              {connection === "online"
                ? "API 已连接"
                : connection === "offline"
                  ? "API 不可达"
                  : "等待连接"}
            </span>
          </Link>
        ) : (
          <span className="connection">
            <Circle
              size={9}
              fill="currentColor"
              className={`connection-${connection}`}
            />
            <span>
              {connection === "online"
                ? "API 已连接"
                : connection === "offline"
                  ? "API 不可达"
                  : "等待连接"}
            </span>
          </span>
        )}
        <Button
          aria-label="连接与外观设置"
          title="连接与外观设置"
          type="text"
          icon={<Settings size={18} />}
          onClick={() => setSettingsOpen(true)}
        />
      </div>
    </>
  );
  const current = location.pathname.startsWith("/reading/papers/")
    ? "阅读"
    : groups.flatMap((g) => g.items).find((i) => i.path === location.pathname)
        ?.label || "Paperbox";
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorPrimary: "#147d73",
          borderRadius: 6,
          fontSize: 14,
          fontFamily:
            'Inter, system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif',
        },
      }}
    >
      <AntApp>
        <SidebarContext.Provider
          value={{ collapsed, toggle: () => setCollapsed((v) => !v) }}
        >
          <div className={`workspace ${collapsed ? "sidebar-collapsed" : ""}`}>
            <aside className="sidebar">{navigation}</aside>
            <Drawer
              title="Paperbox"
              placement="left"
              open={mobile}
              onClose={() => setMobile(false)}
              styles={{
                body: { padding: 0, display: "flex", flexDirection: "column" },
              }}
            >
              {navigation}
            </Drawer>
            <div className="workspace-main">
              <div className="topbar">
                <Button
                  className="desktop-toggle"
                  type="text"
                  aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
                  icon={
                    collapsed ? (
                      <ChevronRight size={18} />
                    ) : (
                      <ChevronLeft size={18} />
                    )
                  }
                  onClick={() => setCollapsed((v) => !v)}
                />
                <Button
                  className="mobile-toggle"
                  type="text"
                  aria-label="打开导航"
                  icon={<Menu size={20} />}
                  onClick={() => setMobile(true)}
                />
                <span className="breadcrumb">
                  工作台 <span>/</span> <strong>{current}</strong>
                </span>
                <span className="topbar-note">个人研究空间</span>
              </div>
              <main
                className={`page ${location.pathname.startsWith("/reading/papers/") ? "reader-page" : ""}`}
              >
                <UploadProvider key={revision}>
                  <Suspense fallback={<div className="blank">正在加载…</div>}>
                    <Routes>
                      <Route element={<RequireAccess capability="read" />}>
                        <Route path="/search" element={<SearchPage />} />
                        <Route
                          path="/search/history"
                          element={
                            <HistoryPage key="searches" kind="searches" />
                          }
                        />
                        <Route path="/library" element={<LibraryPage />} />
                        <Route
                          path="/reading/recent"
                          element={
                            <HistoryPage key="readings" kind="readings" />
                          }
                        />
                        <Route
                          path="/reading/papers/:paperId"
                          element={<ReaderPage />}
                        />
                      </Route>
                      <Route element={<RequireAccess capability="write" />}>
                        <Route path="/imports/new" element={<ImportsPage />} />
                        <Route
                          path="/imports/metadata"
                          element={<MetadataImportPage />}
                        />
                        <Route path="/imports/jobs" element={<JobsPage />} />
                      </Route>
                      <Route element={<RequireAccess capability="admin" />}>
                        <Route path="/system/status" element={<Status />} />
                        <Route
                          path="/system/data"
                          element={<DataCheckPage />}
                        />
                      </Route>
                      <Route
                        path="*"
                        element={<Navigate to="/search" replace />}
                      />
                    </Routes>
                  </Suspense>
                </UploadProvider>
              </main>
            </div>
          </div>
        </SidebarContext.Provider>
        <Modal
          title="连接与外观"
          open={settingsOpen}
          onCancel={() => {
            setSettingsOpen(false);
            setApiKey("");
            setKeyChanged(false);
          }}
          onOk={() => {
            if (keyChanged) setCredential(apiKey);
            setApiKey("");
            setKeyChanged(false);
            setSettingsOpen(false);
          }}
          okText="应用"
          cancelText="取消"
          destroyOnHidden
        >
          <Form layout="vertical">
            <p className="muted">
              当前权限：
              {context
                ? `${{ read: "只读", write: "读写", admin: "管理员" }[context.role]} · ${context.prefix}${context.auth_enabled ? "" : "（鉴权关闭）"}`
                : "尚未验证"}
            </p>
            <Form.Item label="API key" htmlFor="api-key">
              <Input.Password
                id="api-key"
                value={apiKey}
                placeholder="输入以更新当前凭证"
                onChange={(e) => {
                  setApiKey(e.target.value);
                  setKeyChanged(true);
                }}
                autoComplete="off"
              />
              <Button
                type="link"
                onClick={() => {
                  setApiKey("");
                  setKeyChanged(true);
                }}
              >
                清除凭证
              </Button>
            </Form.Item>
            <Form.Item label="外观">
              <Radio.Group
                value={appearance}
                options={[
                  { label: "浅色", value: "light" },
                  { label: "深色", value: "dark" },
                  { label: "跟随系统", value: "system" },
                ]}
                onChange={(e) => {
                  setAppearance(e.target.value);
                  localStorage.setItem("paperbox:theme", e.target.value);
                }}
              />
            </Form.Item>
          </Form>
        </Modal>
      </AntApp>
    </ConfigProvider>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AccessProvider>
        <Workspace />
      </AccessProvider>
    </BrowserRouter>
  );
}
