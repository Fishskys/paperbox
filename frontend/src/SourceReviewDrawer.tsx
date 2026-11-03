import { Alert, App, Button, Descriptions, Drawer, Input, Modal, Select, Spin, Table, Tag, Tooltip } from "antd";
import { Ban, Check, FilePlus, Link as LinkIcon, RefreshCw, Undo2 } from "lucide-react";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage, post } from "./api";
import { Failure } from "./components";
import type { PaperList, SourceCandidate, SourceReviewDetail } from "./contracts";
import { useResource } from "./hooks";

const labels: Record<string, string> = { title: "标题", authors: "作者", year: "年份", "identifier:doi": "DOI",
  "identifier:arxiv": "arXiv", venue: "期刊 / 会议", abstract: "摘要" };
const actions: Record<string, string> = { rematch: "重新匹配", attach: "关联论文", create: "独立入库", reject: "拒绝来源", restore: "恢复待复核" };
const statuses: Record<string, string> = { pending: "待匹配", ambiguous: "匹配有歧义", matched: "已关联", rejected: "已拒绝" };
const valueText = (value: unknown) => Array.isArray(value) ? value.join("；") : typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "");

export default function SourceReviewDrawer({ sourceId, onClose, onDone }: {
  sourceId: string; onClose: () => void; onDone: () => void;
}) {
  const base = `/api/metadata/sources/${encodeURIComponent(sourceId)}`;
  const [refresh, setRefresh] = useState(0);
  const resource = useResource<SourceReviewDetail>(base, refresh);
  const [preview, setPreview] = useState<SourceReviewDetail>();
  const detail = preview || resource.data;
  const [query, setQuery] = useState("");
  const papers = useResource<PaperList>(`/api/papers?limit=20&q=${encodeURIComponent(query)}`);
  const [paperId, setPaperId] = useState<string>();
  const target = useResource<{ values: Record<string, unknown> }>(paperId ? `/api/papers/${encodeURIComponent(paperId)}/metadata` : null);
  const [action, setAction] = useState<string>();
  const [busy, setBusy] = useState(false);
  const guard = useRef(false);
  const [error, setError] = useState("");
  const { message } = App.useApp();
  const pending = detail && ["pending", "ambiguous"].includes(detail.source.match_status) && !detail.source.paper_id;
  const candidates = detail?.match?.candidates || [];
  const unique = detail?.match?.match_status === "matched";
  function reload() {
    setPreview(undefined); setRefresh((n) => n + 1); setError("");
  }
  function confirm(next: string) { setAction(next); setError(""); }
  async function run(previewOnly = false) {
    if (!detail || guard.current) return;
    guard.current = true; setBusy(true); setError("");
    try {
      if (previewOnly) {
        setPreview(await post<SourceReviewDetail>(`${base}/rematch`, { apply: false }));
      } else {
        const operation = action === "create" ? "create-paper" : action;
        const result = await post<SourceReviewDetail>(`${base}/${operation}`, {
          revision: detail.revision, paper_id: paperId,
          apply: true, expected_paper_id: detail.match?.paper_id,
        });
        const created = result.created_paper_id;
        void message.success(created ? <Link to={`/manage/library?paper=${encodeURIComponent(created)}`}>已独立入库，查看论文</Link>
          : result.conflicts?.length ? `已关联，另有 ${result.conflicts.length} 项字段分歧待复核` : "来源处理已保存");
        onDone(); onClose();
      }
    } catch (err) {
      setError(`${errorMessage(err)}。可刷新来源详情后重试。`);
    } finally { guard.current = false; setBusy(false); }
  }
  return <Drawer title="来源详情" open onClose={() => { if (!busy) onClose(); }}
    styles={{ wrapper: { width: 800, maxWidth: "100vw" } }} destroyOnHidden>
    <div className="list-toolbar">
      <Button icon={<RefreshCw size={16} />} disabled={busy} onClick={reload}>刷新详情</Button>
      {pending && <Button icon={<RefreshCw size={16} />} disabled={busy || !!detail?.parse_error}
        loading={busy && !action} onClick={() => void run(true)}>重新匹配</Button>}
    </div>
    {resource.loading && <Spin />}
    {resource.error && <Failure message={resource.error} retry={reload} />}
    {error && <Failure message={error} />}
    {detail && <>
      <Descriptions column={1} items={[
        { key: "ref", label: "来源记录", children: <span className="metadata-reference">{detail.source.source_ref}</span> },
        { key: "status", label: "状态", children: <Tag>{statuses[detail.source.match_status] || detail.source.match_status}</Tag> },
        { key: "type", label: "来源类型", children: detail.source.source_type },
      ]} />
      <h3>导入记录</h3>
      {detail.parse_error && <Alert type="error" showIcon title={detail.parse_error} />}
      <Descriptions column={1} items={Object.entries(detail.values).filter(([key]) => key in labels).map(([key, value]) => ({
        key, label: labels[key], children: <span className="metadata-detail">{valueText(value)}</span>,
      }))} />
      <h3>当前匹配结果</h3>
      {detail.match && <Alert type={unique ? "success" : "warning"} showIcon
        title={unique ? "找到唯一匹配论文" : detail.match.match_status === "unmatched" ? "尚未找到确定匹配" : "匹配仍有歧义"}
        description={<span className="metadata-detail">{detail.match.reason}</span>} />}
      {pending && unique && <Button className="source-review-command" type="primary" icon={<Check size={16} />}
        disabled={busy} onClick={() => confirm("rematch")}>应用唯一匹配</Button>}
      <Table<SourceCandidate> className="source-candidates" rowKey="paper_id" size="small" pagination={false}
        dataSource={candidates} scroll={{ x: 650 }} columns={[
          { title: "候选论文", dataIndex: "title", width: 260, render: (title, row) => <>
            <Link to={`/manage/library?paper=${encodeURIComponent(row.paper_id)}`}>{title || row.paper_id}</Link>
            <p className="metadata-detail">{row.authors?.join("；")}</p>
            <span className="metadata-detail">{row.filename}</span>
          </> },
          { title: "年份", dataIndex: "year", width: 70 },
          { title: "DOI", dataIndex: "doi", width: 200, render: (doi, row) => <>
            <span className="metadata-detail">{doi || "未登记"}</span>
            {row.identifier_conflicts?.map((text) => <p className="metadata-detail" key={text}>{text}</p>)}
          </> },
          { title: "", width: 90, render: (_, row) => pending && <Button disabled={busy} onClick={() => setPaperId(row.paper_id)}>选择</Button> },
        ]} />
      {pending && !detail.parse_error && <>
        <h3>关联现有论文</h3>
        <Input.Search disabled={busy} aria-label="搜索目标论文" placeholder="搜索论文标题"
          onSearch={(value) => { setQuery(value); setPaperId(undefined); }} />
        {papers.error && <Failure message={papers.error} />}
        <Select aria-label="目标论文" disabled={busy} placeholder="选择目标论文" className="metadata-paper-picker"
          value={paperId} onChange={setPaperId} loading={papers.loading} options={Array.from(new Map([
            ...(papers.data?.papers || []), ...candidates,
          ].map((paper) => [paper.paper_id, paper])).values()).map((paper) => ({
            value: paper.paper_id, label: `${paper.title || "未命名论文"}${paper.year ? ` · ${paper.year}` : ""}`,
          }))} />
        {paperId && <>
          {target.loading && <Spin />}{target.error && <Failure message={target.error} />}
          {target.data && <Descriptions column={1} title="目标论文" items={["title", "authors", "year", "identifier:doi"].map((key) => ({
            key, label: labels[key], children: <span className="metadata-detail">{valueText(target.data?.values[key])}</span>,
          }))} />}
        </>}
        <Button icon={<LinkIcon size={16} />} disabled={!paperId || busy || target.loading || !!target.error}
          onClick={() => confirm("attach")}>确认关联</Button>
      </>}
      <div className="source-review-commands">
        {pending && <>
          <Tooltip title={unique && detail.match?.match_method !== "title_year_author" ? "相同标识符已匹配现有论文" : undefined}>
            <span><Button icon={<FilePlus size={16} />} disabled={busy || !!detail.parse_error || !!(unique && detail.match?.match_method !== "title_year_author")}
              onClick={() => confirm("create")}>独立入库</Button></span>
          </Tooltip>
          <Button danger icon={<Ban size={16} />} disabled={busy} onClick={() => confirm("reject")}>拒绝来源</Button>
        </>}
        {detail.source.match_status === "rejected" && <Button icon={<Undo2 size={16} />} disabled={busy} onClick={() => confirm("restore")}>恢复待复核</Button>}
      </div>
      <h3>处理历史</h3>
      <Table rowKey="id" size="small" dataSource={detail.history} pagination={false} scroll={{ x: 500 }} columns={[
        { title: "操作", dataIndex: "action", render: (value) => actions[value] || value },
        { title: "操作者", dataIndex: "actor" },
        { title: "时间", dataIndex: "created_at", render: (value) => new Date(value).toLocaleString() },
      ]} />
    </>}
    <Modal title={actions[action || ""]} open={!!action} onCancel={() => { if (!busy) setAction(undefined); }}
      onOk={() => void run()} confirmLoading={busy} okText="确认处理" cancelText="取消"
      cancelButtonProps={{ disabled: busy }}>
      {error && <Failure message={error} />}
      <p className="metadata-detail">{valueText(detail?.values.title) || detail?.source.source_ref}</p>
      {action === "create" && <Alert type="info" showIcon title="将建立独立论文记录，状态为等待文件" />}
      {action === "reject" && <Alert type="warning" showIcon title="拒绝后保留原始记录，可从已拒绝来源中恢复" />}
    </Modal>
  </Drawer>;
}
