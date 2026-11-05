"""Small idempotency tombstones survive task/report and paper removal."""

from app.db.models import CleanupReceipt
from app.services.cleanup_policy import digest


def remember(session, job):
    payload = job.payload or {}
    request = payload.get("request") or {}
    row = session.get(CleanupReceipt, job.id)
    if row is None:
        row = CleanupReceipt(id=job.id, kind=job.kind, actor=payload.get("actor", payload.get("ingest_actor", "unknown")),
            request_hash=digest(request), summary={})
        session.add(row)
    row.summary = {"operation": request.get("operation"), "dry_run": request.get("dry_run", True), "stage": job.stage}
    return row
