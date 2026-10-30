import { Alert, Button, Checkbox, Form, Input, InputNumber, Modal, Table, Upload } from "antd";
import { Download, FolderSearch, UploadCloud } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage, post, request } from "./api";
import { Failure } from "./components";
import { downloadJson } from "./files";

type Entry = { filename: string; relative?: string; entry?: string; status: string; job_id?: string; error_code?: string; error_message?: string };
type Report = { dry_run?: boolean; matched?: number; accepted: number; duplicate: number; rejected: number; skipped?: number;
  entries_ignored?: number; entries_rejected?: number; results?: Entry[]; jobs?: Entry[] };

export default function ServerImports({ mode }: { mode: "archive" | "directory" }) {
  const [file, setFile] = useState<File | null>(null);
  const [root, setRoot] = useState("");
  const [pattern, setPattern] = useState("**/*.pdf");
  const [recursive, setRecursive] = useState(true);
  const [limit, setLimit] = useState(2000);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const guard = useRef(false);
  function invalidate() { setReport(null); setError(""); }
  useEffect(() => {
    if (!busy) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [busy]);
  async function submit(dryRun: boolean) {
    if (guard.current) return;
    guard.current = true; setBusy(true); setError(""); setReport(null); setConfirm(false);
    try {
      if (mode === "directory") setReport(await post<Report>("/api/papers/ingest/dir", {
        root: root.trim(), glob: pattern, recursive, limit, dry_run: dryRun,
      }, undefined, 180000));
      else if (file) {
        const body = new FormData(); body.append("file", file);
        setReport(await request<Report>("/api/papers/ingest/compressed", { method: "POST", body }, 180000));
      }
    } catch (error) {
      setError(`${errorMessage(error)}${dryRun ? "" : "；结果不明确时先查看任务，避免重复提交。"}`);
    } finally { guard.current = false; setBusy(false); }
  }
  return <div className="import-workspace">
    {mode === "archive" ? <Upload.Dragger accept=".zip,application/zip" showUploadList={false} disabled={busy}
      beforeUpload={(value) => { setFile(value); invalidate(); return false; }}>
      <UploadCloud size={28} className="upload-mark" />
      <p>{file?.name || "选择 ZIP 文件"}</p>
    </Upload.Dragger> : <Form layout="vertical" disabled={busy}>
      <Form.Item label="服务端目录"><Input aria-label="服务端目录" value={root} onChange={(event) => { setRoot(event.target.value); invalidate(); }} /></Form.Item>
      <Form.Item label="文件匹配"><Input aria-label="文件匹配" value={pattern} onChange={(event) => { setPattern(event.target.value); invalidate(); }} /></Form.Item>
      <Form.Item label="处理上限"><InputNumber aria-label="目录处理上限" min={1} max={2000} precision={0} value={limit} onChange={(value) => { setLimit(value || 2000); invalidate(); }} /></Form.Item>
      <Checkbox checked={recursive} onChange={(event) => { setRecursive(event.target.checked); invalidate(); }}>包含子目录</Checkbox>
    </Form>}
    <div className="metadata-actions">
      {mode === "directory" ? <>
        <Button icon={<FolderSearch size={16} />} disabled={!root.trim() || busy} loading={busy} onClick={() => void submit(true)}>预览目录</Button>
        <Button type="primary" icon={<UploadCloud size={16} />} disabled={busy || !report?.dry_run || !report.accepted} onClick={() => setConfirm(true)}>正式导入目录</Button>
      </> : <Button type="primary" icon={<UploadCloud size={16} />} disabled={!file || busy} loading={busy} onClick={() => void submit(false)}>开始 ZIP 导入</Button>}
      <Button icon={<Download size={16} />} disabled={!report || busy} onClick={() => report && downloadJson(report, `paperbox-${mode}-report-${Date.now()}.json`)}>下载报告</Button>
      <Link to="/manage/jobs">查看处理任务</Link>
    </div>
    {error && <Failure message={error} />}
    {report && <>
      <Alert type={report.rejected || report.entries_rejected ? "warning" : "success"} showIcon
        title={`${report.dry_run ? "目录预览" : "导入已接收"}：接收 ${report.accepted}，重复 ${report.duplicate}，拒绝 ${report.rejected + (report.entries_rejected || 0)}，跳过 ${report.skipped || report.entries_ignored || 0}`} />
      <Table<Entry> className="data-table" rowKey={(row) => row.relative || row.entry || row.filename}
        dataSource={report.results || report.jobs || []} scroll={{ x: 650 }} pagination={{ pageSize: 20, showSizeChanger: false }}
        columns={[{ title: "文件", render: (_, row) => row.relative || row.entry || row.filename },
          { title: "状态", dataIndex: "status" }, { title: "错误", render: (_, row) => `${row.error_code || ""} ${row.error_message || ""}` },
          { title: "任务", render: (_, row) => row.job_id ? <Link to={`/manage/jobs?job=${encodeURIComponent(row.job_id)}`}>查看任务</Link> : "—" }]} />
    </>}
    <Modal title="确认目录导入" open={confirm} onCancel={() => setConfirm(false)} okText="确认导入" cancelText="取消" onOk={() => void submit(false)}>
      <p>{root}</p><p>将接收 {report?.accepted || 0} 个文件并创建后台任务。</p>
    </Modal>
  </div>;
}
