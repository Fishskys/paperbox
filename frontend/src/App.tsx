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
  Files,
} from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import {
  createBrowserRouter,
  RouterProvider,
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
const LibraryManagementPage = lazy(() => import("./pages/LibraryManagement"));
const HistoryPage = lazy(() => import("./pages/History"));
const ReaderPage = lazy(() => import("./pages/Reader"));
const ImportsPage = lazy(() => import("./pages/Imports"));
const MetadataImportPage = lazy(() => import("./pages/MetadataImport"));
const JobsPage = lazy(() => import("./pages/Jobs"));
const DataCheckPage = lazy(() => import("./pages/DataCheck"));
const MaintenancePage = lazy(() => import("./pages/Maintenance"));

const groups = [
  {
    label: "检索",
    capability: "read" as const,
    icon: Search,
    items: [
      { label: "常规检索", path: "/search/standard" },
      { label: "智能检索", path: "/search" },
      { label: "检索记录", path: "/search/history" },
    ],
  },
  {
    label: "阅读",
    capability: "read" as const,
    icon: Library,
    items: [
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
    ],
  },
  {
    label: "论文管理",
    capability: "write" as const,
    icon: Files,
    items: [
      { label: "论文库管理", path: "/manage/library" },
      { label: "处理任务", path: "/manage/jobs" },
    ],
  },
  {
    label: "系统管理",
    capability: "admin" as const,
    icon: ShieldCheck,
    items: [
      { label: "运行状态", path: "/system/status" },
      { label: "数据检查", path: "/system/data" },
      { label: "索引维护", path: "/system/index" },
      { label: "元数据维护", path: "/system/metadata" },
    ],
  },
];

function LegacyRoute({ to, library = false }: { to: string; library?: boolean }) {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  if (library) {
    if (params.get("q") && !params.has("scope")) params.set("scope", "title");
    if (!params.has("sort")) params.set("sort", "newest");
  }
  return <Navigate replace to={{ pathname: to, search: params.toString(), hash: location.hash }} state={location.state} />;
}

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
          colorPrimary: dark ? "#67c5b0" : "#116e65",
          colorLink: dark ? "#67c5b0" : "#116e65",
          colorText: dark ? "#e1e8e7" : "#243136",
          colorTextSecondary: dark ? "#b4c2bf" : "#536269",
          colorTextTertiary: dark ? "#b4c2bf" : "#536269",
          colorTextQuaternary: dark ? "#b4c2bf" : "#536269",
          colorTextPlaceholder: dark ? "#b4c2bf" : "#536269",
          colorBorder: dark ? "#839792" : "#7b8b91",
          colorBgContainer: dark ? "#202526" : "#ffffff",
          colorBgElevated: dark ? "#202526" : "#ffffff",
          colorBgLayout: dark ? "#171c1d" : "#f6f8f9",
          colorPrimaryHover: dark ? "#83d5c3" : "#0d6159",
          colorPrimaryActive: dark ? "#55b5a0" : "#09574f",
          colorPrimaryText: dark ? "#67c5b0" : "#116e65",
          colorPrimaryTextHover: dark ? "#83d5c3" : "#0d6159",
          colorPrimaryTextActive: dark ? "#55b5a0" : "#09574f",
          colorTextLightSolid: dark ? "#172724" : "#ffffff",
          colorSuccessText: dark ? "#9ed8a9" : "#286238",
          colorWarningText: dark ? "#f1cb7b" : "#795015",
          colorErrorText: dark ? "#ffb4b0" : "#a12b32",
          colorInfoText: dark ? "#a4ceff" : "#235b96",
          borderRadius: 6,
          fontSize: 14,
          fontFamily:
            'Inter, system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif',
        },
        components: {
          Tag: {
            colorSuccess: dark ? "#9ed8a9" : "#286238",
            colorWarning: dark ? "#f1cb7b" : "#795015",
            colorError: dark ? "#ffb4b0" : "#a12b32",
            colorInfo: dark ? "#a4ceff" : "#235b96",
          },
          // Tooltip surfaces remain dark in either appearance.
          Tooltip: { colorTextLightSolid: "#ffffff" },
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
                        <Route path="/search/standard" element={<LibraryPage title="常规检索" conventional />} />
                        <Route
                          path="/search/history"
                          element={
                            <HistoryPage key="searches" kind="searches" />
                          }
                        />
                        <Route path="/library" element={<LegacyRoute to="/search/standard" library />} />
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
                        <Route
                          path="/manage/library"
                          element={<LibraryManagementPage />}
                        />
                        <Route path="/imports/new" element={<ImportsPage />} />
                        <Route
                          path="/imports/metadata"
                          element={<MetadataImportPage />}
                        />
                        <Route path="/manage/jobs" element={<JobsPage />} />
                        <Route path="/imports/jobs" element={<LegacyRoute to="/manage/jobs" />} />
                      </Route>
                      <Route element={<RequireAccess capability="admin" />}>
                        <Route path="/system/index" element={<MaintenancePage key="index" kind="index" />} />
                        <Route path="/system/metadata" element={<MaintenancePage key="metadata" kind="metadata" />} />
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

const router = createBrowserRouter([{ path: "*", element: <AccessProvider><Workspace /></AccessProvider> }]);

export default function App() {
  return (
    <RouterProvider router={router} />
  );
}
