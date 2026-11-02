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
  quality_decisions?: { id: string; field: string; rule_version: string; action: string; reason: string; created_at: string; before?: unknown; candidate?: unknown; selected?: unknown; evidence?: Record<string, unknown> }[];
  index_sync?: { pending: boolean; attempts: number; last_error?: string | null };
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
  papers: number;
  changed_papers: number;
  skipped_no_file: number;
  failed: number;
  changes: { paper_id: string; field: string; old: unknown; new: unknown; action: string }[];
  errors: { paper_id: string; error: string; message?: string }[];
  note: string;
  index_documents: number;
  index_pending?: number;
  quality_counts?: Record<string, number>;
};
export type PaperList = {
  facets?: Record<string, { key: string; count: number }[]> | null;
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
  total_scope?: "query" | "candidate_window";
  mode?: string;
  backend?: string;
  recalled_papers?: number;
  recall_k?: number;
  rerank?: { enabled: boolean; requested?: boolean; applied?: boolean; status?: string; model?: string | null };
  rewrite?: { applied: boolean };
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
  conflicts_total?: number;
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
    orphan_objects?: number;
    orphan_documents?: number;
  };
  parser_backends?: { papers: Record<string, number>; documents: Record<string, number> };
  embedding_models?: { chunks: Record<string, number>; documents: Record<string, number> };
  orphan_objects?: string[];
  orphan_documents?: string[];
  problems: {
    paper_id: string;
    title?: string | null;
    issues: string[];
    deleted: boolean;
    status?: string;
    files_pg?: number;
    objects_minio?: number;
    chunks_pg?: number;
    chunks_os?: number;
    missing_objects?: string[];
    orphan_objects?: string[];
    embedding_models?: string[];
    parser_backend?: string | null;
  }[];
};

export type ReindexReport = { dry_run: boolean; selected: number; queued: number; job_ids: string[];
  skipped: Record<string, unknown>[]; reasons: Record<string, unknown>[]; skipped_reasons: Record<string, unknown>[]; note: string };
export type IdentifierCheck = { checked_at: string; consistent: boolean; papers_live: number; errors: string[]; truncated: boolean;
  drifts: { paper_id: string; kind: string; scheme: string; mirror_value: unknown; identifier_values: string[]; detail: string }[] };
