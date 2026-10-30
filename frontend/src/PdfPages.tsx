import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { Page } from "react-pdf";
import { highlightText } from "./pdf-text";

type Viewport = RefObject<HTMLDivElement | null>;

function PdfSheet({
  number,
  width,
  height,
  viewport,
  onError,
  query,
}: {
  number: number;
  width: number;
  height: number;
  viewport: Viewport;
  onError: (message: string) => void;
  query: string;
}) {
  const element = useRef<HTMLElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => setVisible(entry.isIntersecting),
      { root: viewport.current, rootMargin: "900px 0px" },
    );
    if (element.current) observer.observe(element.current);
    return () => observer.disconnect();
  }, [viewport]);
  return (
    <section
      ref={element}
      className="pdf-sheet"
      data-page-number={number}
      aria-label={`第 ${number} 页`}
      style={{ width, height }}
    >
      {visible && (
        <Page
          pageNumber={number}
          width={width}
          loading={<div className="pdf-loading">正在渲染第 {number} 页…</div>}
          onRenderError={(error) => onError(error.message)}
          customTextRenderer={({ str }) => highlightText(str, query)}
        />
      )}
    </section>
  );
}

export default function PdfPages({
  pdf,
  page,
  width,
  viewport,
  onPageChange,
  onError,
  query = "",
}: {
  pdf: PDFDocumentProxy;
  page: number;
  width: number;
  viewport: Viewport;
  onPageChange: (page: number) => void;
  onError: (message: string) => void;
  query?: string;
}) {
  const [ratios, setRatios] = useState<number[]>([]);
  const document = useRef<HTMLDivElement>(null);
  const position = useRef({ page: 0, width: 0 });
  useEffect(() => {
    let cancelled = false;
    // Read geometry once so mixed-size pages retain stable scroll positions.
    void Promise.all(
      Array.from({ length: pdf.numPages }, async (_, index) => {
        const sheet = await pdf.getPage(index + 1);
        const size = sheet.getViewport({ scale: 1 });
        return size.height / size.width;
      }),
    ).then(
      (sizes) => {
        if (!cancelled) setRatios(sizes);
      },
      (error: Error) => {
        if (!cancelled) onError(error.message);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [pdf, onError]);
  useLayoutEffect(() => {
    const root = viewport.current;
    const sheet = document.current?.children[page - 1];
    if (
      !root ||
      !sheet ||
      (position.current.page === page && position.current.width === width)
    )
      return;
    position.current = { page, width };
    root.scrollTop +=
      sheet.getBoundingClientRect().top - root.getBoundingClientRect().top - 18;
  }, [page, width, ratios, viewport]);
  useEffect(() => {
    const root = viewport.current;
    if (!root || !ratios.length) return;
    let frame = 0;
    const scroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const sheets = Array.from(document.current?.children || []);
        const anchor =
          root.getBoundingClientRect().top +
          Math.min(120, root.clientHeight / 3);
        const index = sheets.findIndex(
          (sheet) => sheet.getBoundingClientRect().bottom > anchor,
        );
        const current = index < 0 ? pdf.numPages : index + 1;
        if (current !== position.current.page) {
          position.current.page = current;
          onPageChange(current);
        }
      });
    };
    root.addEventListener("scroll", scroll, { passive: true });
    return () => {
      root.removeEventListener("scroll", scroll);
      cancelAnimationFrame(frame);
    };
  }, [pdf, ratios, viewport, onPageChange]);
  return (
    <div className="pdf-pages" ref={document}>
      {!ratios.length && <div className="pdf-loading">正在准备页面…</div>}
      {ratios.map((ratio, index) => (
        <PdfSheet
          key={index}
          number={index + 1}
          width={width}
          height={width * ratio}
          viewport={viewport}
          onError={onError}
          query={query}
        />
      ))}
    </div>
  );
}
