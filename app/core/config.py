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

#: Accepted values of ``SEARCH_BACKEND`` (plan §7, M5). ``native`` = one ``hybrid``
#: request fused by the ``paperbox-rrf60`` search pipeline and collapsed to papers
#: (**deployed default**, ``app/search/native.py``); ``python`` = the two-leg
#: retriever whose legs are fused in-process. Both passed the M5 gate and the
#: python path is kept as the A/B baseline and the fallback. Only ``mode=hybrid``
#: is affected -- keyword/semantic are single-leg and always use the Python path.
SEARCH_BACKENDS = frozenset({"python", "native"})

#: Accepted values of ``MCP_TOOLSET``. Frozen at ``v1`` (docs/architecture/11-mcp-agent-interface.md
#: section 8): a breaking change to a tool schema ships as a new value, so a client
#: can pin the one it was written against.
MCP_TOOLSETS = frozenset({"v1"})

#: Accepted values of ``CHUNK_MODE``. Kept here rather than imported from
#: ``app.parsing.chunking``: that module imports ``app.core.logging``, which
#: imports this one, so the dependency has to point this way.
CHUNK_MODES = frozenset({"length", "semantic"})


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
    # `.env` 是唯一真源（AGENTS §3.4）；这里的默认值只是没配 .env 时的兜底，
    # 必须跟着当前物理索引走 —— v1/v2 已于 2026-09-30 drop。
    opensearch_index: str = Field(default="paper_chunks_v3", alias="OPENSEARCH_INDEX")
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
    #: Per-batch HTTP timeout. The embedding container serializes inference
    #: behind a FIFO queue (T7.3), so a request can legitimately wait for the
    #: ones ahead of it: the timeout must exceed the worst-case queue wait,
    #: otherwise normal backpressure shows up as a failed batch.
    embedding_timeout: float = Field(default=300.0, alias="EMBEDDING_TIMEOUT")
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

    # --- retrieval backend (plan §7, M5) ---
    #: Effective hybrid backend; a request may override it for one call
    #: (``SearchRequest.backend``), which is how the A/B drives both paths
    #: against the same process. An unknown value fails at startup, not at
    #: query time.
    search_backend: str = Field(default="native", alias="SEARCH_BACKEND")

    # --- MCP agent interface (2026-10-04) ---
    #: Contract: docs/architecture/11-mcp-agent-interface.md
    #: Mount the Streamable HTTP MCP endpoint at /mcp. Off by default on purpose:
    #: turning it on also requires an explicit host allowlist, so an existing
    #: deployment cannot be broken by an upgrade.
    mcp_enabled: bool = Field(default=False, alias="MCP_ENABLED")
    #: Comma separated ``Host`` allowlist for the Streamable HTTP transport.
    #: **No default**: the SDK default accepts ``127.0.0.1``/``localhost`` only and
    #: answers every other Host with a bare-text 421, so a LAN agent would look
    #: broken for no visible reason. Empty while ``MCP_ENABLED=true`` fails at
    #: startup (see ``_check_mcp``).
    mcp_allowed_hosts: str = Field(default="", alias="MCP_ALLOWED_HOSTS")
    #: Master switch for every writing tool; off means they are not even listed.
    mcp_write_enabled: bool = Field(default=False, alias="MCP_WRITE_ENABLED")
    #: Per-tool switches, all gated behind ``mcp_write_enabled`` as well.
    mcp_allow_delete: bool = Field(default=False, alias="MCP_ALLOW_DELETE")
    mcp_allow_metadata_write: bool = Field(
        default=False, alias="MCP_ALLOW_METADATA_WRITE"
    )
    mcp_allow_reindex: bool = Field(default=False, alias="MCP_ALLOW_REINDEX")
    #: Character budget for one tool result's body text, and the ceiling a caller
    #: may ask for. Asking above the ceiling is an error, never a silent clamp.
    mcp_max_chars: int = Field(default=8000, alias="MCP_MAX_CHARS")
    mcp_max_chars_ceiling: int = Field(default=32000, alias="MCP_MAX_CHARS_CEILING")
    #: How long a writing tool waits for its job before handing back a job id.
    #: Must stay below the client's per-tool timeout.
    mcp_wait_seconds: int = Field(default=120, alias="MCP_WAIT_SECONDS")
    #: Lifetime of the signed download URL returned by ``paper_get_file``.
    mcp_download_ttl_seconds: int = Field(default=300, alias="MCP_DOWNLOAD_TTL_SECONDS")
    #: Optional signing secret; empty derives one from ``PAPER_API_KEY``.
    mcp_download_secret: str = Field(default="", alias="MCP_DOWNLOAD_SECRET")
    #: Tool contract version (frozen at v1, see the contract doc section 8).
    mcp_toolset: str = Field(default="v1", alias="MCP_TOOLSET")
    #: Fallback download host for clients that do not send one through the request
    #: (stdio shells). Empty means "use the Host the agent reached us on".
    mcp_public_base_url: str = Field(default="", alias="MCP_PUBLIC_BASE_URL")
    #: ``name:key`` pairs (``;`` separated) that name the calling agent in the
    #: audit log. Empty falls back to ``PAPER_API_KEY`` (agent name ``default``).
    paper_api_keys: str = Field(default="", alias="PAPER_API_KEYS")

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
    #: Escape hatch for the inbound-URL safety gate (``app/services/net_guard.py``):
    #: comma-separated host names and/or CIDRs that may resolve to a non-public
    #: address. Empty = every private/loopback/link-local target is refused.
    ingest_allow_private_hosts: str = Field(
        default="", alias="INGEST_ALLOW_PRIVATE_HOSTS"
    )

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
    #: ``docling`` (default) or ``pypdf``. docling is the primary backend for
    #: two-column/tabled papers; pypdf stays the degradation backend and is used
    #: automatically whenever docling is unreachable, with the reason recorded in
    #: the degradation ledger. One key flips the whole pipeline back (plan §6.1).
    parser_backend: str = Field(default="docling", alias="PARSER_BACKEND")
    #: Parses allowed to run at once. Keep at 1: docling is CPU-bound and its
    #: container is deliberately capped, so a second concurrent parse only makes
    #: both slower and walks into the memory ceiling the caps exist for.
    parser_concurrency: int = Field(default=1, alias="PARSER_CONCURRENCY")
    #: Cache parse artifacts in MinIO (plan T7). Off = always re-parse.
    parser_cache: bool = Field(default=True, alias="PARSER_CACHE")
    #: 0 = whole document; >0 parses only the first N pages (becomes docling's
    #: ``page_range`` and is recorded as a degradation).
    parser_max_pages: int = Field(default=0, alias="PARSER_MAX_PAGES")

    # --- chunking (plan 2026-09-28_160551-docling-parser-backend §2 T7.2) ---
    #: Boundary policy of ``chunk_document``. ``length`` (default) grows a chunk
    #: until the next paragraph would push it past the target token count;
    #: ``semantic`` embeds the section's sentences with the retrieval model and
    #: cuts where the similarity dips. Semantic mode calls the embedding server
    #: during parsing; a failed call degrades that section to ``length`` and
    #: logs a warning, it never fails the ingestion job.
    chunk_mode: str = Field(default="length", alias="CHUNK_MODE")

    #: Semantic mode only: cosine below which a sentence boundary counts as a
    #: candidate cut (see ``chunking.SEMANTIC_SIMILARITY_THRESHOLD``). Deployed
    #: value is what the A/B in ``logs/eval/chunking`` picked.
    chunk_semantic_threshold: float = Field(
        default=0.80, alias="CHUNK_SEMANTIC_THRESHOLD"
    )
    #: Semantic mode only: a dip may cut only once the pending chunk has at least
    #: this many estimated tokens (``chunking.SEMANTIC_MIN_TOKENS``).
    chunk_semantic_min_tokens: int = Field(
        default=200, alias="CHUNK_SEMANTIC_MIN_TOKENS"
    )

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
    #: Formula LaTeX (plan decision 16) -- **off by default**: it is the single
    #: most expensive knob (measured 5.9s -> 39.2s on a 5-page paper, 252s on the
    #: worst formula-dense one, and ~310s for a full 5.9MB import against ~52s on
    #: the pypdf path). Deployments that need ``$$...$$`` turn it on together with
    #: ``DOCLING_FORMULA_PRESET`` and an image that carries the formula models.
    #: When it *is* on and the server fails on it, the client retries once with
    #: formulas off and reports ``formulas=text``. The value is part of the parse
    #: artifact cache identity (see ``parser_service.parse_options``), so
    #: flipping it re-parses instead of replaying a differently-shaped markdown.
    docling_formula_enrichment: bool = Field(
        default=False, alias="DOCLING_FORMULA_ENRICHMENT"
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

    @field_validator("search_backend")
    @classmethod
    def _check_search_backend(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in SEARCH_BACKENDS:
            raise ValueError(f"SEARCH_BACKEND must be one of {sorted(SEARCH_BACKENDS)}")
        return normalized

    @field_validator("parser_concurrency")
    @classmethod
    def _check_parser_concurrency(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("PARSER_CONCURRENCY must be positive")
        return value

    @field_validator("chunk_mode")
    @classmethod
    def _check_chunk_mode(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in CHUNK_MODES:
            raise ValueError(f"CHUNK_MODE must be one of {sorted(CHUNK_MODES)}")
        return normalized

    @field_validator("chunk_semantic_threshold")
    @classmethod
    def _check_chunk_semantic_threshold(cls, value: float) -> float:
        if not 0.0 < value <= 1.0:
            raise ValueError("CHUNK_SEMANTIC_THRESHOLD must be in (0, 1]")
        return value

    @field_validator(
        "parser_max_pages", "docling_max_retries", "chunk_semantic_min_tokens"
    )
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

    @model_validator(mode="after")
    def _check_mcp(self) -> "Settings":
        """MCP: never let the transport fall back to the SDK's localhost default.

        ``streamable_http_app()`` arms DNS-rebinding protection with a localhost
        allowlist, and answers every other Host with a bare-text 421 -- invisible
        from the client side. So enabling MCP without naming the hosts we serve is
        a startup error, not a runtime surprise.
        """
        if self.mcp_max_chars <= 0:
            raise ValueError("MCP_MAX_CHARS must be positive")
        if self.mcp_max_chars_ceiling < self.mcp_max_chars:
            raise ValueError("MCP_MAX_CHARS_CEILING must be >= MCP_MAX_CHARS")
        if self.mcp_wait_seconds < 0:
            raise ValueError("MCP_WAIT_SECONDS must be >= 0")
        if self.mcp_download_ttl_seconds <= 0:
            raise ValueError("MCP_DOWNLOAD_TTL_SECONDS must be positive")
        if self.mcp_toolset not in MCP_TOOLSETS:
            raise ValueError(f"MCP_TOOLSET must be one of {sorted(MCP_TOOLSETS)}")
        if self.mcp_enabled and not self.agent_keys and not self.paper_api_key:
            raise ValueError(
                "MCP_ENABLED=true needs a credential: set PAPER_API_KEYS (agent_name:key; "
                "...) or PAPER_API_KEY, otherwise every /mcp request is a 401"
            )
        if self.mcp_enabled and not self.mcp_allowed_host_list:
            raise ValueError(
                "MCP_ENABLED=true requires MCP_ALLOWED_HOSTS to list every host "
                "agents use (the LAN IP and the hostname, each as 'host' and "
                "'host:*'); the SDK default accepts localhost only and answers "
                "anything else with 421"
            )
        return self

    @property
    def mcp_allowed_host_list(self) -> list[str]:
        """Comma separated Host allowlist for the MCP transport (may be empty)."""
        return [item.strip() for item in self.mcp_allowed_hosts.split(",") if item.strip()]

    @property
    def agent_keys(self) -> dict[str, str]:
        """``agent name -> key`` from ``PAPER_API_KEYS`` (invalid entries dropped)."""
        pairs: dict[str, str] = {}
        for item in self.paper_api_keys.split(";"):
            name, separator, key = item.partition(":")
            name = name.strip()
            key = key.strip()
            if separator and name and key:
                pairs[name] = key
        return pairs

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
