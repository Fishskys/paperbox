"""Apply explicitly reviewed PDF conflicts with durable, reversible audit data.

This is an operator tool, not a new merge policy. Uncertain values must be
checked against the PDF before they are supplied to this service.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperFieldProvenance, PaperFile, PaperSource
from app.services import metadata_sources as sources
from app.services import paper_service, provenance_service as prov

IMPORTER = "scripts/reconcile_pdf_metadata.py"
RULE_VERSION = "pdf-quality-review-v1"


def apply_review(session: Session, decisions: list[dict[str, Any]], *, apply: bool = False) -> dict[str, Any]:
    """Validate a reviewed batch; apply under caller-owned transaction if asked.

    Locks serialize against normal metadata edits. Snapshot comparison refuses
    stale previews. Audit sources keep original claims, decisions, before/after,
    reasons and evidence; no historical claim is deleted or marked human.
    """
    report: dict[str, Any] = {"applied": apply, "reviewed": 0, "changed": 0, "already_applied": 0, "paper_ids": []}
    changed: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for decision in decisions:
        paper_id, field = decision["paper_id"], decision["field"]
        if field not in ("authors", "abstract") or (paper_id, field) in seen:
            raise ValueError("review must contain unique PDF author/abstract fields")
        seen.add((paper_id, field))
        value, reason = decision["value"], decision.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("every decision requires a reason")
        if field == "authors":
            if not isinstance(value, list) or not value or any(not isinstance(n, str) or not n.strip() for n in value):
                raise ValueError("authors must be a nonempty list of names")
            normalized = [paper_service.normalize_text(n) for n in value]
            if len(set(normalized)) != len(normalized):
                raise ValueError("duplicate authors in reviewed result")
            if not decision.get("evidence"):
                raise ValueError("author corrections require PDF evidence")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError("abstract must be nonempty text")
        payload = {**decision, "rule_version": RULE_VERSION, "actor": IMPORTER}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        ref = f"quality:{decision['provenance_id']}:{digest}"
        paper = session.execute(select(Paper).where(Paper.id == paper_id).with_for_update()).scalar_one_or_none()
        if paper is None or paper.deleted_at is not None:
            raise ValueError(f"paper no longer live: {paper_id}")
        evidence = decision.get("evidence")
        if isinstance(evidence, dict) and evidence.get("file_id"):
            file = session.get(PaperFile, evidence["file_id"])
            if file is None or file.paper_id != paper_id or not file.is_primary or file.sha256 != evidence.get("pdf_sha256"):
                raise ValueError(f"PDF evidence no longer matches primary file: {paper_id}")
        audit = session.execute(select(PaperSource).where(PaperSource.source_ref == ref, PaperSource.importer == IMPORTER)).scalar_one_or_none()
        current = session.execute(
            select(PaperFieldProvenance).where(
                PaperFieldProvenance.paper_id == paper_id,
                PaperFieldProvenance.field == field,
                PaperFieldProvenance.is_current.is_(True),
            ).with_for_update()
        ).scalar_one_or_none()
        if audit is not None:
            report["already_applied"] += 1
            # Retry also repairs an index refresh interrupted after PG commit.
            if audit.raw.get("changed") and current is not None and current.value == value:
                changed.add(paper_id)
            continue
        rejected = session.get(PaperFieldProvenance, decision["provenance_id"], with_for_update=True)
        if (current is None or current.id != decision["current_id"] or current.value != decision["kept"]
                or current.decision_at is not None or current.decided_by == prov.DECIDED_MANUAL
                or rejected is None or rejected.paper_id != paper_id or rejected.field != field
                or rejected.is_current or rejected.value != decision["rejected"]
                or rejected.decision_at is not None or rejected.decided_by in (prov.DECIDED_MANUAL, prov.DECIDED_DISMISSED, prov.DECIDED_QUALITY_REJECTED)):
            raise ValueError(f"stale or protected conflict: {paper_id}/{field}")
        kept_source = session.get(PaperSource, current.source_id) if current.source_id else None
        lost_source = session.get(PaperSource, rejected.source_id) if rejected.source_id else None
        if (kept_source is None or kept_source.source_type != sources.SOURCE_TYPE_PDF_HEURISTIC
                or lost_source is None or lost_source.source_type != sources.SOURCE_TYPE_PDF_EMBEDDED):
            raise ValueError("review is restricted to embedded versus heuristic PDF conflicts")
        duplicates = []
        for claim_id in decision.get("duplicate_provenance_ids", []):
            duplicate = session.get(PaperFieldProvenance, claim_id, with_for_update=True)
            duplicate_source = session.get(PaperSource, duplicate.source_id) if duplicate and duplicate.source_id else None
            if (duplicate is None or duplicate.id == rejected.id or duplicate.is_current
                    or duplicate.paper_id != paper_id or duplicate.field != field or duplicate.value != rejected.value
                    or duplicate.decision_at is not None
                    or duplicate.decided_by not in (prov.DECIDED_INITIAL, prov.DECIDED_STRUCTURED_OVERRIDE)
                    or duplicate_source is None or duplicate_source.source_type != sources.SOURCE_TYPE_PDF_EMBEDDED):
                raise ValueError("duplicate claim is stale, protected or not identical")
            duplicates.append(duplicate)
        materialized = prov.read_field(paper, field)
        if materialized != decision.get("materialized_kept", decision["kept"]):
            raise ValueError(f"paper/provenance mismatch: {paper_id}/{field}")
        differs = value != current.value or value != materialized
        report["reviewed"] += 1
        report["changed"] += int(differs)
        if differs:
            changed.add(paper_id)
        if not apply:
            continue
        audit = sources.upsert_source(
            session, source_type=sources.SOURCE_TYPE_PDF_HEURISTIC,
            source_ref=ref, paper_id=paper_id, importer=IMPORTER,
            match_status=sources.MATCH_STATUS_MATCHED,
            raw={**payload, "changed": differs, "materialized_before": materialized,
                 "reviewed_at": datetime.now(timezone.utc).isoformat(),
                 "original_current_source_id": current.source_id, "original_rejected_source_id": rejected.source_id,
                 "duplicate_claims": [{"id": row.id, "source_id": row.source_id, "decided_by": row.decided_by} for row in duplicates],
                 "original_current_decider": current.decided_by, "original_rejected_decider": rejected.decided_by},
        )
        rejected.decided_by = prov.DECIDED_QUALITY_REJECTED
        for duplicate in duplicates:
            duplicate.decided_by = prov.DECIDED_QUALITY_REJECTED
        if differs:
            if value != current.value:
                current.decided_by = prov.DECIDED_QUALITY_REJECTED
            prov.set_field(session, paper, field, value, source_id=audit.id,
                           decided_by=prov.DECIDED_STRUCTURED_OVERRIDE, override=True)
        audit.raw = {**audit.raw, "materialized_after": prov.read_field(paper, field)}
        session.flush()
    report["paper_ids"] = sorted(changed)
    return report


def refresh_authors(session: Session, paper_ids: list[str]) -> dict[str, int]:
    """Refresh only author keywords; do not re-embed or rewrite chunk text."""
    from app.db.models import PaperChunk
    from app.search import opensearch

    updates = []
    for paper_id in paper_ids:
        paper = paper_service.get_paper(session, paper_id)
        if paper is None:
            continue
        ids = session.scalars(select(PaperChunk.id).where(PaperChunk.paper_id == paper_id)).all()
        if not ids:
            continue
        updates.extend([
            {"chunk_id": str(cid), "doc": {"authors": paper_service.paper_author_names(paper)}} for cid in ids
        ])
    return opensearch.bulk_update_documents(updates) if updates else {"updated": 0, "failed": 0}
