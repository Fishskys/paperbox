"""Environment-driven configuration for paperbox.

All settings come from environment variables (see ``.env`` at the repository
root, which is git-ignored). Nothing is hard-coded: this machine runs the
infrastructure inside WSL2 (mirrored networking) while the app runs on Windows,
and the dependency ports are reachable through ``localhost`` as long as they are
allowed in WSL's ufw (see ``AGENTS.md`` §3).
"""

from __future__ import annotations

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


settings = get_settings()
