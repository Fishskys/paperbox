import { Alert, Button, Modal, Pagination, Skeleton, Tag } from "antd";
import { Download, RefreshCw, ScanLine, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAccess } from "./access";
import { authHeaders, checkResponse, errorMessage, post, request, useCredentialRevision } from "./api";
import { Blank, Failure } from "./components";
import { figureArchive, saveFigureBlob } from "./figure-model";
import type { Figure, FigureDeletion, FigureExtraction, FigureList } from "./figure-model";
import { useVisible } from "./hooks";
import { readerPath } from "./search-model";

function FigureImage({ path, alt }: { path: string; alt: string }) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController(); let objectUrl = "";
    void fetch(path, { headers: authHeaders(), signal: controller.signal }).then(async (response) => {
      checkResponse(response.status, response.ok ? {} : await response.json().catch(() => null));
      const blob = await response.blob();
      if (!controller.signal.aborted) { objectUrl = URL.createObjectURL(blob); setUrl(objectUrl); }
    }).catch((error) => { if (!controller.signal.aborted) setError(errorMessage(error)); });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [path]);
  return error ? <Failure message={error} /> : url ? <img className="figure-preview" src={url} alt={alt} /> : <Skeleton.Image active />;
}

type Props = { paperId: string; go?: (page: number) => void; manage?: boolean; originalAvailable?: boolean };
type Action = { kind: "extract" } | { kind: "delete"; figure?: Figure; preview: FigureDeletion };
const statuses: Record<string, string> = {
  not_extracted: "尚未提取", queued: "排队中", running: "提取中", completed: "已完成",
  partial: "部分完成", failed: "提取失败", cancelled: "已取消", deleted: "已删除", stale: "原文已变更",
};

export default function FigureGallery(props: Props) {
  return <FigurePanel key={props.paperId} {...props} />;
}

function FigurePanel({ paperId, go, manage = false, originalAvailable = false }: Props) {
  const { can } = useAccess();
  const writable = manage && can("write");
  const [page, setPage] = useState(1);
  const [refresh, setRefresh] = useState(0);
  const credential = useCredentialRevision();
  const visible = useVisible();
  const base = `/api/papers/${encodeURIComponent(paperId)}/figures`;
  const [state, setState] = useState<{ data?: FigureList; error?: string; loading: boolean }>({ loading: true });
  const [busy, setBusy] = useState(false);
  const guard = useRef(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<{ text: string; warning?: boolean } | null>(null);
  const [action, setAction] = useState<Action | null>(null);
  const [lastJob, setLastJob] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setState((previous) => ({ ...previous, loading: true, error: undefined }));
    async function load() {
      try {
        const data = await request<FigureList>(`${base}?limit=10&offset=${(page - 1) * 10}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setState({ data, loading: false });
        setPending(false);
        if (data.job_id) setLastJob(data.job_id);
        if (data.job_id || ["queued", "running"].includes(data.status))
          timer = setTimeout(() => void load(), 2000);
      } catch (error) {
        if (!controller.signal.aborted) setState((previous) => ({ ...previous, loading: false, error: errorMessage(error) }));
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [base, page, refresh, credential, visible]);
  const data = state.data;
  const active = pending || !!data?.job_id || ["queued", "running"].includes(data?.status || "");
  const disabled = busy || state.loading || !!state.error;
  async function extract(force: boolean) {
    if (!writable || guard.current) return;
    guard.current = true; setBusy(true); setError(""); setNotice(null);
    try {
      const result = await post<FigureExtraction>(`${base}/extract`, { force });
      setLastJob(result.job_id);
      setPending(["queued", "running"].includes(result.status));
      setNotice({ text: ["queued", "running"].includes(result.status) ? "附图提取任务已接收" : "已有附图版本可用" });
      setAction(null); setRefresh((value) => value + 1);
    } catch (error) { setError(`${errorMessage(error)}；请刷新附图或查看相关任务，核对提交结果。`); }
    finally { guard.current = false; setBusy(false); }
  }
  async function previewDelete(figure?: Figure) {
    if (!writable || guard.current) return;
    guard.current = true; setBusy(true); setError(""); setNotice(null);
    try {
      const preview = await request<FigureDeletion>(`${base}${figure ? `/${encodeURIComponent(figure.figure_id)}` : ""}?dry_run=true`, { method: "DELETE" });
      setAction({ kind: "delete", figure, preview });
    } catch (error) { setError(errorMessage(error)); }
    finally { guard.current = false; setBusy(false); }
  }
  async function remove() {
    if (!writable || action?.kind !== "delete" || guard.current) return;
    guard.current = true; setBusy(true); setError("");
    try {
      const result = await request<FigureDeletion>(`${base}${action.figure ? `/${encodeURIComponent(action.figure.figure_id)}` : ""}?dry_run=false`, { method: "DELETE" });
      setNotice({ text: `已删除 ${result.figures_deleted} 张附图${result.cleanup_pending ? "，存储清理待完成" : ""}`, warning: result.cleanup_pending });
      setAction(null); setPage(1); setRefresh((value) => value + 1);
    } catch (error) { setError(`${errorMessage(error)}；请刷新附图核对结果后再操作。`); }
    finally { guard.current = false; setBusy(false); }
  }
  async function download(figure?: Figure) {
    if (guard.current || (!figure && !data?.generation_id)) return;
    guard.current = true; setBusy(true); setError("");
    try {
      if (figure) {
        const response = await fetch(figure.native_url, { headers: authHeaders(), signal: AbortSignal.timeout(180000) });
        checkResponse(response.status, response.ok ? {} : await response.json().catch(() => null));
        saveFigureBlob(await response.blob(), `${figure.figure_id}.${figure.native_format}`);
      } else saveFigureBlob(await figureArchive(paperId, data!.generation_id!), `${paperId}-figures.zip`);
    } catch (error) { setError(errorMessage(error)); }
    finally { guard.current = false; setBusy(false); }
  }
  return <>
    <div className="metadata-actions figure-actions">
      <Button icon={<RefreshCw size={16} />} loading={state.loading} disabled={busy} onClick={() => setRefresh((value) => value + 1)}>刷新附图</Button>
      <Button icon={<Download size={16} />} disabled={disabled || !data?.total || !data.generation_id} onClick={() => void download()}>下载全部附图</Button>
      {writable && <>
        <Button icon={<ScanLine size={16} />} disabled={disabled || active || !originalAvailable} onClick={() => data?.generation_id ? setAction({ kind: "extract" }) : void extract(false)}>{data?.generation_id ? "重新提取附图" : "提取附图"}</Button>
        <Button danger icon={<Trash2 size={16} />} disabled={disabled || active || !data?.total} onClick={() => void previewDelete()}>删除全部附图</Button>
      </>}
    </div>
    {data && <div className="data-summary"><span>附图状态：{active ? "提取任务处理中" : statuses[data.status] || data.status}</span><span>共 {data.total} 张</span></div>}
    {writable && !originalAvailable && <Alert type="info" showIcon title="补传 PDF 原文后可提取附图" />}
    {notice && <Alert showIcon type={notice.warning ? "warning" : "success"} title={notice.text} />}
    {writable && (data?.job_id || lastJob) && <Link to={`/manage/jobs?job=${encodeURIComponent(data?.job_id || lastJob!)}`}>查看附图任务</Link>}
    {(state.error || error) && <Failure message={state.error || error} retry={() => setRefresh((value) => value + 1)} />}
    {state.loading && !data && <Skeleton active />}
    {data?.last_error && <Failure message={data.last_error} />}
    {!!data?.warnings?.length && <Alert type="warning" showIcon title="附图提取存在质量提示" description={data.warnings.map((warning) => String(warning.message || warning.code || JSON.stringify(warning))).join("；")} />}
    {data && !data.total && <Blank text={active ? "附图正在提取" : "暂无已生成附图"} />}
    {data?.figures.map((figure) => <figure className="paper-figure" key={figure.figure_id}>
      <figcaption><strong>{figure.display_name}</strong>{figure.match_status === "needs_review" && <Tag color="warning">待复核</Tag>}<p>{figure.caption_text}</p></figcaption>
      {figure.parts.map((part) => <div key={part.part}>{part.preview_url && <FigureImage path={part.preview_url} alt={`${figure.display_name} ${part.part}`} />}{go ? <Button type="link" onClick={() => go(part.page)}>第 {part.page} 页</Button> : <Link to={readerPath(paperId, part.page)}>第 {part.page} 页</Link>}</div>)}
      <div className="metadata-actions">
        <Button icon={<Download size={16} />} disabled={disabled} onClick={() => void download(figure)}>下载附图</Button>
        {writable && <Button danger icon={<Trash2 size={16} />} aria-label={`删除附图 ${figure.display_name}`} disabled={disabled || active} onClick={() => void previewDelete(figure)}>删除附图</Button>}
      </div>
    </figure>)}
    {data && data.total > 10 && <Pagination size="small" current={page} total={data.total} pageSize={10} showSizeChanger={false} onChange={setPage} />}
    <Modal title={action?.kind === "extract" ? "重新提取附图" : "删除附图"} open={!!action} confirmLoading={busy} closable={!busy} mask={{ closable: !busy }} cancelButtonProps={{ disabled: busy }} onCancel={() => !busy && setAction(null)} okText="确认执行" cancelText="取消" onOk={() => action?.kind === "extract" ? extract(true) : remove()}>
      {action?.kind === "extract" ? <p>将重新提取论文附图，任务成功后发布新版本。提取期间保留已有附图。</p> : action?.kind === "delete" && <p>将删除{action.figure ? ` ${action.figure.display_name}` : "全部附图"}，预览共 {action.preview.figures_deleted} 张。论文原文与元数据保留。</p>}
    </Modal>
  </>;
}
