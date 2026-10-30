// The fields consumed by the WebUI; the source contract is app/schemas/.
export type Capability = "read" | "write" | "admin";
export type AuthContext = {
  prefix: string;
  role: Capability;
  source: string;
  is_anonymous: boolean;
  auth_enabled: boolean;
  read_access: "public" | "key";
  capabilities: Record<Capability, boolean>;
};
export type Paper = {
  paper_id: string;
  title: string;
  authors: string[];
  year?: number | null;
  venue?: string | null;
  abstract?: string | null;
  doi?: string | null;
  arxiv_id?: string | null;
  paper_type?: string | null;
  volume?: string | null;
  issue?: string | null;
  pages?: string | null;
  publication_date?: string | null;
  url?: string | null;
  status: string;
  files?: {
    mime_type?: string | null;
    storage_key?: string;
    size_bytes?: number | null;
  }[];
};
export type PaperMetadata = {
  paper_id: string;
  values: Record<string, unknown>;
  sources: { source_id: string; source_type: string; source_ref: string }[];
  provenance: Record<
    string,
    {
      provenance_id: string;
      value: unknown;
      source_id?: string | null;
      is_current: boolean;
      decided_by: string;
      decided_at?: string | null;
    }[]
  >;
};
export type ReextractReport = {
  dry_run: boolean;
  changed_papers: number;
  skipped_no_file: number;
  failed: number;
  changes: { field: string; old: unknown; new: unknown; action: string }[];
  errors: { paper_id: string; error: string }[];
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
  kind?: string;
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
export type QueueSnapshot = {
  started: boolean;
  concurrency: number;
  running: number;
  queued: number;
  queued_high: number;
  queued_low: number;
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
export type ReviewSource = {
  source_id: string;
  source_type: string;
  source_ref: string;
  match_status: string;
  match_method?: string | null;
};
export type MetadataConflict = {
  paper_id: string;
  field: string;
  kept: unknown;
  rejected: unknown;
  provenance_id?: string | null;
};
export type MetadataReviewReport = {
  total: number;
  items: ReviewSource[];
  conflicts: MetadataConflict[];
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
