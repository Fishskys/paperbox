import { Alert, Button, Checkbox, Descriptions, Form, Input, Modal, Select, Table, Tabs, Tag } from "antd";
import { Download, History, Play, RefreshCw, ScanLine, ShieldCheck, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage, post, request } from "../api";
import { Failure, PageHeader } from "../components";
import type { CleanupCategory, CleanupHistory, CleanupIndex, CleanupPolicy, CleanupReport, CleanupRequest, DeletedPapers } from "../cleanup-contracts";
import { downloadJson } from "../files";
import { useResource } from "../hooks";
import { MaintenanceTaskStatus, useMaintenanceTask } from "../maintenance-tasks";

const base = "/api/admin/cleanup";
const categories: { value: CleanupCategory; label: string }[] = [
  { value: "app_logs", label: "过期应用日志" }, { value: "search_logs", label: "服务端检索记录" },
  { value: "jobs", label: "终态任务与报告" }, { value: "parse_cache", label: "解析缓存" },
  { value: "figure_history", label: "已清空的图片历史批次" }, { value: "orphan_objects", label: "无论文归属的对象" },
  { value: "orphan_records", label: "无引用的作者、标签和期刊会议" },
];
const names = Object.fromEntries([...categories, { value: "deleted_papers", label: "永久删除论文" }, { value: "indices", label: "旧索引" }].map(row => [row.value, row.label]));
const states: Record<string, string> = { ready: "可清理", protected: "受保护", completed: "已清理", skipped: "已跳过", failed: "失败", working: "执行中" };
const manualNames: Record<string, string> = { container_images_and_build_cache: "容器镜像与构建缓存", uv_npm_model_cache: "uv、npm 与模型缓存",
  multipart_uploads: "MinIO 未完成的分段上传", database_vacuum_and_wal: "数据库维护与 WAL", backups: "备份保留", unregistered_artifacts: "未登记的构建与验收产物" };
function bytes(size: number | null | undefined) { return size == null ? "未估算" : `${(size / 1024 / 1024).toFixed(2)} MiB`; }

export default function Cleanup() {
  const [refresh, setRefresh] = useState(0);
  const [tab, setTab] = useState("overview");
  const [selected, setSelected] = useState<CleanupCategory[]>(["app_logs", "search_logs", "jobs"]);
  const [papers, setPapers] = useState<string[]>([]);
  const [indices, setIndices] = useState<string[]>([]);
  const [page, setPage] = useState(1);
  const [historyPage, setHistoryPage] = useState(1);
  const [reportPage, setReportPage] = useState(1);
  const [scope, setScope] = useState<CleanupRequest>();
  const [invalidatedJob, setInvalidatedJob] = useState<string>();
  const [confirm, setConfirm] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const [claim, setClaim] = useState<CleanupIndex>();
  const [rollback, setRollback] = useState(false);
  const [claiming, setClaiming] = useState(false);
  const [error, setError] = useState("");
  const policy = useResource<CleanupPolicy>(`${base}/policy`, refresh);
  const indexList = useResource<CleanupIndex[]>(tab === "indices" ? `${base}/indices` : null, refresh);
  const deleted = useResource<DeletedPapers>(tab === "deleted" ? `${base}/deleted-papers?offset=${(page - 1) * 20}` : null, refresh);
  const history = useResource<CleanupHistory>(tab === "history" ? `${base}/jobs?offset=${(historyPage - 1) * 20}` : null, refresh);
  const task = useMaintenanceTask<CleanupReport, CleanupRequest>("cleanup", `${base}/jobs`);
  const currentScope = scope || (task.job?.job_id !== invalidatedJob ? task.job?.request : undefined);
  const detailed = useResource<CleanupReport>(task.report && reportPage > 1 ? `${base}/jobs/${task.report.job_id}/report?offset=${(reportPage - 1) * 100}` : null);
  const report = reportPage > 1 ? detailed.data : task.report;
  const valid = !!currentScope && !!task.report?.dry_run && task.report.operation === "cleanup" && task.job?.stage === "COMPLETED"
    && task.report.ready > 0 && Date.parse(task.report.expires_at) > Date.now();
  const invalidate = () => { setScope(undefined); setInvalidatedJob(task.job?.job_id); };
  async function preview(categories: CleanupCategory[], selection: Partial<CleanupRequest> = {}) {
    const body: CleanupRequest = { operation: "cleanup", dry_run: true, categories, ...selection };
    setScope(body); setReportPage(1);
    await task.submit(body);
  }
  async function execute() {
    if (!currentScope || !task.job) return;
    setConfirm(false); setReportPage(1);
    await task.submit({ ...currentScope, dry_run: false, preview_job_id: task.job.job_id });
    setRefresh(value => value + 1);
  }
  async function claimIndex() {
    if (!claim) return;
    setClaiming(true); setError("");
    try { await post(`${base}/indices/claim`, { name: claim.name, is_rollback: rollback }); setClaim(undefined); invalidate(); setRefresh(v => v + 1); }
    catch (error) { setError(errorMessage(error)); }
    finally { setClaiming(false); }
  }
  async function download() {
    if (!task.job) return;
    try { downloadJson(await request(`${base}/jobs/${task.job.job_id}/download`), `cleanup-${task.job.job_id}.json`); }
    catch (error) { setError(errorMessage(error)); }
  }
  const ordinary = <>
    <Form layout="vertical"><Form.Item label="清理范围"><Select aria-label="清理范围" mode="multiple" options={categories} value={selected} disabled={task.active}
      onChange={value => { setSelected(value); invalidate(); }} /></Form.Item></Form>
    <Button icon={<ScanLine size={16} />} disabled={!selected.length || task.active} onClick={() => void preview(selected)}>预览清理</Button>
  </>;
  const dangerous = currentScope?.categories.some(value => ["deleted_papers", "indices"].includes(value));
  return <>
    <PageHeader title="系统清理" description="保留策略、清理预览与执行记录" />
    <div className="metadata-actions"><Button icon={<RefreshCw size={16} />} onClick={() => setRefresh(v => v + 1)}>刷新</Button></div>
    {[policy.error, indexList.error, deleted.error, history.error, detailed.error, error].filter(Boolean).map((message, i) => <Failure key={i} message={message!} />)}
    <Tabs activeKey={tab} onChange={setTab} items={[
      { key: "overview", label: "概览", children: policy.data && <>
        <Descriptions column={{ xs: 1, md: 2 }} items={[
          { key: "auto", label: "自动清理", children: <Tag color={policy.data.automatic ? "green" : "default"}>{policy.data.automatic ? "已启用" : "未启用"}</Tag> },
          { key: "interval", label: "清理周期", children: `${policy.data.interval_s} 秒` },
          { key: "preview", label: "预览有效期", children: `${policy.data.preview_hours} 小时` },
          { key: "batch", label: "每类每批上限", children: policy.data.batch_size },
          { key: "logs", label: "应用文件日志轮转", children: policy.data.log_rotation.enabled ? `${bytes(policy.data.log_rotation.max_bytes)} / ${policy.data.log_rotation.backups} 份` : "未配置文件日志" },
          { key: "upload", label: "上传暂存 GC", children: `${policy.data.upload_gc.interval_s} 秒；失败重试保护 ${policy.data.upload_gc.retry_hours} 小时` },
          { key: "figures", label: "图片待清理标记", children: policy.data.figure_cleanup_pending ?? 0 },
          { key: "scheduler", label: "定期清理任务", children: policy.data.scheduler_status.started ? `运行中；已检查 ${policy.data.scheduler_status.passes} 次` : "未运行" },
        ]} />
        <Table rowKey="key" pagination={false} dataSource={Object.entries(policy.data.days).map(([key, days]) => ({ key, days }))}
          columns={[{ title: "数据", render: (_, row) => names[row.key] || (row.key === "failed_jobs" ? "失败任务" : row.key) }, { title: "保留天数", dataIndex: "days" }]} />
        <Alert type="info" showIcon title="元数据来源、字段历史和人工决定长期保留。永久删除论文与淘汰索引始终需要单独确认。" />
        <Table rowKey="key" pagination={false} dataSource={policy.data.manual_only.map(key => ({ key }))} columns={[{ title: "外部运维范围", render: (_, row) => manualNames[row.key] || row.key }]} />
      </> },
      { key: "cleanup", label: "清理预览", children: ordinary },
      { key: "deleted", label: "永久删除", children: <>
        <Alert type="warning" showIcon title="永久删除会移除论文、原文、图片、元数据历史和已登记索引中的副本，无法撤销。" />
        <Table rowKey="id" loading={deleted.loading} dataSource={deleted.data?.papers || []} scroll={{ x: 650 }}
          rowSelection={{ selectedRowKeys: papers, preserveSelectedRowKeys: true, onChange: keys => { setPapers(keys.map(String)); invalidate(); }, getCheckboxProps: () => ({ disabled: task.active }) }}
          pagination={{ current: page, pageSize: 20, total: deleted.data?.total || 0, showSizeChanger: false, onChange: setPage }}
          columns={[{ title: "标题", dataIndex: "title" }, { title: "论文 ID", dataIndex: "id" }, { title: "删除时间", dataIndex: "deleted_at" }]} />
        <Button danger icon={<ScanLine size={16} />} disabled={!papers.length || papers.length > 100 || task.active} onClick={() => void preview(["deleted_papers"], { paper_ids: papers })}>预览永久删除（{papers.length}）</Button>
      </> },
      { key: "indices", label: "索引归属", children: <>
        <Table rowKey="name" loading={indexList.loading} dataSource={indexList.data || []} scroll={{ x: 750 }}
          rowSelection={{ selectedRowKeys: indices, onChange: keys => { setIndices(keys.map(String)); invalidate(); }, getCheckboxProps: row => ({ disabled: task.active || !row.owned || row.is_rollback || !!row.aliases.length }) }}
          columns={[{ title: "物理索引", dataIndex: "name" }, { title: "别名", render: (_, row) => row.aliases.join(", ") || "无" },
            { title: "归属", render: (_, row) => row.owned ? "已登记" : "待确认" }, { title: "回滚保护", render: (_, row) => row.is_rollback ? "已保护" : "无" },
            { title: "文档数", dataIndex: "documents" }, { title: "占用", dataIndex: "display_size" },
            { title: "操作", render: (_, row) => <Button icon={<ShieldCheck size={16} />} disabled={task.active} onClick={() => { setClaim(row); setRollback(row.is_rollback); }}>确认归属</Button> }]} />
        <Button danger icon={<ScanLine size={16} />} disabled={!indices.length || indices.length > 20 || task.active} onClick={() => void preview(["indices"], { indices })}>预览淘汰索引</Button>
      </> },
      { key: "history", label: "执行记录", children: <Table rowKey="job_id" loading={history.loading} dataSource={history.data?.jobs || []} scroll={{ x: 650 }}
        pagination={{ current: historyPage, pageSize: 20, total: history.data?.total || 0, showSizeChanger: false, onChange: setHistoryPage }}
        columns={[{ title: "任务", render: (_, row) => <Link to={`?task=${row.job_id}`} onClick={() => { invalidate(); setReportPage(1); }}>{row.job_id}</Link> },
          { title: "方式", render: (_, row) => row.dry_run ? "预览" : "执行" }, { title: "状态", dataIndex: "stage" },
          { title: "报告", render: (_, row) => row.report_available ? <History size={16} aria-label="报告可用" /> : "生成中" }]} /> },
    ]} />
    <MaintenanceTaskStatus task={task} hideDownload />
    {task.report && <>
      <Alert showIcon type={task.report.failed || task.report.errors.length ? "warning" : "info"}
        title={`${task.report.dry_run ? "预览" : "执行"}：可清理 ${task.report.ready}，已清理 ${task.report.completed}，受保护 ${task.report.protected}，跳过 ${task.report.skipped}，失败 ${task.report.failed}；对象大小 ${bytes(task.report.size_bytes)}`} />
      {task.report.limited && <Alert type="warning" title="已达到扫描或批次上限，仅包含本批候选；本批完成后可重新预览。" />}
      {task.report.errors.map(message => <Failure key={message} message={message} />)}
      <div className="metadata-actions">
        <Button type="primary" danger={dangerous} icon={dangerous ? <Trash2 size={16} /> : <Play size={16} />} disabled={!valid || task.active} onClick={() => { setConfirmation(""); setConfirm(true); }}>执行本次预览</Button>
        <Button icon={<RefreshCw size={16} />} disabled={!currentScope || task.active || task.job?.stage !== "FAILED" || task.report.dry_run} onClick={() => currentScope && void task.submit({ ...currentScope, dry_run: false, retry_job_id: task.job?.job_id })}>重试失败项</Button>
        <Button icon={<Download size={16} />} onClick={() => void download()}>下载完整清理报告</Button>
      </div>
      <Table rowKey="id" dataSource={report?.items || []} scroll={{ x: 650 }} pagination={{ current: reportPage, pageSize: 100, total: task.report.total, showSizeChanger: false, onChange: setReportPage }}
        columns={[{ title: "类别", render: (_, row) => names[row.category] || row.category }, { title: "资源", dataIndex: "resource_key" },
          { title: "大小", render: (_, row) => bytes(row.size_bytes) }, { title: "状态", render: (_, row) => states[row.state] || row.state }, { title: "说明", dataIndex: "error" }]} />
    </>}
    <Modal title={dangerous ? "确认永久删除" : "确认清理"} open={confirm} onCancel={() => setConfirm(false)} onOk={() => void execute()} okText="确认执行" cancelText="取消"
      okButtonProps={{ danger: dangerous, disabled: dangerous && confirmation !== "DELETE" }}>
      <p>仅处理预览中的 {task.report?.ready || 0} 项候选。发生变化或仍在使用的资源将跳过。</p>
      {dangerous && <Form.Item label="输入 DELETE 确认"><Input aria-label="永久删除确认" value={confirmation} onChange={event => setConfirmation(event.target.value)} /></Form.Item>}
    </Modal>
    <Modal title="确认索引归属" open={!!claim} onCancel={() => setClaim(undefined)} onOk={() => void claimIndex()} okText="登记" cancelText="取消" confirmLoading={claiming}>
      <p>{claim?.name}</p><Checkbox checked={rollback} onChange={event => setRollback(event.target.checked)}>保护此索引用于回滚</Checkbox>
    </Modal>
  </>;
}
