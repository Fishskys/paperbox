import { authHeaders, checkResponse } from "./api";
import type { Paper } from "./contracts";

export function downloadJson(value: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

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
