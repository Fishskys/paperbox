// The fields consumed by the WebUI; the source contract is app/schemas/.
export type Paper = {
  paper_id: string;
  title: string;
  authors: string[];
  year?: number | null;
  venue?: string | null;
  abstract?: string | null;
  doi?: string | null;
  status: string;
  files?: { mime_type?: string | null }[];
};
export type PaperList = {
  total: number;
  limit: number;
  offset: number;
  papers: Paper[];
};
export type Evidence = {
  chunk_id: string;
  text: string;
  page?: number | null;
  section?: string | null;
};
export type SearchResult = Paper & { evidence: Evidence[]; score: number };
export type SearchResponse = {
  total: number;
  took_ms: number;
  results: SearchResult[];
};
export type Job = {
  job_id: string;
  paper_id?: string | null;
  stage: string;
  progress: number;
  error_code?: string | null;
  error_message?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  finished_at?: string | null;
};
export type JobList = {
  total: number;
  limit: number;
  offset: number;
  jobs: Job[];
};
export type Accepted = {
  job_id: string;
  paper_id?: string | null;
  status: string;
  duplicate?: boolean;
};
export type FileResult = {
  filename: string;
  status: string;
  job_id?: string | null;
  paper_id?: string | null;
  error_message?: string | null;
};
export type BatchAccepted = {
  results: FileResult[];
  accepted: number;
  duplicate: number;
  rejected: number;
};
export type MetadataSource = {
  source_ref: string;
  paper_id?: string | null;
  match_status?: string;
  match_method?: string | null;
  reason?: string | null;
  note?: string | null;
  evidence?: string[];
  match_conflicts?: string[];
  candidates?: unknown[];
};
export type MetadataImportReport = {
  detected: number;
  total: number;
  matched: number;
  created_shell: number;
  ambiguous: number;
  unmatched: number;
  unchanged: number;
  failed: number;
  skipped: number;
  failures: { index: number; identifier?: string | null; reason: string }[];
  conflicts: Record<string, unknown>[];
  sources: MetadataSource[];
  dry_run: boolean;
  format: string;
};
export type Consistency = {
  consistent: boolean;
  checked_at: string;
  errors: string[];
  truncated: boolean;
  totals: {
    papers_live: number;
    documents_os: number;
    objects_minio: number;
    problems: number;
  };
  problems: {
    paper_id: string;
    title?: string | null;
    issues: string[];
    deleted: boolean;
  }[];
};
