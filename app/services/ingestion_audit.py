"""Append decision evidence to the existing durable job payload."""

from datetime import datetime, timezone

from app.core.logging import get_key_prefix, get_request_id
from app.db.models import IngestionJob, Paper, new_uuid
from app.services import paper_service


def identity(paper: Paper) -> dict:
    return {"paper_id": paper.id, "title": paper.title, "doi": paper.doi, "arxiv_id": paper.arxiv_id,
            "year": paper.year, "authors": paper_service.paper_author_names(paper), "fingerprint": paper.fingerprint,
            "file_sha256": sorted({f.sha256 for f in paper_service.live_files(paper) if f.sha256})}


def record(job: IngestionJob, *, kind: str, rule: str, target_paper_id: str,
           evidence: dict, file_action: dict) -> None:
    payload = dict(job.payload or {})
    events = list(payload.get("deduplication_audit") or [])
    events.append({"event_id": new_uuid(), "created_at": datetime.now(timezone.utc).isoformat(),
        "actor": payload.get("ingest_actor") or get_key_prefix() or "worker",
        "request_id": payload.get("ingest_request_id") or get_request_id(),
        "kind": kind, "rule": rule, "target_paper_id": target_paper_id,
        "evidence": evidence, "file_action": file_action})
    job.payload = {**payload, "deduplication_audit": events}
