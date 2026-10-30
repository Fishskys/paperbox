import { Button, Pagination, Skeleton, Tag } from "antd";
import { Download } from "lucide-react";
import { useEffect, useState } from "react";
import { authHeaders, checkResponse, errorMessage } from "./api";
import { Blank, Failure } from "./components";
import { useResource } from "./hooks";

type Part = { part: number; page: number; preview_url?: string | null };
type Figure = { figure_id: string; display_name: string; caption_text: string; match_status: string; native_url: string; native_format: string; parts: Part[] };
type Figures = { status: string; total: number; figures: Figure[]; last_error?: string | null };

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

export default function FigureGallery({ paperId, go }: { paperId: string; go: (page: number) => void }) {
  const [page, setPage] = useState(1);
  const { data, error, loading } = useResource<Figures>(`/api/papers/${encodeURIComponent(paperId)}/figures?limit=10&offset=${(page - 1) * 10}`);
  const [downloadError, setDownloadError] = useState("");
  const [busy, setBusy] = useState(false);
  async function download(figure: Figure) {
    setBusy(true); setDownloadError("");
    try {
      const response = await fetch(figure.native_url, { headers: authHeaders() });
      checkResponse(response.status, response.ok ? {} : await response.json().catch(() => null));
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a"); link.href = url; link.download = `${figure.figure_id}.${figure.native_format}`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { setDownloadError(errorMessage(error)); } finally { setBusy(false); }
  }
  return <>
    {(error || downloadError) && <Failure message={error || downloadError} />}
    {loading && <Skeleton active />}
    {data?.last_error && <Failure message={data.last_error} />}
    {data && !data.total && <Blank text={["queued", "running"].includes(data.status.toLowerCase()) ? "附图正在提取" : "暂无已生成附图"} />}
    {data?.figures.map((figure) => <figure className="paper-figure" key={figure.figure_id}>
      <figcaption><strong>{figure.display_name}</strong>{figure.match_status === "needs_review" && <Tag color="warning">待复核</Tag>}<p>{figure.caption_text}</p></figcaption>
      {figure.parts.map((part) => <div key={part.part}>{part.preview_url && <FigureImage path={part.preview_url} alt={`${figure.display_name} ${part.part}`} />}<Button type="link" onClick={() => go(part.page)}>第 {part.page} 页</Button></div>)}
      <Button icon={<Download size={16} />} disabled={busy} onClick={() => void download(figure)}>下载附图</Button>
    </figure>)}
    {data && data.total > 10 && <Pagination size="small" current={page} total={data.total} pageSize={10} showSizeChanger={false} onChange={setPage} />}
  </>;
}