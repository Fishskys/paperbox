import { authHeaders, checkResponse } from "./api";
import type { Paper } from "./contracts";

export function hasOriginal(paper: Paper) {
  if (!paper.files) return paper.status !== "AWAITING_FILE";
  return paper.files.some(
    (file) =>
      file.mime_type === "application/pdf" ||
      /\.pdf$/i.test(file.storage_key || ""),
  );
}

export async function downloadPaper(id: string, signal?: AbortSignal) {
  const response = await fetch(`/api/papers/${encodeURIComponent(id)}/file`, {
    headers: authHeaders(),
    signal,
  });
  checkResponse(
    response.status,
    response.ok ? {} : await response.json().catch(() => null),
  );
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${id}.pdf`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
