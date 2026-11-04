import { App, Button, Dropdown, Tooltip } from "antd";
import { Copy, Download } from "lucide-react";
import { useRef, useState } from "react";
import { authHeaders, checkResponse, errorMessage } from "./api";

export default function CitationActions({ paperId }: { paperId: string }) {
  const { message } = App.useApp();
  const [busy, setBusy] = useState(false);
  const guard = useRef(false);
  async function citation(format: "text" | "bibtex" | "ris") {
    if (guard.current) return;
    guard.current = true; setBusy(true);
    try {
      const response = await fetch(`/api/papers/${encodeURIComponent(paperId)}/citation?format=${format}`, { headers: authHeaders(), signal: AbortSignal.timeout(30000) });
      checkResponse(response.status, response.ok ? {} : await response.json().catch(() => null));
      if (format === "text") {
        await navigator.clipboard.writeText(await response.text());
        void message.success("引用已复制");
      } else {
        const url = URL.createObjectURL(await response.blob());
        const anchor = document.createElement("a");
        anchor.href = url; anchor.download = `paper-${paperId}.${format === "bibtex" ? "bib" : "ris"}`; anchor.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (error) { void message.error(`引用操作失败：${errorMessage(error)}`); }
    finally { guard.current = false; setBusy(false); }
  }
  return <div className="inline">
    <Tooltip title="复制引用"><Button aria-label="复制引用" icon={<Copy size={16} />} loading={busy} onClick={() => void citation("text")} /></Tooltip>
    <Dropdown menu={{ items: [{ key: "bibtex", label: "BibTeX (.bib)" }, { key: "ris", label: "RIS (.ris)" }], onClick: ({ key }) => void citation(key as "bibtex" | "ris") }} trigger={["click"]} disabled={busy}>
      <Tooltip title="导出引用"><Button aria-label="导出引用" icon={<Download size={16} />} disabled={busy} /></Tooltip>
    </Dropdown>
  </div>;
}
