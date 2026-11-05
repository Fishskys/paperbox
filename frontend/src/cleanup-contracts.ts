import type { MaintenanceJob } from "./contracts";

export type CleanupCategory = "app_logs" | "search_logs" | "jobs" | "parse_cache" | "figure_history" | "orphan_objects" | "orphan_records" | "deleted_papers" | "indices";
export interface CleanupRequest {
  operation: "inventory" | "cleanup";
  categories: CleanupCategory[];
  dry_run: boolean;
  paper_ids?: string[];
  indices?: string[];
  preview_job_id?: string;
  retry_job_id?: string;
}
export interface CleanupReport {
  job_id: string;
  operation: string;
  dry_run: boolean;
  expires_at: string;
  total: number;
  ready: number;
  protected: number;
  completed: number;
  skipped: number;
  failed: number;
  size_bytes: number | null;
  limited: boolean;
  errors: string[];
  items: { id: string; category: string; resource_key: string; size_bytes: number | null; state: string; error: string | null; detail: Record<string, unknown> }[];
}
export interface CleanupPolicy {
  automatic: boolean;
  interval_s: number;
  preview_hours: number;
  batch_size: number;
  days: Record<string, number>;
  log_rotation: { enabled: boolean; max_bytes: number; backups: number };
  upload_gc: { interval_s: number; archive_hours: number; retry_hours: number };
  manual_only: string[];
  figure_cleanup_pending: number;
  scheduler_status: { started: boolean; passes: number; last: { figure_objects: number } | null };
}
export interface CleanupIndex {
  name: string;
  owned: boolean;
  aliases: string[];
  is_rollback: boolean;
  documents: number;
  display_size: string | null;
}
export interface DeletedPapers { total: number; papers: { id: string; title: string | null; deleted_at: string }[] }
export interface CleanupHistory { total: number; jobs: MaintenanceJob[] }
