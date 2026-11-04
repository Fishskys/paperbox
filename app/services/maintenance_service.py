"""Durable maintenance orchestration; reports survive HTTP and process lifetimes."""

from datetime import datetime, timezone
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import IngestionJob, Paper, PaperFieldProvenance
from app.schemas.maintenance import MaintenanceIn
from app.services import ingestion_service as ingest, metadata_reextract, paper_service


class MaintenanceError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def get(session: Session, job_id: str) -> IngestionJob:
    job = ingest.get_job(session, job_id)
    if job is None or job.kind != "maintenance":
        raise MaintenanceError("maintenance job not found", 404)
    return job


def serialize(job: IngestionJob) -> dict:
    payload = job.payload or {}
    return {**ingest.serialize_job(job), "operation": payload["request"]["operation"],
            "dry_run": payload["request"]["dry_run"], "report_available": "report" in payload}


def revision(session: Session, paper: Paper) -> str:
    snapshot = paper_service.serialize_paper(paper)
    for key in ("created_at", "updated_at"):
        snapshot.pop(key, None)
    snapshot["current_claims"] = sorted(session.scalars(select(PaperFieldProvenance.id).where(
        PaperFieldProvenance.paper_id == paper.id, PaperFieldProvenance.is_current.is_(True))))
    encoded = TypeAdapter(dict).dump_python(snapshot, mode="json")
    return hashlib.sha256(json.dumps(encoded, sort_keys=True).encode()).hexdigest()


def request_job_id(actor: str, request_id) -> str:
    return str(uuid5(NAMESPACE_URL, f"paperbox:maintenance:{actor}:{request_id}"))


def submit(session: Session, body: MaintenanceIn, *, actor: str) -> IngestionJob:
    from app.workers import queue

    request = body.model_dump(mode="json", exclude={"request_id"})
    job_id = request_job_id(actor, body.request_id)
    existing = session.get(IngestionJob, job_id)
    if existing:
        if (existing.payload or {}).get("request") != request:
            raise MaintenanceError("request_id was already used for different parameters")
        if existing.stage in (ingest.STAGE_RECEIVED, ingest.STAGE_QUEUED):
            queue.submit(session, existing.id, "maintenance", queue.PRIORITY_BATCH)
        return existing
    payload = {"source_type": "maintenance", "request": request, "actor": actor}
    if body.operation in ("metadata", "reindex"):
        if body.dry_run:
            if body.paper_ids:
                ids = [str(value) for value in body.paper_ids]
                papers = session.scalars(select(Paper).where(Paper.id.in_(ids), Paper.deleted_at.is_(None))).all()
                found = {p.id: revision(session, p) for p in papers}
                payload["scope"] = {paper_id: found.get(paper_id, "missing") for paper_id in ids}
            else:
                papers = metadata_reextract.select_live_papers(session, paper_id=str(body.paper_id) if body.paper_id else None, limit=body.limit)
                payload["scope"] = {p.id: revision(session, p) for p in papers}
        else:
            preview = get(session, str(body.preview_job_id))
            previous = (preview.payload or {}).get("request", {})
            expected = {**request, "dry_run": True, "preview_job_id": None}
            if preview.stage != ingest.STAGE_COMPLETED or previous != expected or not previous.get("dry_run"):
                raise MaintenanceError("a completed matching preview is required")
            payload["scope"] = dict(preview.payload.get("scope") or {})
            if not payload["scope"]:
                raise MaintenanceError("preview scope is empty")
    job = IngestionJob(id=job_id, kind="maintenance", stage=ingest.STAGE_RECEIVED, progress=0, payload=payload)
    session.add(job)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return submit(session, body, actor=actor)
    return queue.submit(session, job_id, "maintenance", queue.PRIORITY_BATCH) or job


def persist(session: Session, job_id: str, *, progress: float, report=None, finished: bool = False):
    job = get(session, job_id)
    if report is not None:
        job.payload = {**job.payload, "report": TypeAdapter(dict).dump_python(report, mode="json")}
    job.progress = progress
    job.stage = ingest.STAGE_COMPLETED if finished else "PARSING"
    if finished:
        job.finished_at = datetime.now(timezone.utc)
    session.commit()


def execute(session: Session, job_id: str) -> None:
    """Run at most once. An interrupted apply needs inspection and a new preview."""
    job = get(session, job_id)
    if job.stage not in (ingest.STAGE_RECEIVED, ingest.STAGE_QUEUED):
        return
    payload = dict(job.payload)
    request = payload["request"]
    persist(session, job_id, progress=1)
    if request["operation"] == "consistency":
        from app.services import consistency_service
        report = consistency_service.check_consistency(limit=min(request["limit"], 1000),
            with_parser_papers=request["parser_papers"],
            on_progress=lambda value: persist(session, job_id, progress=value)).as_dict()
        persist(session, job_id, progress=100, report=report, finished=True)
        return
    report = metadata_reextract.ReextractSummary(dry_run=request["dry_run"]).as_api_dict()
    report["items"] = []
    report["queued"] = 0
    report["skipped_active"] = 0
    scope = payload.get("scope") or {}
    ordered = [(paper_id, scope[paper_id]) for paper_id in request["paper_ids"]] if request.get("paper_ids") else list(scope.items())
    for index, (paper_id, expected_revision) in enumerate(ordered):
        report["papers"] += 1
        try:
            session.expire_all()
            paper = session.get(Paper, paper_id)
            if paper is None or paper.deleted_at or revision(session, paper) != expected_revision:
                raise MaintenanceError("paper changed since preview; preview again")
            if ingest.count_running_jobs(session, paper_id):
                report["skipped_active"] += 1
                report["items"].append({"paper_id": paper_id, "title": paper.title, "status": "skipped_active_job"})
                persist(session, job_id, progress=5 + 90 * (index + 1) / len(scope), report=report)
                continue
            if request["operation"] == "reindex":
                record = paper_service.original_file(paper)
                item = {"paper_id": paper_id, "title": paper.title, "status": "ready" if request["dry_run"] else "queued"}
                if record is None:
                    report["skipped_no_file"] += 1
                    item["status"] = "skipped_no_file"
                elif not request["dry_run"]:
                    child = ingest.prepare_reindex_job(session, paper, record)
                    child.payload = {**child.payload, "maintenance_job_id": job_id, "ingest_actor": payload["actor"]}
                    item["job_id"] = child.id
                    item["status"] = "accepted"
                report["items"].append(item)
                persist(session, job_id, progress=5 + 90 * (index + 1) / len(scope), report=report)
                if item["status"] == "accepted":
                    from app.workers import queue
                    queue.submit(session, item["job_id"], queue.KIND_REINDEX, queue.PRIORITY_BATCH)
                    item["status"] = "queued"
                    report["queued"] += 1
                    persist(session, job_id, progress=5 + 90 * (index + 1) / len(scope), report=report)
                continue
            one = metadata_reextract.reextract_one(session, paper, apply=not request["dry_run"],
                refresh_index=request["refresh_index"], detail=True).as_api_dict()
            for key in ("changed_papers", "skipped_no_file", "failed", "index_documents", "index_pending"):
                report[key] += one[key]
            for key in ("added_fields", "added_identifiers", "quality_counts"):
                for name, count in one[key].items():
                    report[key][name] = report[key].get(name, 0) + count
            report["changes"].extend(one["changes"])
            report["items"].append({"paper_id": paper_id, "title": paper.title,
                "status": "skipped_no_file" if one["skipped_no_file"] else "ready" if request["dry_run"] and one["changed_papers"] else "unchanged" if not one["changed_papers"] else "completed"})
        except Exception as exc:
            session.rollback()
            report["failed"] += 1
            report["errors"].append({"paper_id": paper_id, "error": str(exc)})
            if report["items"] and report["items"][-1]["paper_id"] == paper_id:
                report["items"][-1]["error"] = str(exc)
            else:
                report["items"].append({"paper_id": paper_id, "status": "failed", "error": str(exc)})
        persist(session, job_id, progress=5 + 90 * (index + 1) / len(scope), report=report)
    persist(session, job_id, progress=100, report=report, finished=True)
