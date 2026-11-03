import { Alert, Button, Modal, Table } from "antd";
import { Check, RefreshCw } from "lucide-react";
import { useRef, useState } from "react";
import { errorMessage, post } from "./api";
import { Failure } from "./components";
import type { SourceReviewDetail } from "./contracts";

type Result = Partial<SourceReviewDetail> & { source_id: string; error?: string };
type Report = { items: Result[]; applied: number };
export default function SourceRematchBatch({ sourceIds, onDone }: { sourceIds: string[]; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [report, setReport] = useState<Report>();
  const [busy, setBusy] = useState(false);
  const [applied, setApplied] = useState(false);
  const [error, setError] = useState("");
  const guard = useRef(false);
  const ready = report?.items.filter((row) => !row.error && row.match?.match_status === "matched"
    && row.source && ["pending", "ambiguous"].includes(row.source.match_status) && !row.source.paper_id) || [];
  async function run(apply = false) {
    if (guard.current) return;
    guard.current = true; setBusy(true); setError("");
    try {
      const items = apply ? ready.map((row) => ({ source_id: row.source_id, revision: row.revision,
        expected_paper_id: row.match?.paper_id })) : sourceIds.map((source_id) => ({ source_id }));
      const result = await post<Report>("/api/metadata/sources/rematch", { items, apply }, undefined, 120000);
      setReport(result);
      if (apply) { setApplied(true); onDone(); }
    } catch (err) { setError(errorMessage(err)); }
    finally { guard.current = false; setBusy(false); }
  }
  return <>
    <Button icon={<RefreshCw size={16} />} disabled={!sourceIds.length || sourceIds.length > 50 || busy}
      onClick={() => { setOpen(true); setApplied(false); setReport(undefined); void run(); }}>重新匹配所选</Button>
    <Modal title="批量重新匹配" open={open} width={850} styles={{ wrapper: { maxWidth: "100vw" } }}
      onCancel={() => { if (!busy) setOpen(false); }} footer={<div className="inline">
        <Button disabled={busy} onClick={() => setOpen(false)}>关闭</Button>
        {!applied && <Button type="primary" icon={<Check size={16} />} loading={busy} disabled={!ready.length || busy}
          onClick={() => void run(true)}>应用唯一匹配（{ready.length}）</Button>}
      </div>}>
      {error && <Failure message={error} />}
      {report && <Alert showIcon type={applied ? "success" : "info"} title={applied
        ? `已关联 ${report.applied} 条来源` : `已预览 ${report.items.length} 条来源，可自动关联 ${ready.length} 条`} />}
      <Table<Result> rowKey="source_id" loading={busy} pagination={false} dataSource={report?.items || []} scroll={{ x: 650 }}
        columns={[
          { title: "来源", render: (_, row) => <span className="metadata-detail">{String(row.values?.title || row.source?.source_ref || row.source_id)}</span> },
          { title: "结果", width: 120, render: (_, row) => row.error ? "处理失败" : row.applied ? "已关联"
            : row.match?.match_status === "matched" ? "唯一匹配" : row.match?.match_status === "unmatched" ? "尚未匹配" : "仍有歧义" },
          { title: "原因", render: (_, row) => <span className="metadata-detail">{row.error || row.match?.reason}</span> },
        ]} />
    </Modal>
  </>;
}
