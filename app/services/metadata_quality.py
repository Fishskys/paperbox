"""Durable, idempotent automatic decisions; never mark them as human edits."""

import hashlib
import json
from contextvars import ContextVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MetadataQualityDecision, new_uuid
from app.services import metadata_policy as policy
from app.core.logging import get_logger

logger = get_logger(__name__)
preview_mode: ContextVar[bool] = ContextVar("metadata_quality_preview", default=False)


def record(session: Session, paper_id: str, field: str, payload: dict) -> None:
    payload = {**payload, "actor": "metadata_quality", "rule_version": policy.QUALITY_VERSION}
    identity = payload
    if payload.get("scope") == "candidate":
        # Intrinsic rejection remains the same after a later layer fills a blank.
        identity = {key: payload[key] for key in ("actor", "rule_version", "scope", "action", "reason", "candidate", "source_id", "evidence")}
    digest = hashlib.sha256(json.dumps([paper_id, field, identity], sort_keys=True, ensure_ascii=True, default=str).encode()).hexdigest()
    if session.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    inserted = session.execute(insert(MetadataQualityDecision).values(id=new_uuid(), paper_id=paper_id,
        field=field, rule_version=policy.QUALITY_VERSION, fingerprint=digest, payload=payload)
        .on_conflict_do_nothing(index_elements=[MetadataQualityDecision.fingerprint])
        .returning(MetadataQualityDecision.id)).scalar_one_or_none()
    if inserted is not None and not preview_mode.get():
        logger.info("PDF metadata quality decision", extra={"extra_fields": {"paper_id": paper_id, "field": field,
            "rule_version": policy.QUALITY_VERSION, "action": payload["action"], "reason": payload["reason"]}})


def for_paper(session: Session, paper_id: str) -> list[dict]:
    rows = session.scalars(select(MetadataQualityDecision).where(MetadataQualityDecision.paper_id == paper_id)
                           .order_by(MetadataQualityDecision.created_at.desc(), MetadataQualityDecision.id).limit(100)).all()
    return [{"id": row.id, "field": row.field, "rule_version": row.rule_version,
             "created_at": row.created_at, **row.payload} for row in rows]
