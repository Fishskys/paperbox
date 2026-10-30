import { authHeaders, checkResponse } from "./api";

export type Figure = {
  figure_id: string;
  display_name: string;
  caption_text: string;
  match_status: string;
  native_url: string;
  native_format: string;
  parts: { part: number; page: number; preview_url?: string | null }[];
};
export type FigureList = {
  status: string;
  generation_id: string | null;
  job_id: string | null;
  total: number;
  figures: Figure[];
  warnings?: Record<string, unknown>[];
  last_error?: string | null;
};
export type FigureExtraction = { job_id: string; status: string; generation_id: string };
export type FigureDeletion = { figures_deleted: number; objects_deleted: number; cleanup_pending: boolean; dry_run: boolean };

export async function figureArchive(paperId: string, generationId: string) {
  if (!generationId) throw new Error("暂无可下载的附图版本，请先刷新附图");
  const params = new URLSearchParams({ generation_id: generationId, variant: "all" });
  const response = await fetch(`/api/papers/${encodeURIComponent(paperId)}/figures/download?${params}`, {
    headers: authHeaders(), signal: AbortSignal.timeout(180000),
  });
  checkResponse(response.status, response.ok ? {} : await response.json().catch(() => null));
  if (response.status === 202) throw new Error("附图尚未就绪，请等待任务完成后重新下载");
  if (!response.headers.get("Content-Type")?.includes("application/zip"))
    throw new Error("服务未返回附图压缩包，请刷新附图后重试");
  return response.blob();
}

export function saveFigureBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
