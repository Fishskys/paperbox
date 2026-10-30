import type { Job } from "./contracts";

const kinds: Record<string, string> = {
  ingest: "论文导入",
  reindex: "重建索引",
  figures: "图片提取",
};

export function jobKind(kind?: string) {
  return kind ? kinds[kind] || kind : "未知";
}

export function jobDuration(job: Job) {
  if (!job.created_at) return "未知";
  const end = job.finished_at || job.updated_at;
  if (!end) return "未知";
  const seconds = Math.max(0, Math.round((Date.parse(end) - Date.parse(job.created_at)) / 1000));
  if (!Number.isFinite(seconds)) return "未知";
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
