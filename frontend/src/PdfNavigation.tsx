import { Button, Input, Pagination, Progress, Radio, Tag } from "antd";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { useEffect, useRef, useState } from "react";
import { Outline, Thumbnail } from "react-pdf";
import { errorMessage } from "./api";
import { Blank, Failure } from "./components";
import FigureGallery from "./FigureGallery";
import { findText } from "./pdf-text";

function LazyThumbnail({ pdf, number, selected, go }: { pdf: PDFDocumentProxy; number: number; selected: boolean; go: (page: number) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { rootMargin: "300px" });
    if (ref.current) observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return <div ref={ref} className={`pdf-thumbnail ${selected ? "selected" : ""}`}>
    {visible ? <Thumbnail pdf={pdf} pageNumber={number} width={125} onItemClick={() => go(number)} /> : <div className="thumbnail-placeholder" />}
    <Button type="text" onClick={() => go(number)} aria-label={`缩略图第 ${number} 页`}>{number}</Button>
  </div>;
}

type Match = { page: number; count: number; snippet: string };
function TextSearch({ pdf, go, onQuery }: { pdf: PDFDocumentProxy; go: (page: number) => void; onQuery: (query: string) => void }) {
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<Match[]>([]);
  const [scanned, setScanned] = useState(0);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);
  const cache = useRef(new Map<number, string>());
  useEffect(() => {
    if (!query) return;
    let cancelled = false;
    void (async () => {
      const found: Match[] = [];
      try {
        for (let number = 1; number <= pdf.numPages && !cancelled; number++) {
          let text = cache.current.get(number);
          if (text === undefined) {
            const content = await (await pdf.getPage(number)).getTextContent();
            text = content.items.map((item) => "str" in item ? item.str : "").join(" ");
            cache.current.set(number, text);
          }
          const match = findText(text, query);
          if (match) found.push({ page: number, ...match });
          if (!cancelled) { setScanned(number); setMatches([...found]); }
        }
      } catch (error) { if (!cancelled) setError(errorMessage(error)); }
    })();
    return () => { cancelled = true; };
  }, [pdf, query]);
  function search(value: string) {
    if (value.trim() === query) return;
    setMatches([]); setScanned(0); setError(""); setPage(1); setQuery(value.trim()); onQuery(value.trim());
  }
  return <>
    <Input.Search aria-label="文内查找" allowClear value={input} onChange={(event) => { setInput(event.target.value); if (!event.target.value) search(""); }} onSearch={search} />
    {query && <Progress percent={Math.round(scanned * 100 / pdf.numPages)} size="small" />}
    {error && <Failure message={error} />}
    {query && <p className="muted">{matches.reduce((sum, match) => sum + match.count, 0)} 处 · {matches.length} 页{scanned < pdf.numPages ? " · 查找中" : ""}</p>}
    {matches.slice((page - 1) * 20, page * 20).map((match) => <button className="pdf-text-match" key={match.page} onClick={() => go(match.page)}><strong>第 {match.page} 页 · {match.count} 处</strong><span>{match.snippet}</span></button>)}
    {matches.length > 20 && <Pagination size="small" current={page} total={matches.length} pageSize={20} showSizeChanger={false} onChange={setPage} />}
  </>;
}

export default function PdfNavigation({ pdf, paperId, page, go, onQuery }: { pdf: PDFDocumentProxy; paperId: string; page: number; go: (page: number) => void; onQuery: (query: string) => void }) {
  const [tab, setTab] = useState("outline");
  const [empty, setEmpty] = useState(false);
  const [error, setError] = useState("");
  return <>
    <Radio.Group className="pdf-navigation-tabs" value={tab} onChange={(event) => setTab(event.target.value)} optionType="button" options={[
      { label: "目录", value: "outline" }, { label: "缩略图", value: "thumbnails" }, { label: "查找", value: "search" }, { label: "附图", value: "figures" },
    ]} />
    {tab === "outline" && <><Outline pdf={pdf} onLoadSuccess={(outline) => setEmpty(!outline?.length)} onLoadError={(error) => setError(error.message)} onItemClick={({ pageNumber }) => go(pageNumber)} />{empty && <Blank text="此 PDF 未提供目录" />}{error && <Failure message={error} />}</>}
    {tab === "thumbnails" && <div className="pdf-thumbnails">{Array.from({ length: pdf.numPages }, (_, i) => <LazyThumbnail key={i} pdf={pdf} number={i + 1} selected={page === i + 1} go={go} />)}</div>}
    <div hidden={tab !== "search"}><TextSearch pdf={pdf} go={go} onQuery={onQuery} /></div>
    {tab === "figures" && <><Tag>已有附图</Tag><FigureGallery paperId={paperId} go={go} /></>}
  </>;
}