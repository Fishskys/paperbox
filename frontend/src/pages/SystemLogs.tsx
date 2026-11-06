import { Alert, Button, Form, Input, Select, Table, Tag } from "antd";
import { Download, RefreshCw, Search, X } from "lucide-react";
import { useState } from "react";
import { errorMessage, request } from "../api";
import { Failure } from "../components";
import { downloadJson } from "../files";
import { useResource } from "../hooks";
import type { SystemLogEntry, SystemLogs } from "../observability-contracts";

const base = "/api/admin/observability/logs";
type Filters = { level?: string; logger?: string; q?: string; request_id?: string; since?: string; until?: string };
const colors: Record<string, string> = { DEBUG: "default", INFO: "blue", WARNING: "warning", ERROR: "error", CRITICAL: "error" };

export default function SystemLogsPage() {
  const [form] = Form.useForm<Filters>();
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [refresh, setRefresh] = useState(0);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState("");
  const [exportNotice, setExportNotice] = useState("");
  const params = new URLSearchParams(query);
  params.set("offset", String((page - 1) * 50)); params.set("limit", "50");
  const result = useResource<SystemLogs>(`${base}?${params}`, refresh);
  function apply(values: Filters) {
    if (values.since && values.until && values.since > values.until) { setError("开始时间不能晚于结束时间"); return; }
    const next = new URLSearchParams();
    for (const [key, value] of Object.entries(values)) {
      if (value?.trim()) next.set(key, key === "since" || key === "until" ? new Date(value).toISOString() : value.trim());
    }
    setError(""); setExportNotice(""); setPage(1); setQuery(next.toString()); setRefresh(v => v + 1);
  }
  async function download() {
    setExporting(true); setError(""); setExportNotice("");
    try {
      const data = await request<SystemLogs>(`${base}/export?${query}`);
      if (!data.available) { setError(data.reason || "日志不可用"); return; }
      downloadJson(data, `paperbox-logs-${new Date().toISOString().replaceAll(":", "-")}.json`);
      setExportNotice(`已导出 ${data.total} 条日志${data.truncated ? "，仅包含最近扫描窗口，历史记录未全部覆盖" : ""}`);
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setExporting(false); }
  }
  return <>
    <Form form={form} layout="vertical" onFinish={apply} className="system-log-filters">
      <Form.Item name="q" label="关键词" className="system-log-keyword"><Input allowClear prefix={<Search size={15} />} placeholder="搜索日志" /></Form.Item>
      <Form.Item name="level" label="级别"><Select allowClear placeholder="全部级别" options={Object.keys(colors).map(value => ({ value, label: value }))} /></Form.Item>
      <Form.Item name="logger" label="模块"><Input allowClear placeholder="模块名称" /></Form.Item>
      <Form.Item name="request_id" label="请求 ID"><Input allowClear placeholder="精确匹配" /></Form.Item>
      <Form.Item name="since" label="开始时间"><Input type="datetime-local" step="1" /></Form.Item>
      <Form.Item name="until" label="结束时间"><Input type="datetime-local" step="1" /></Form.Item>
      <div className="system-log-filter-actions"><Button type="primary" htmlType="submit" icon={<Search size={16} />}>查询</Button><Button icon={<X size={16} />} onClick={() => { form.resetFields(); apply({}); }}>重置</Button></div>
    </Form>
    <div className="system-view-toolbar">
      <span className="muted">最近扫描范围内匹配 {result.data?.total ?? "—"} 条</span>
      <div className="row-end"><Button icon={<RefreshCw size={16} />} loading={result.loading} onClick={() => setRefresh(v => v + 1)}>刷新日志</Button><Button icon={<Download size={16} />} loading={exporting} disabled={!result.data?.available || result.loading} onClick={() => void download()}>导出筛选结果</Button></div>
    </div>
    {(error || result.error) && <Failure message={error || result.error!} />}
    {result.data?.reason && <Alert type={result.data.available ? "warning" : "info"} showIcon title={result.data.reason} />}
    {result.data?.truncated && <Alert type="warning" showIcon title={`当前仅扫描最近 ${(result.data.scan_limit_bytes / 1024 / 1024).toFixed(1)} MiB 日志，筛选和导出均限于此范围，未覆盖全部历史。`} />}
    {exportNotice && <Alert type="success" showIcon title={exportNotice} />}
    <Table<SystemLogEntry> className="system-log-table" rowKey="id" loading={result.loading} dataSource={result.data?.entries || []} scroll={{ x: 1000 }}
      pagination={{ current: page, pageSize: 50, total: result.data?.total || 0, showSizeChanger: false, onChange: setPage }}
      expandable={{ expandedRowRender: entry => <div className="system-log-detail"><p>模块：{entry.logger} · 请求 ID：{entry.request_id || "—"} · 身份：{entry.key_prefix || "—"}</p><pre>{entry.message}</pre></div> }}
      columns={[
        { title: "时间", dataIndex: "timestamp", width: 185, render: value => new Date(value).toLocaleString("zh-CN") },
        { title: "级别", dataIndex: "level", width: 105, render: value => <Tag color={colors[value] || "default"}>{value}</Tag> },
        { title: "模块", dataIndex: "logger", width: 220, ellipsis: true },
        { title: "消息", dataIndex: "message", ellipsis: true, render: value => <span className="system-log-preview">{String(value).split("\n")[0]}</span> },
        { title: "请求 ID", dataIndex: "request_id", width: 180, ellipsis: true },
      ]} />
  </>;
}
