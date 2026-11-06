import { Alert, Button, Card, Skeleton, Tag, Tooltip } from "antd";
import { Info, RefreshCw } from "lucide-react";
import { useState } from "react";
import { Failure } from "../components";
import { useResource } from "../hooks";
import type { ProcessMemory, RuntimeMemory } from "../observability-contracts";
import { groupDependencies, serviceNames, type Dependency } from "../system-status";

type Health = { version: string; degraded: boolean; services: Record<string, string>; dependencies?: Record<string, Dependency> };
const states: Record<string, { text: string; color: string }> = {
  ok: { text: "正常", color: "success" }, error: { text: "异常", color: "error" },
  disabled: { text: "未启用", color: "default" }, unknown: { text: "未知", color: "warning" },
  partial: { text: "部分未启用", color: "warning" },
};
function bytes(value: number | null | undefined) {
  if (value == null) return "不可用";
  return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`;
}
function Memory({ value, loading }: { value?: ProcessMemory; loading: boolean }) {
  return <>
    <div className="system-memory-value">{loading ? "采集中" : bytes(value?.rss_bytes)}</div>
    <div className="system-memory-meta"><span>RSS{value?.source === "container" ? " · 进程合计" : ""}</span>
      <Tooltip title="RSS 是驻留物理内存，进程合计可能重复计算共享页；PSS 按比例分摊共享内存。"><Info size={14} aria-label="内存指标说明" /></Tooltip>
    </div>
    <div className="system-memory-meta">PSS {bytes(value?.pss_bytes)}{value?.processes ? ` · ${value.processes} 个进程` : ""}</div>
    {value?.container && <div className="system-memory-meta">{value.container}</div>}
    {value?.partial && <Tag color="warning">部分进程未采集</Tag>}
    {value?.reason && <p className="muted system-card-detail">{value.reason}</p>}
  </>;
}

export default function Status() {
  const [refresh, setRefresh] = useState(0);
  const { data, error, loading } = useResource<Health>("/health", refresh);
  const memory = useResource<RuntimeMemory>("/api/admin/observability/memory", refresh);
  return <>
    <div className="system-view-toolbar"><h2>服务与依赖</h2>
      <Button icon={<RefreshCw size={16} />} loading={loading || memory.loading} onClick={() => setRefresh(v => v + 1)}>刷新状态</Button>
    </div>
    {error && <Failure message={error} retry={() => setRefresh(v => v + 1)} />}
    {memory.error && !error && <Alert type="warning" showIcon title={`内存采集失败：${memory.error}`} />}
    {loading && <Skeleton active />}
    {data && <div className="system-status-grid">
      <Card size="small" className="system-metric-card" title="Paperbox API" extra={<Tag color={data.degraded ? "warning" : "success"}>{data.degraded ? "依赖未完全就绪" : "服务已连接"}</Tag>}>
        <Memory value={memory.data?.backend} loading={memory.loading} /><p className="system-card-detail muted">v{data.version}</p>
      </Card>
      {groupDependencies(data.services, data.dependencies, memory.data?.dependencies).map(group => {
        const view = states[group.status];
        return <Card size="small" className="system-metric-card" key={group.key} title={group.title} extra={<Tag color={view.color}>{view.text}</Tag>}>
          <Memory value={group.memory} loading={memory.loading} />
          {group.services.map(service => {
            const entry = service.detail;
            const state = states[service.status] || states.unknown;
            return <div className="system-card-detail" key={service.name}>
              {group.services.length > 1 && <div className="system-service-heading"><strong>{serviceNames[service.name] || service.name}</strong><Tag color={state.color}>{state.text}</Tag></div>}
              <div>{entry?.latency_ms != null && <span>{Math.round(entry.latency_ms)} ms · </span>}<span>{entry?.required === false ? "可选依赖" : "必需依赖"}</span>{entry?.reason && <p>{entry.reason}</p>}</div>
              {entry?.checked_at && <small className="muted">{new Date(entry.checked_at).toLocaleString("zh-CN")}{entry.cached ? " · 缓存结果" : ""}</small>}
            </div>;
          })}
        </Card>;
      })}
    </div>}
    {memory.data && <p className="muted system-measured-at">内存采样于 {new Date(memory.data.measured_at).toLocaleString("zh-CN")}</p>}
  </>;
}
