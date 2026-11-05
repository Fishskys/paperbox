"""Bounded, frozen previews and explicitly confirmed administrative cleanup."""

from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.models import CleanupCandidate, CleanupReceipt, IngestionJob, Paper
from app.schemas.cleanup import CleanupIn
from app.services import cleanup_indices, cleanup_policy as policy, ingestion_service as ingest, operation_receipts
from app.services.cleanup_resources import Resources


class CleanupError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def request_job_id(actor, request_id):
    return str(uuid5(NAMESPACE_URL, f"paperbox:cleanup:{actor}:{request_id}"))


def get(session, identifier):
    job = session.get(IngestionJob, str(identifier))
    if job is None or job.kind != "cleanup":
        receipt = session.get(CleanupReceipt, str(identifier))
        raise CleanupError("cleanup report expired" if receipt and receipt.kind == "cleanup" else "cleanup task not found",
            410 if receipt and receipt.kind == "cleanup" else 404)
    return job


def serialize(job):
    request = job.payload["request"]
    return {**ingest.serialize_job(job), "operation": request["operation"], "dry_run": request["dry_run"],
        "report_available": bool(job.payload.get("report_available")), "request": scope(request)}


def scope(request):
    return {key: value for key, value in request.items() if key not in ("dry_run", "preview_job_id", "retry_job_id")}


def valid_source(session, request):
    source = get(session, request.get("preview_job_id") or request["retry_job_id"])
    payload = source.payload
    previous = payload["request"]
    if scope(previous) != scope(request) or previous["operation"] != "cleanup":
        raise CleanupError("preview parameters do not match")
    expires = payload.get("expires_at")
    if not expires or datetime.fromisoformat(expires) <= policy.now() or payload.get("policy_signature") != policy.signature():
        raise CleanupError("preview expired or policy changed; preview again")
    if request.get("preview_job_id"):
        if source.stage != "COMPLETED" or not previous["dry_run"] or payload.get("errors"):
            raise CleanupError("a successful completed preview is required")
    elif source.stage != "FAILED" or previous["dry_run"]:
        raise CleanupError("only a failed execution can be resumed")
    return source


def submit(session, body: CleanupIn, *, actor, enqueue=True):
    from app.workers import queue

    request = body.model_dump(mode="json", exclude={"request_id"})
    identifier = request_job_id(actor, body.request_id)
    existing = session.get(IngestionJob, identifier)
    receipt = session.get(CleanupReceipt, identifier)
    if receipt and receipt.request_hash != policy.digest(request):
        raise CleanupError("request_id already used with different parameters")
    if existing:
        if existing.kind != "cleanup" or existing.payload["request"] != request:
            raise CleanupError("request_id already used")
        if enqueue and existing.stage in ("RECEIVED", "QUEUED"):
            queue.submit(session, identifier, "cleanup", queue.PRIORITY_BATCH)
        return existing
    if receipt:
        raise CleanupError("task expired; this request cannot be replayed", 410)
    source = None if body.dry_run else valid_source(session, request)
    payload = {"source_type": "cleanup", "actor": actor, "request": request, "policy_signature": policy.signature(),
        "expires_at": (policy.now() + timedelta(hours=policy.policy()["preview_hours"])).isoformat() if source is None else source.payload["expires_at"],
        "errors": [], "limited": False, "report_available": False}
    job = IngestionJob(id=identifier, kind="cleanup", stage="RECEIVED", progress=0, payload=payload)
    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return submit(session, body, actor=actor, enqueue=enqueue)
    if source:
        rows = session.scalars(select(CleanupCandidate).where(CleanupCandidate.job_id == source.id)).all()
        for row in rows:
            if body.retry_job_id and row.state in ("completed", "skipped", "protected"):
                continue
            session.add(CleanupCandidate(job_id=identifier, category=row.category, resource_key=row.resource_key,
                revision=row.revision, detail=dict(row.detail), size_bytes=row.size_bytes,
                state="protected" if row.state == "protected" else "ready", error=row.error if row.state == "protected" else None))
    operation_receipts.remember(session, job)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return submit(session, body, actor=actor, enqueue=enqueue)
    return (queue.submit(session, identifier, "cleanup", queue.PRIORITY_BATCH) or job) if enqueue else job


def report(session, identifier, *, offset=0, limit=100):
    job = get(session, identifier)
    groups = session.execute(select(CleanupCandidate.state, func.count(), func.sum(CleanupCandidate.size_bytes),
        func.count(CleanupCandidate.size_bytes)).where(CleanupCandidate.job_id == job.id).group_by(CleanupCandidate.state)).all()
    counts = {state: count for state, count, _, _ in groups}
    total = sum(counts.values())
    known = sum(known for _, _, _, known in groups)
    rows = session.scalars(select(CleanupCandidate).where(CleanupCandidate.job_id == job.id).order_by(
        CleanupCandidate.category, CleanupCandidate.resource_key).offset(offset).limit(limit)).all()
    return {"job_id": job.id, "operation": job.payload["request"]["operation"], "dry_run": job.payload["request"]["dry_run"],
        "expires_at": job.payload["expires_at"], "total": total,
        **{key: counts.get(key, 0) for key in ("ready", "protected", "completed", "skipped", "failed")},
        "size_bytes": sum(size or 0 for _, _, size, _ in groups) if known == total else None,
        "limited": job.payload.get("limited", False), "errors": job.payload.get("errors", []),
        "items": [{key: getattr(row, key) for key in ("id", "category", "resource_key", "size_bytes", "state", "error", "detail")} for row in rows]}


def _finish(session, job, *, failed=False):
    job.stage = "FAILED" if failed else "COMPLETED"
    job.progress = 100
    job.finished_at = policy.now()
    job.payload = {**job.payload, "report_available": True}
    if failed:
        job.error_code = "CLEANUP_FAILED"
        job.error_message = "Some resources could not be cleaned; inspect the report before resuming."
    operation_receipts.remember(session, job)
    session.commit()


def execute(session, identifier, *, resources=None):
    job = session.execute(select(IngestionJob).where(IngestionJob.id == identifier).with_for_update()).scalar_one()
    if job.kind != "cleanup":
        raise CleanupError("not a cleanup task")
    if job.stage not in ("RECEIVED", "QUEUED"):
        return
    resources = resources or Resources(session)
    job.stage, job.started_at, job.progress = "PARSING", policy.now(), 1
    session.commit()
    if job.payload["request"]["dry_run"]:
        for category in job.payload["request"]["categories"]:
            try:
                count = protected = scanned = 0
                for item in resources.scan(category, job.payload["request"], policy.now()):
                    scanned += 1
                    if scanned > policy.policy()["scan_limit"] or count >= policy.policy()["batch_size"]:
                        job.payload = {**job.payload, "limited": True}
                        break
                    if item["state"] == "protected":
                        protected += 1
                        if protected > 20:
                            job.payload = {**job.payload, "limited": True}
                            continue
                    else:
                        count += 1
                    session.add(CleanupCandidate(job_id=job.id, **item))
                session.commit()
            except Exception as exc:
                session.rollback()
                job.payload = {**job.payload, "errors": [*job.payload.get("errors", []), f"{category}: {type(exc).__name__}: {exc}"]}
                session.commit()
        _finish(session, job, failed=bool(job.payload.get("errors")))
        return
    # External deletion may be interrupted after a successful side effect. The
    # persisted working state permits idempotent retry of the frozen manifest.
    candidates = session.scalars(select(CleanupCandidate).where(CleanupCandidate.job_id == job.id,
        CleanupCandidate.state == "ready").order_by(CleanupCandidate.id)).all()
    for candidate in candidates:
        candidate.state = "working"
        candidate.detail = {**candidate.detail, "started": True}
        session.commit()
        try:
            with cleanup_indices.exclusive(session):
                if datetime.fromisoformat(job.payload["expires_at"]) <= policy.now() or job.payload["policy_signature"] != policy.signature():
                    raise ValueError("preview expired or policy changed")
                resources.remove(candidate)
                candidate.state, candidate.error = "completed", None
                session.commit()
        except ValueError as exc:
            session.rollback()
            candidate.state, candidate.error = "skipped", str(exc)
            session.commit()
        except Exception as exc:
            session.rollback()
            candidate.state, candidate.error = "failed", f"{type(exc).__name__}: {exc}"
            session.commit()
    failed = session.scalar(select(func.count()).select_from(CleanupCandidate).where(CleanupCandidate.job_id == job.id,
        CleanupCandidate.state.in_(("failed", "working"))))
    _finish(session, job, failed=bool(failed))


def list_jobs(session, *, offset=0, limit=20):
    query = select(IngestionJob).where(IngestionJob.kind == "cleanup")
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    jobs = session.scalars(query.order_by(IngestionJob.created_at.desc()).offset(offset).limit(limit))
    return {"total": total, "jobs": [serialize(job) for job in jobs]}


def deleted_papers(session, *, offset=0, limit=20):
    query = select(Paper).where(Paper.deleted_at.is_not(None))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(Paper.deleted_at).offset(offset).limit(limit))
    return {"total": total, "papers": [{"id": row.id, "title": row.title, "deleted_at": row.deleted_at} for row in rows]}
