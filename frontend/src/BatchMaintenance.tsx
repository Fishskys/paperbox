import { Alert, Button, Modal, Select, Table, Tag } from "antd";
import { Play, ScanLine, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import type { BatchMaintenanceReport, Paper } from "./contracts";
import { MaintenanceTaskStatus, useMaintenanceTask } from "./maintenance-tasks";

const statuses: Record<string, string> = { ready: "可执行", completed: "已完成", unchanged: "无需变更", accepted: "已接收", queued: "已入队", failed: "失败", skipped_no_file: "无原文，已跳过", skipped_active_job: "有活动任务，已跳过" };

export default function BatchMaintenance({ papers, onClear }: { papers: Paper[]; onClear: () => void }) {
  const [operation, setOperation] = useState<"metadata" | "reindex">("metadata");
  const [previewScope, setPreviewScope] = useState("");
  const [confirm, setConfirm] = useState(false);
  const task = useMaintenanceTask<BatchMaintenanceReport>("batch");
  const scope = JSON.stringify({ operation, paper_ids: papers.map(paper => paper.paper_id) });
  const validPreview = scope === previewScope && task.job?.dry_run && task.job.stage === "COMPLETED" && task.report?.dry_run && task.report.items.some(item => item.status === "ready");
  async function submit(dry: boolean) {
    setConfirm(false);
    if (dry) setPreviewScope(scope);
    await task.submit({ operation, paper_ids: papers.map(paper => paper.paper_id), limit: papers.length,
      dry_run: dry, ...(!dry ? { preview_job_id: task.job?.job_id } : {}) });
  }
  return <section className="batch-maintenance" aria-label="批量维护">
    <div className="metadata-actions">
      <span>已选 {papers.length} / 100 篇</span>
      <Select aria-label="批量操作" value={operation} disabled={task.active} onChange={setOperation} style={{ width: 180 }} options={[{ value: "metadata", label: "重新提取元数据" }, { value: "reindex", label: "重建索引" }]} />
      <Button icon={<ScanLine size={16} />} disabled={!papers.length || task.active} onClick={() => void submit(true)}>预览所选论文</Button>
      <Button type="primary" icon={<Play size={16} />} disabled={!validPreview || task.active || !papers.length} onClick={() => setConfirm(true)}>执行所选维护</Button>
      <Button icon={<X size={16} />} disabled={!papers.length || task.active} onClick={onClear}>清空选择</Button>
    </div>
    <MaintenanceTaskStatus task={task} />
    {task.report && <>
      <Alert type={task.report.failed ? "warning" : "info"} title={`${task.report.dry_run ? "预览" : "执行"}：${task.report.papers} 篇，失败 ${task.report.failed}，无原文 ${task.report.skipped_no_file}，活动任务 ${task.report.skipped_active || 0}，已入队 ${task.report.queued || 0}`} />
      <Table className="data-table" size="small" rowKey="paper_id" dataSource={task.report.items} pagination={{ pageSize: 10 }} scroll={{ x: 650 }} columns={[
        { title: "论文", render: (_, item) => <Link to={`/manage/library?paper=${encodeURIComponent(item.paper_id)}`}>{item.title || item.paper_id}</Link> },
        { title: "结果", render: (_, item) => <Tag color={item.status === "failed" ? "error" : undefined}>{statuses[item.status] || item.status}</Tag> },
        { title: "明细", render: (_, item) => item.job_id ? <Link to={`/manage/jobs?job=${encodeURIComponent(item.job_id)}`}>查看处理任务</Link> : item.error || "—" },
      ]} expandable={{ expandedRowRender: item => <pre className="report-value">{JSON.stringify(task.report!.changes.filter(change => change.paper_id === item.paper_id), null, 2)}</pre> }} />
    </>}
    <Modal title="确认批量维护" open={confirm} onCancel={() => setConfirm(false)} onOk={() => void submit(false)} okText="确认执行" cancelText="取消">
      <p>{operation === "metadata" ? "重新提取元数据" : "重建索引"}：{papers.length} 篇。处理范围以已完成预览为准，变更或不可处理的论文将逐项报告。</p>
    </Modal>
  </section>;
}
