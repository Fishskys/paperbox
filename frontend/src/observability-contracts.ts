export type ProcessMemory = {
  rss_bytes: number | null;
  pss_bytes: number | null;
  processes: number;
  partial: boolean;
  source: "backend" | "container" | "unavailable";
  container: string | null;
  reason: string | null;
};
export type RuntimeMemory = {
  measured_at: string;
  backend: ProcessMemory;
  dependencies: Record<string, ProcessMemory>;
};
export type SystemLogEntry = {
  id: string;
  timestamp: string;
  level: string;
  logger: string;
  request_id: string | null;
  key_prefix: string | null;
  message: string;
};
export type SystemLogs = {
  entries: SystemLogEntry[];
  total: number;
  offset: number;
  limit: number;
  available: boolean;
  reason: string | null;
  truncated: boolean;
  scanned_bytes: number;
  scan_limit_bytes: number;
  measured_at: string;
};
