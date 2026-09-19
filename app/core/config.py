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

    @field_validator("query_rewrite_max_chars")
    @classmethod
    def _check_rewrite_max_chars(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("QUERY_REWRITE_MAX_CHARS must be positive")
        return value

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
