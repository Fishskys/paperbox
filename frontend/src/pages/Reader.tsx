import { App, Button, Drawer, InputNumber, Skeleton, Tooltip } from "antd";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Download,
  Info,
  Maximize,
  Minus,
  PanelLeftClose,
  Plus,
  ListTree,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Document, pdfjs } from "react-pdf";
import type { PDFDocumentProxy } from "pdfjs-dist";
import {
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { errorMessage, useAuthHeaders, useCredentialRevision } from "../api";
import { useSidebar } from "../workspace-context";
import { Blank, Failure } from "../components";
import type { Paper } from "../contracts";
import { downloadPaper, hasOriginal } from "../files";
import { remember } from "../history";
import { useResource } from "../hooks";
import { positivePage } from "../search-model";
import PdfPages from "../PdfPages";
import PdfNavigation from "../PdfNavigation";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();
const options = {
  cMapUrl: "/pdfjs/cmaps/",
  standardFontDataUrl: "/pdfjs/standard_fonts/",
  wasmUrl: "/pdfjs/wasm/",
};

export default function Reader() {
  const { paperId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const revision = useCredentialRevision();
  const headers = useAuthHeaders();
  const { toggle } = useSidebar();
  const { message } = App.useApp();
  const {
    data: paper,
    error,
    loading,
  } = useResource<Paper>(`/api/papers/${encodeURIComponent(paperId)}`);
  const [pages, setPages] = useState(0);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [pdfError, setPdfError] = useState("");
  const [reload, setReload] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [info, setInfo] = useState(false);
  const [navigation, setNavigation] = useState(false);
  const [query, setQuery] = useState("");
  const [width, setWidth] = useState(700);
  const container = useRef<HTMLDivElement>(null);
  const page = positivePage(
    params.get("page"),
    pages || Number.MAX_SAFE_INTEGER,
  );
  const file = useMemo(
    () => ({
      url: `/api/papers/${encodeURIComponent(paperId)}/file`,
      httpHeaders: headers,
    }),
    [paperId, headers],
  );
  const from = (location.state as { from?: string } | null)?.from || "/search/standard";
  useEffect(() => {
    setPages(0);
    setPdf(null);
    setPdfError("");
    setQuery("");
  }, [paperId, revision, reload]);
  useEffect(() => {
    if (!container.current) return;
    const observer = new ResizeObserver(([entry]) =>
      setWidth(Math.max(240, entry.contentRect.width - 32)),
    );
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (paper && pages)
      remember("readings", {
        id: paper.paper_id,
        title: paper.title,
        authors: paper.authors,
        page,
        time: new Date().toISOString(),
      });
  }, [paper, pages, page]);
  useEffect(() => {
    if (pages && params.get("page") !== String(page))
      setParams(
        { page: String(page) },
        { replace: true, state: location.state },
      );
  }, [page, pages, params, setParams, location.state]);
  function go(value: number) {
    setParams(
      { page: String(Math.min(pages, Math.max(1, value))) },
      { replace: true, state: location.state },
    );
  }
  async function download() {
    try {
      await downloadPaper(paperId);
    } catch (error) {
      void message.error(errorMessage(error));
    }
  }
  return (
    <div className="reader">
      <div className="reader-title">
        <Button
          aria-label="返回来源页面"
          icon={<ArrowLeft size={17} />}
          onClick={() => {
            if (location.state?.from) navigate(-1);
            else navigate(from);
          }}
        />
        <h1>{paper?.title || "阅读"}</h1>
        <Tooltip title="收起 / 展开侧栏">
          <Button
            className="reader-sidebar-toggle"
            type="text"
            aria-label="切换侧栏"
            icon={<PanelLeftClose size={18} />}
            onClick={toggle}
          />
        </Tooltip>
      </div>
      <div className="reader-toolbar">
        <div className="inline">
          <Button
            aria-label="上一页"
            icon={<ChevronLeft size={17} />}
            disabled={!pages || page <= 1}
            onClick={() => go(page - 1)}
          />
          <InputNumber
            aria-label="阅读页码"
            controls={false}
            min={1}
            max={pages || 1}
            value={page}
            onChange={(value) => {
              if (value && pages) go(value);
            }}
          />
          <span className="muted">/ {pages || "—"}</span>
          <Button
            aria-label="下一页"
            icon={<ChevronRight size={17} />}
            disabled={!pages || page >= pages}
            onClick={() => go(page + 1)}
          />
        </div>
        <div className="inline reader-zoom">
          <Button
            aria-label="缩小"
            icon={<Minus size={16} />}
            disabled={zoom <= 0.5}
            onClick={() => setZoom((v) => Math.max(0.5, v - 0.1))}
          />
          <span>{Math.round(zoom * 100)}%</span>
          <Button
            aria-label="放大"
            icon={<Plus size={16} />}
            disabled={zoom >= 2}
            onClick={() => setZoom((v) => Math.min(2, v + 0.1))}
          />
          <Tooltip title="适应宽度">
            <Button
              aria-label="适应宽度"
              icon={<Maximize size={16} />}
              onClick={() => setZoom(1)}
            />
          </Tooltip>
        </div>
        <div className="inline">
          <Tooltip title="目录、缩略图与文内查找"><Button aria-label="阅读导航" icon={<ListTree size={16} />} disabled={!pdf} onClick={() => setNavigation(true)} /></Tooltip>
          <Tooltip title="论文信息">
            <Button
              aria-label="论文信息"
              icon={<Info size={16} />}
              onClick={() => setInfo(true)}
            />
          </Tooltip>
          <Tooltip title="下载原文">
            <Button
              aria-label="下载原文"
              disabled={!paper || !hasOriginal(paper)}
              icon={<Download size={16} />}
              onClick={() => void download()}
            />
          </Tooltip>
        </div>
      </div>
      {error && <Failure message={error} />}
      {loading && <Skeleton active />}
      {pdfError && (
        <Failure
          message={`原文加载失败：${pdfError}`}
          retry={() => setReload((v) => v + 1)}
        />
      )}
      <div className="pdf-viewport" ref={container}>
        {paper && !hasOriginal(paper) && <Blank text="待补充 PDF 原文" />}
        {paper && hasOriginal(paper) && (
          <Document
            key={`${paperId}-${revision}-${reload}`}
            file={file}
            options={options}
            loading={<div className="pdf-loading">正在加载原文…</div>}
            error=""
            onLoadSuccess={(document) => {
              setPages(document.numPages);
              setPdf(document);
              setPdfError("");
            }}
            onLoadError={(error) => setPdfError(error.message)}
          >
            {pdf && (
              <PdfPages
                key={`${paperId}-${revision}-${reload}`}
                pdf={pdf}
                page={page}
                width={Math.min(width, 1100) * zoom}
                viewport={container}
                onPageChange={go}
                onError={setPdfError}
                query={query}
              />
            )}
          </Document>
        )}
      </div>
      <Drawer title="阅读导航" open={navigation} onClose={() => setNavigation(false)} size={380}>
        {pdf && <PdfNavigation key={`${paperId}-${revision}-${reload}`} pdf={pdf} paperId={paperId} page={page} go={go} onQuery={setQuery} />}
      </Drawer>
      <Drawer title="论文信息" open={info} onClose={() => setInfo(false)}>
        {paper && (
          <div className="paper-info">
            <h2>{paper.title}</h2>
            <p>{paper.authors.join(", ")}</p>
            <p>{[paper.year, paper.venue].filter(Boolean).join(" · ")}</p>
            <h3>摘要</h3>
            <p>{paper.abstract || "暂无摘要"}</p>
            <h3>DOI</h3>
            <p>{paper.doi || "未标注"}</p>
          </div>
        )}
      </Drawer>
    </div>
  );
}
