"""Effective retention policy and stable snapshot identities."""

from datetime import datetime, timezone
import hashlib
import json

from app.core.config import settings

SAFE_CATEGORIES = ("app_logs", "search_logs", "jobs", "parse_cache", "figure_history", "orphan_objects", "orphan_records")


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def policy():
    return {
        "automatic": settings.cleanup_auto_enabled,
        "automatic_categories": ["app_logs", "search_logs", "jobs", "figure_history", "orphan_records"],
        "interval_s": settings.cleanup_interval_s,
        "preview_hours": settings.cleanup_preview_hours,
        "batch_size": settings.cleanup_batch_size,
        "scan_limit": settings.cleanup_scan_limit,
        "days": {"app_logs": settings.cleanup_log_days, "search_logs": settings.cleanup_search_days,
            "jobs": settings.cleanup_job_days, "failed_jobs": settings.cleanup_failed_job_days,
            "parse_cache": settings.cleanup_cache_days, "figure_history": settings.cleanup_figure_days,
            "orphan_objects": 1, "orphan_records": settings.cleanup_job_days},
        "log_rotation": {"enabled": bool(settings.log_file), "max_bytes": settings.log_max_bytes, "backups": settings.log_backup_count},
        "upload_gc": {"interval_s": settings.ingest_gc_interval_s, "archive_hours": settings.ingest_archive_ttl_hours, "retry_hours": 72},
        "protected": ["metadata_sources", "field_history", "manual_decisions", "current_figures", "active_jobs", "idempotency_receipts"],
        "manual_only": ["container_images_and_build_cache", "uv_npm_model_cache", "multipart_uploads", "database_vacuum_and_wal", "backups", "unregistered_artifacts"],
    }


def signature():
    return digest({**policy(), "log_file": settings.log_file})
