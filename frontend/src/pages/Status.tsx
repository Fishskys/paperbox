import { Button, Skeleton, Tag } from "antd";
import { RefreshCw, Server } from "lucide-react";
import { useState } from "react";
import { Blank, Failure, PageHeader } from "../components";
import { useResource } from "../hooks";

type Dependency = {
  status?: string;
  reason?: string;
  latency_ms?: number;
  checked_at?: string;
  cached?: boolean;
  required?: boolean;
};
type Health = {
  version: string;
  degraded: boolean;
  services: Record<string, string>;
  dependencies?: Record<string, Dependency>;
};
const states: Record<string, { text: string; color: string }> = {
  ok: { text: "正常", color: "success" },
  error: { text: "异常", color: "error" },
  disabled: { text: "未启用", color: "default" },
  unknown: { text: "未知", color: "warning" },
};

export default function Status() {
  const [refresh, setRefresh] = useState(0);
  const { data, error, loading } = useResource<Health>("/health", refresh);
  return (
    <>
      <PageHeader
        title="运行状态"
        description="服务与依赖"
        actions={
          <Button
            icon={<RefreshCw size={16} />}
            loading={loading}
            onClick={() => setRefresh((v) => v + 1)}
          >
            刷新状态
          </Button>
        }
      />
      {error && (
        <Failure message={error} retry={() => setRefresh((v) => v + 1)} />
      )}
      {loading && <Skeleton active />}
      {data && (
        <>
          <div className="section-heading">
            <span>
              <Server size={18} /> Paperbox API{" "}
              <span className="muted">v{data.version}</span>
            </span>
            <Tag color={data.degraded ? "warning" : "success"}>
              {data.degraded ? "依赖未完全就绪" : "服务已连接"}
            </Tag>
          </div>
          {Object.entries(data.services).map(([name, status]) => {
            const entry = data.dependencies?.[name];
            const view = states[status] || states.unknown;
            return (
              <div className="dependency-row" key={name}>
                <div>
                  <strong>{name}</strong>
                  <p className="muted">
                    {entry?.reason ||
                      (status === "ok" ? "连接正常" : "暂无详情")}
                  </p>
                  {entry?.checked_at && (
                    <small className="muted">
                      {new Date(entry.checked_at).toLocaleString("zh-CN")}
                      {entry.cached ? " · 缓存结果" : ""}
                    </small>
                  )}
                </div>
                <div className="row-end">
                  {entry?.latency_ms != null && (
                    <span className="muted">
                      {Math.round(entry.latency_ms)} ms
                    </span>
                  )}
                  <Tag color={view.color}>{view.text}</Tag>
                </div>
              </div>
            );
          })}
          {!Object.keys(data.services).length && <Blank text="暂无依赖状态" />}
        </>
      )}
    </>
  );
}
