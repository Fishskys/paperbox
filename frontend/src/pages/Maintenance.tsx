import { Alert, Button, Checkbox, Form, Input, InputNumber, Modal, Select, Table } from "antd";
import { Download, Play, ScanLine } from "lucide-react";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage, post } from "../api";
import { Failure, PageHeader } from "../components";
import type { ReextractReport, ReindexReport } from "../contracts";
import { downloadJson } from "../files";
import { MaintenanceTaskStatus, useMaintenanceTask } from "../maintenance-tasks";

const reasons = [
  { value: "embedding_model_changed", label: "向量模型变化" }, { value: "parser_backend_changed", label: "解析器变化" },
  { value: "open_degradations", label: "未解决的降级" }, { value: "missing_chunks", label: "缺少索引片段" },
];
export default function Maintenance({ kind }: { kind: "index" | "metadata" }) {
  const index = kind === "index";
  const [ids, setIds] = useState("");
  const [all, setAll] = useState(false);
  const [selectedReasons, setReasons] = useState<string[]>([]);
  const [limit, setLimit] = useState(20);
  const [refreshIndex, setRefreshIndex] = useState(true);
  const [indexReport, setReport] = useState<ReindexReport | null>(null);
  const task = useMaintenanceTask<ReextractReport>("metadata");
  const report = index ? indexReport : task.report;
  const [previewValid, setPreviewValid] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(false);
  const guard = useRef(false);
  const invalidate = () => { setReport(null); setError(""); setPreviewValid(false); };
  const count = report ? "selected" in report ? report.selected : report.papers : 0;
  async function submit(dry: boolean) {
    if (guard.current) return;
    if (!index && ids.trim().split(/[\s,]+/).filter(Boolean).length > 1) { setError("元数据维护每次仅支持指定一篇论文；批量处理请留空论文 ID。"); return; }
    guard.current = true; setBusy(true); setError(""); setConfirm(false); setReport(null);
    const paperIds = ids.split(/[\s,]+/).filter(Boolean);
    try {
      if (index) setReport(await post<ReindexReport>("/api/papers/reindex",
        { dry_run: dry, include_all: all, ...(paperIds.length ? { paper_ids: paperIds } : {}), ...(selectedReasons.length ? { reasons: selectedReasons } : {}) }));
      else {
        await task.submit({ operation: "metadata", dry_run: dry, limit, refresh_index: refreshIndex,
          ...(paperIds[0] ? { paper_id: paperIds[0] } : {}), ...(!dry ? { preview_job_id: task.job?.job_id } : {}) });
        setPreviewValid(dry);
      }
    } catch (error) { setError(`${errorMessage(error)}${dry ? "" : "；结果不明确时先检查任务或论文状态，避免重复提交。"}`); }
    finally { guard.current = false; setBusy(false); }
  }
  return <>
    <PageHeader title={index ? "索引维护" : "元数据维护"} description={index ? "批量重建检索索引" : "从已存原文重新提取元数据"} />
    <Form layout="vertical" disabled={busy || (!index && task.active)} className="import-workspace">
      <Form.Item label={index ? "论文 ID（留空按范围选择）" : "论文 ID（留空按处理上限选择）"}>
        <Input.TextArea aria-label="维护论文 ID" value={ids} autoSize={{ minRows: 2, maxRows: 5 }} onChange={(event) => { setIds(event.target.value); invalidate(); }} />
      </Form.Item>
      {index ? <>
        <Form.Item label="重建原因"><Select aria-label="重建原因" mode="multiple" value={selectedReasons} options={reasons} disabled={busy || !!ids.trim() || all} onChange={(value) => { setReasons(value); invalidate(); }} /></Form.Item>
        <Checkbox checked={all} disabled={busy || !!ids.trim()} onChange={(event) => { setAll(event.target.checked); invalidate(); }}>包括全部论文</Checkbox>
      </> : <>
        <Form.Item label="处理上限"><InputNumber aria-label="元数据处理上限" min={1} max={2000} precision={0} value={limit} onChange={(value) => { setLimit(value || 20); invalidate(); }} /></Form.Item>
        <Checkbox checked={refreshIndex} onChange={(event) => { setRefreshIndex(event.target.checked); invalidate(); }}>同步索引元数据</Checkbox>
      </>}
    </Form>
    <div className="metadata-actions">
      <Button icon={<ScanLine size={16} />} loading={busy || (!index && task.active)} onClick={() => void submit(true)}>预览维护</Button>
      <Button type="primary" icon={<Play size={16} />} disabled={busy || (!index && (task.active || !previewValid)) || !report?.dry_run || !count || (!index && ids.trim().split(/[\s,]+/).filter(Boolean).length > 1)} onClick={() => setConfirm(true)}>执行维护</Button>
      <Button icon={<Download size={16} />} disabled={!report || busy} onClick={() => report && downloadJson(report, `paperbox-${kind}-maintenance-${Date.now()}.json`)}>下载报告</Button>
    </div>
    {!index && <MaintenanceTaskStatus task={task} />}
    {error && <Failure message={error} />}
    {report && <>
      <Alert showIcon type={"failed" in report && report.failed ? "warning" : "info"} title={"selected" in report
        ? `${report.dry_run ? "预览" : "已提交"}：选中 ${report.selected}，入队 ${report.queued}，跳过 ${report.skipped.length}`
        : `${report.dry_run ? "预览" : "已完成"}：处理 ${report.papers}，变更 ${report.changed_papers}，无原文 ${report.skipped_no_file}，失败 ${report.failed}`} description={report.note} />
      {"job_ids" in report ? <>
        <div className="metadata-actions">{report.job_ids.map((id) => <Link key={id} to={`/manage/jobs?job=${encodeURIComponent(id)}`}>任务 {id}</Link>)}</div>
        <Table className="data-table" rowKey="_key" pagination={{ pageSize: 20 }} scroll={{ x: 550 }} dataSource={[...report.reasons, ...report.skipped, ...report.skipped_reasons].map((row, i) => ({
          _key: String(i), target: String(row.paper_id || row.code || row.reason || "—"), detail: String(row.message || row.detail || row.description || row.note || "—"), count: String(row.count ?? row.papers ?? "—"),
        }))} columns={[{ title: "论文 / 原因", dataIndex: "target" }, { title: "说明", dataIndex: "detail" }, { title: "数量", dataIndex: "count" }]} />
      </> : <>
        {report.errors.map((value, i) => <Failure key={i} message={`${value.paper_id || ""} ${value.error || value.message || JSON.stringify(value)}`} />)}
        <Table className="data-table" rowKey="_key" dataSource={report.changes.map((row, i) => ({ ...row, _key: String(i) }))} scroll={{ x: 650 }} pagination={{ pageSize: 20 }} columns={[
          { title: "论文", render: (_, row) => <Link to={`/manage/library?paper=${encodeURIComponent(row.paper_id)}`}>{row.paper_id}</Link> },
          { title: "字段", dataIndex: "field" }, { title: "原值", render: (_, row) => JSON.stringify(row.old) }, { title: "新值", render: (_, row) => JSON.stringify(row.new) }, { title: "处置", dataIndex: "action" },
        ]} />
      </>}
    </>}
    <Modal title={index ? "确认重建索引" : "确认重新提取元数据"} open={confirm} onCancel={() => setConfirm(false)} okText="确认执行" cancelText="取消" onOk={() => void submit(false)}>
      <p>将处理 {count} 篇论文。后台执行，刷新页面后仍可查看进度和报告。</p>
    </Modal>
  </>;
}
