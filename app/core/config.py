"""Environment-driven configuration for paperbox.

All settings come from environment variables (see ``.env`` at the repository
root, which is git-ignored). Nothing is hard-coded: this machine runs the
infrastructure inside WSL2 (mirrored networking) while the app runs on Windows,
and the dependency ports are reachable through ``localhost`` as long as they are
allowed in WSL's ufw (see ``AGENTS.md`` §3).
"""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Accepted values of ``PARSER_BACKEND``. ``docling`` is the new primary backend,
#: ``pypdf`` the degradation backend (and the default until the acceptance run).
PARSER_BACKENDS = frozenset({"pypdf", "docling"})


class Settings(BaseSettings):
    """Typed view over the process environment."""

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- application ---
    app_name: str = "paperbox"
    app_env: str = Field(default="local", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- PostgreSQL (fact source for paper metadata) ---
    postgres_dsn: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/paperbox",
        alias="POSTGRES_DSN",
    )
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_user: str = Field(default="postgres", alias="POSTGRES_USER")
    postgres_password: str = Field(default="postgres", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="paperbox", alias="POSTGRES_DB")

    # --- OpenSearch (full-text + vector index) ---
    opensearch_url: str = Field(default="http://localhost:9200", alias="OPENSEARCH_URL")
    opensearch_index: str = Field(default="paper_chunks_v1", alias="OPENSEARCH_INDEX")
    opensearch_alias: str = Field(default="paper_chunks_current", alias="OPENSEARCH_ALIAS")

    # --- MinIO (raw object storage) ---
    minio_endpoint: str = Field(default="localhost:9000", alias="MINIO_ENDPOINT")
    minio_access_key: str = Field(default="minioadmin", alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field(default="minioadmin", alias="MINIO_SECRET_KEY")
    minio_secure: bool = Field(default=False, alias="MINIO_SECURE")
    minio_bucket: str = Field(default="paperbox", alias="MINIO_BUCKET")

    # --- Embedding Server (external service, BAAI/bge-m3) ---
    embedding_url: str = Field(default="http://localhost:8090", alias="EMBEDDING_URL")
    embedding_model: str = Field(default="BAAI/bge-m3", alias="EMBEDDING_MODEL")
    embedding_dimension: int = Field(default=1024, alias="EMBEDDING_DIMENSION")
    embedding_batch_size: int = Field(default=32, alias="EMBEDDING_BATCH_SIZE")
    embedding_timeout: float = Field(default=120.0, alias="EMBEDDING_TIMEOUT")
    embedding_max_retries: int = Field(default=2, alias="EMBEDDING_MAX_RETRIES")

    # --- HTTP API ---
    paper_api_key: str = Field(default="change-me", alias="PAPER_API_KEY")
    paper_api_host: str = Field(default="0.0.0.0", alias="PAPER_API_HOST")
    paper_api_port: int = Field(default=8077, alias="PAPER_API_PORT")

    # --- rerank (SPEC-P1 section D2) ---
    #: Whether the embedding service exposes ``/rerank`` (server capability).
    rerank_enabled: bool = Field(default=True, alias="RERANK_ENABLED")
    rerank_model: str = Field(
        default="Xenova/ms-marco-MiniLM-L-6-v2", alias="RERANK_MODEL"
    )
    rerank_url: str = Field(default="http://127.0.0.1:8090", alias="RERANK_URL")
    rerank_timeout: float = Field(default=10.0, alias="RERANK_TIMEOUT")
    #: Candidate over-fetch factor before reranking (top_k * this).
    rerank_candidates: int = Field(default=5, alias="RERANK_CANDIDATES")

    # --- RRF fusion weights (SPEC-P1 section H2) ---
    #: Weight of the keyword (BM25) leg in RRF fusion. 1.0 == the classic
    #: unweighted RRF; 0.0 removes the leg from the fusion.
    rrf_keyword_weight: float = Field(default=1.0, alias="RRF_KEYWORD_WEIGHT")
    #: Weight of the semantic (kNN) leg in RRF fusion.
    rrf_semantic_weight: float = Field(default=1.0, alias="RRF_SEMANTIC_WEIGHT")

    # --- query rewrite (SPEC-P1 section I1) ---
    #: Off by default: when disabled the search path behaves exactly as before
    #: and never talks to the LLM endpoint.
    query_rewrite_enabled: bool = Field(default=False, alias="QUERY_REWRITE_ENABLED")
    query_rewrite_url: str = Field(default="", alias="QUERY_REWRITE_URL")
    query_rewrite_api_key: str = Field(default="", alias="QUERY_REWRITE_API_KEY")
    query_rewrite_model: str = Field(default="", alias="QUERY_REWRITE_MODEL")
    query_rewrite_timeout: float = Field(default=10.0, alias="QUERY_REWRITE_TIMEOUT")
    #: Hard cap on the rewritten query length (and the input length we rewrite).
    query_rewrite_max_chars: int = Field(default=300, alias="QUERY_REWRITE_MAX_CHARS")
    query_rewrite_target_language: str = Field(
        default="en", alias="QUERY_REWRITE_TARGET_LANGUAGE"
    )
    #: Token budget for one rewrite call. Reasoning models (deepseek-flash,
    #: o-series, ...) spend tokens on hidden ``reasoning_content`` first: with a
    #: 64-token cap the visible ``content`` came back empty and
    #: ``finish_reason="length"`` -- the rewrite silently degraded. 512 leaves
    #: room for the reasoning plus a one-line query.
    query_rewrite_max_tokens: int = Field(default=512, alias="QUERY_REWRITE_MAX_TOKENS")

    # --- search logging (SPEC-P1 section B) ---
    search_log_enabled: bool = Field(default=True, alias="SEARCH_LOG_ENABLED")
    search_log_results_limit: int = Field(default=20, alias="SEARCH_LOG_RESULTS_LIMIT")

    # --- ingestion limits (used from Phase 1 onwards) ---
    ingest_download_timeout: float = Field(default=120.0, alias="INGEST_DOWNLOAD_TIMEOUT")
    ingest_max_file_mb: int = Field(default=100, alias="INGEST_MAX_FILE_MB")
    #: How many ingestion pipelines may run at the same time (2026-09-19).
    #: Uploads beyond this limit wait in the in-process queue (stage ``QUEUED``)
    #: instead of piling onto the embedding server and OpenSearch at once.
    #: Keep it small: each pipeline streams a PDF, embeds ~40 chunks on 4 ORT
    #: threads and bulk-indexes into the same single-node OpenSearch.
    ingest_concurrency: int = Field(default=2, alias="INGEST_CONCURRENCY")

    # --- upload admission (2026-09-19, plan section 4) ---
    #: How many ``/ingest/files`` requests may be *in flight* at once. The excess
    #: is answered with ``429 + Retry-After`` instead of being buffered: the
    #: client has no concurrency knob, the server owns the decision.
    ingest_upload_concurrency: int = Field(default=2, alias="INGEST_UPLOAD_CONCURRENCY")
    #: Processing-backlog depth at which *multi-file* uploads are refused (429).
    #: Single-file uploads are exempt -- one human waiting is one job. 0 disables
    #: backlog throttling entirely.
    ingest_queue_high_watermark: int = Field(
        default=50, alias="INGEST_QUEUE_HIGH_WATERMARK"
    )
    #: Files per ``/ingest/files`` request (``422`` when exceeded).
    ingest_max_files_per_request: int = Field(
        default=20, alias="INGEST_MAX_FILES_PER_REQUEST"
    )
    #: Total bytes per ``/ingest/files`` request (``413`` when exceeded).
    ingest_max_request_mb: int = Field(default=200, alias="INGEST_MAX_REQUEST_MB")

    # --- server-side directory import (2026-09-19, plan section 3.2) ---
    #: Whitelist of roots ``/ingest/dir`` may read, separated by ``;`` or ``,``.
    #: **Empty means the endpoint is disabled** (404): reading the server's own
    #: filesystem is a new attack surface, so it is opt-in per deployment.
    ingest_local_roots: str = Field(default="", alias="INGEST_LOCAL_ROOTS")

    # --- archive import (2026-09-19, plan section 3.3) ---
    #: Size ceiling for the uploaded archive itself.
    ingest_archive_max_mb: int = Field(default=500, alias="INGEST_ARCHIVE_MAX_MB")
    #: Ceiling on extracted entries (zip bomb guard #1).
    ingest_archive_max_files: int = Field(default=2000, alias="INGEST_ARCHIVE_MAX_FILES")
    #: Ceiling on the total uncompressed size (zip bomb guard #2).
    ingest_archive_max_uncompressed_mb: int = Field(
        default=5000, alias="INGEST_ARCHIVE_MAX_UNCOMPRESSED_MB"
    )
    #: Ceiling on the uncompressed/compressed ratio (zip bomb guard #3).
    ingest_archive_max_ratio: int = Field(default=100, alias="INGEST_ARCHIVE_MAX_RATIO")
    #: Where archives are extracted (empty = the system temp directory).
    ingest_archive_tmp_dir: str = Field(default="", alias="INGEST_ARCHIVE_TMP_DIR")
    #: How long an extraction directory may survive after its jobs finished.
    ingest_archive_ttl_hours: int = Field(default=24, alias="INGEST_ARCHIVE_TTL_HOURS")

    # --- housekeeping (2026-09-19, plan section 5) ---
    #: Interval of the staging/extraction GC; it also runs once at startup.
    ingest_gc_interval_s: int = Field(default=300, alias="INGEST_GC_INTERVAL_S")

    # --- parsing backend (plan 2026-09-28_160551-docling-parser-backend §2 T4) ---
    #: ``pypdf`` (default) or ``docling``. docling is the new primary backend for
    #: two-column/tabled papers; pypdf stays the degradation backend *and* the
    #: default until the parser acceptance run passes (plan T8).
    parser_backend: str = Field(default="pypdf", alias="PARSER_BACKEND")
    #: Parses allowed to run at once. Keep at 1: docling is CPU-bound and its
    #: container is deliberately capped, so a second concurrent parse only makes
    #: both slower and walks into the memory ceiling the caps exist for.
    parser_concurrency: int = Field(default=1, alias="PARSER_CONCURRENCY")
    #: Cache parse artifacts in MinIO (plan T7). Off = always re-parse.
    parser_cache: bool = Field(default=True, alias="PARSER_CACHE")
    #: 0 = whole document; >0 parses only the first N pages (becomes docling's
    #: ``page_range`` and is recorded as a degradation).
    parser_max_pages: int = Field(default=0, alias="PARSER_MAX_PAGES")

    # --- docling-serve (plan §1.3 mapping table; every value below is measured,
    #     not guessed -- see the plan's §0.5/§0.6/§0.7/§0.8 notes) ---
    #: Base URL of docling-serve. Since 2026-09-29 it runs on the fnOS NAS
    #: (``http://192.168.31.53:8091``); the local WSL container is the rollback.
    #: **Empty disables the backend**: the client raises ``DoclingUnavailable``
    #: without dialing, so "no docling configured" degrades instead of timing out.
    docling_url: str = Field(default="http://127.0.0.1:8091", alias="DOCLING_URL")
    #: Client-side timeout. Must stay *above* ``DOCLING_DOCUMENT_TIMEOUT``: the
    #: server aborts the document itself, and a shorter client timeout would throw
    #: away a parse that was about to finish.
    docling_timeout: float = Field(default=660.0, alias="DOCLING_TIMEOUT")
    #: ``document_timeout`` form field. The server default is *no* deadline, which
    #: let a 5-page paper burn every core for 17+ minutes; always send one. 600s
    #: covers the worst measured case (formula-dense 7-page paper, 252s on the NAS).
    docling_document_timeout: float = Field(
        default=600.0, alias="DOCLING_DOCUMENT_TIMEOUT"
    )
    #: Retries for transient failures (5xx / timeout / transport error / empty
    #: body). 4xx and "200 with a non-empty errors list" are never retried.
    docling_max_retries: int = Field(default=1, alias="DOCLING_MAX_RETRIES")
    #: Send ``do_ocr=true``. The corpus is born-digital, and the server default is
    #: true -- so we switch it off explicitly (measured: OCR costs >50% throughput).
    docling_ocr: bool = Field(default=False, alias="DOCLING_OCR")
    #: ``accurate`` | ``fast``.
    docling_table_mode: str = Field(default="accurate", alias="DOCLING_TABLE_MODE")
    #: Formula LaTeX (plan decision 16). Expensive: 5.9s -> 39.2s on a 5-page
    #: paper locally, 252s on the worst formula-dense paper. When it fails the
    #: client retries once with formulas off and reports ``formulas=text``.
    docling_formula_enrichment: bool = Field(
        default=True, alias="DOCLING_FORMULA_ENRICHMENT"
    )
    #: Required *together with* ``do_formula_enrichment``: on its own the server
    #: answers 404 (``Preset 'default' not found for CodeFormulaVlmOptions``).
    #: ``granite_docling`` is the remote alternative and is not used here.
    docling_formula_preset: str = Field(
        default="codeformulav2", alias="DOCLING_FORMULA_PRESET"
    )
    #: Page-boundary marker. Both backends emit the same string, and the marker
    #: count is ``pages - 1`` (measured for every paper in the T2/T2d corpus).
    docling_page_break: str = Field(
        default="<!-- page-break -->", alias="DOCLING_PAGE_BREAK"
    )
    #: Image tag, used as the fallback parser version when ``GET /version`` is
    #: unreachable (the pinned tag is part of the artifact provenance).
    docling_image_tag: str = Field(default="", alias="DOCLING_IMAGE_TAG")

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("embedding_batch_size")
    @classmethod
    def _check_batch_size(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("EMBEDDING_BATCH_SIZE must be positive")
        return value

    @field_validator("rrf_keyword_weight", "rrf_semantic_weight")
    @classmethod
    def _check_rrf_weight(cls, value: float) -> float:
        # 0.0 disables a leg on purpose; negatives would invert the ranking.
        if value < 0:
            raise ValueError("RRF weights must be non-negative")
        return value

    @field_validator("ingest_concurrency")
    @classmethod
    def _check_ingest_concurrency(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("INGEST_CONCURRENCY must be positive")
        return value

    @field_validator("parser_backend")
    @classmethod
    def _check_parser_backend(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in PARSER_BACKENDS:
            raise ValueError(
                f"PARSER_BACKEND must be one of {sorted(PARSER_BACKENDS)}"
            )
        return normalized

    @field_validator("parser_concurrency")
    @classmethod
    def _check_parser_concurrency(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("PARSER_CONCURRENCY must be positive")
        return value

    @field_validator("parser_max_pages", "docling_max_retries")
    @classmethod
    def _check_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be zero or positive")
        return value

    @field_validator("docling_table_mode")
    @classmethod
    def _check_table_mode(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in {"accurate", "fast"}:
            raise ValueError("DOCLING_TABLE_MODE must be 'accurate' or 'fast'")
        return normalized

    @field_validator("query_rewrite_max_chars")
    @classmethod
    def _check_rewrite_max_chars(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("QUERY_REWRITE_MAX_CHARS must be positive")
        return value

    @model_validator(mode="after")
    def _check_docling_timeouts(self) -> "Settings":
        """The server must abort before the client gives up.

        ``document_timeout`` is what stops a runaway conversion, so a client
        timeout below it can only discard a parse that was about to finish (and
        on the NAS the worst measured paper needs 252s of the 600s budget).
        """
        if self.docling_timeout <= self.docling_document_timeout:
            raise ValueError(
                "DOCLING_TIMEOUT must be greater than DOCLING_DOCUMENT_TIMEOUT"
            )
        return self

    @model_validator(mode="after")
    def _check_query_rewrite(self) -> "Settings":
        """Fail fast instead of silently degrading to no-rewrite."""
        if not self.query_rewrite_enabled:
            return self
        missing = [
            name
            for name, value in (
                ("QUERY_REWRITE_URL", self.query_rewrite_url),
                ("QUERY_REWRITE_MODEL", self.query_rewrite_model),
                ("QUERY_REWRITE_API_KEY", self.query_rewrite_api_key),
            )
            if not str(value).strip()
        ]
        if missing:
            raise ValueError(
                "QUERY_REWRITE_ENABLED=true requires " + ", ".join(missing)
            )
        return self

    @property
    def database_url(self) -> str:
        """DSN used by SQLAlchemy / Alembic (psycopg 3 driver kept as-is)."""
        return self.postgres_dsn

    @property
    def local_roots(self) -> list[Path]:
        """Whitelisted roots for ``/ingest/dir`` (empty list = endpoint off)."""
        return parse_local_roots(self.ingest_local_roots)

    @property
    def archive_tmp_dir(self) -> Path:
        """Directory the archive service extracts into (system temp by default)."""
        raw = (self.ingest_archive_tmp_dir or "").strip()
        return Path(raw) if raw else Path(tempfile.gettempdir())


def parse_local_roots(value: str | None) -> list[Path]:
    """Split ``INGEST_LOCAL_ROOTS`` into normalized absolute paths.

    Accepts ``;``, ``,`` and ``os.pathsep`` as separators so the same value works
    on Windows and Linux. Every entry is ``realpath``-ed (symlinks and ``..``
    resolved) because the containment check in ``app.services.local_scan``
    compares real paths -- a whitelist entry that is itself a symlink would
    otherwise never match.
    """
    text = str(value or "")
    for separator in (";", ",", os.pathsep):
        text = text.replace(separator, "\n")
    roots: list[Path] = []
    seen: set[str] = set()
    for line in text.splitlines():
        entry = line.strip().strip('"').strip("'")
        if not entry:
            continue
        resolved = os.path.realpath(os.path.expanduser(entry))
        key = os.path.normcase(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(Path(resolved))
    return roots


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


settings = get_settings()
