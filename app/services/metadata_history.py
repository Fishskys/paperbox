"""Evidence-backed, field-scoped reconciliation of historical PDF claims."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperFieldProvenance, PaperFile, PaperSource, new_uuid
from app.parsing.pdf import PageText, normalize_page_text
from app.parsing.mupdf import invoke
from app.parsing.metadata_fields import authors_problem
from app.services import metadata_policy as policy, metadata_quality, metadata_reextract
from app.services import metadata_sources, object_storage, paper_service, provenance_service as prov

IMPORTER = "scripts/reconcile_pdf_quality_history.py"
FIELDS = ("authors", "abstract")


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, default=str).encode()).hexdigest()


def _claim(row: PaperFieldProvenance) -> dict:
    return {"id": row.id, "value": deepcopy(row.value), "source_id": row.source_id,
            "decided_by": row.decided_by, "decision_at": row.decision_at.isoformat() if row.decision_at else None}


def _protected(row: PaperFieldProvenance) -> bool:
    return row.decision_at is not None or row.decided_by in (prov.DECIDED_MANUAL, prov.DECIDED_DISMISSED)


def snapshots(session: Session, candidates: list[dict] | None = None) -> list[dict]:
    """Capture immutable preview guards; callers may enforce a read-only transaction."""
    if candidates is None:
        candidates, total = prov.conflict_page(session, limit=200)
        for offset in range(200, total, 200):
            candidates.extend(prov.conflict_page(session, limit=200, offset=offset)[0])
    result = []
    seen = set()
    for candidate in candidates:
        key = (candidate["paper_id"], candidate["field"])
        if key in seen or key[1] not in FIELDS:
            continue
        seen.add(key)
        paper = paper_service.get_paper(session, key[0])
        current = prov.current_claim(session, *key)
        if paper is None or current is None or _protected(current):
            continue
        source = session.get(PaperSource, current.source_id) if current.source_id else None
        if source is None or source.source_type not in policy.PDF_SOURCES:
            continue
        history = []
        for row in prov.field_history(session, *key):
            if row.is_current or _protected(row) or row.decided_by == prov.DECIDED_QUALITY_REJECTED:
                continue
            origin = session.get(PaperSource, row.source_id) if row.source_id else None
            if origin and origin.source_type in policy.PDF_SOURCES:
                history.append({**_claim(row), "source_type": origin.source_type})
        record = paper_service.original_file(paper)
        result.append({"paper_id": paper.id, "title": paper.title, "url": paper.url, "field": key[1],
                       "current": {**_claim(current), "source_type": source.source_type}, "history": history,
                       "materialized": deepcopy(prov.read_field(paper, key[1])),
                       "file": {"id": record.id, "object_key": record.object_key, "bucket": record.bucket,
                                "sha256": record.sha256} if record else None})
    return result


def extract(snapshot: dict, *, download=None) -> dict:
    """Download an immutable PDF and extract evidence without any database writes."""
    record = snapshot["file"]
    if not record:
        return {"error": "missing_primary_file"}
    pdf = (download or object_storage.download_bytes)(record["object_key"], bucket=record["bucket"])
    digest = hashlib.sha256(pdf).hexdigest()
    if record["sha256"] and record["sha256"] != digest:
        raise ValueError("stored PDF checksum differs from the file registry")
    bundle = invoke("metadata_bundle", pdf)
    pages = [PageText(page=item["page"], text=normalize_page_text(item["text"])) for item in bundle["pages"]]
    embedded, body, evidence = metadata_reextract.pdf_candidates(
        pages, pdf_bytes=pdf, title=snapshot["title"], url=snapshot["url"], bundle=bundle)
    return {"embedded": embedded.as_dict(), "body": body or {},
            "quality": {**evidence, "pdf_sha256": digest, "primary_file_id": record["id"]},
            "first_pages": [{"page": page.page, "text": page.text} for page in pages[:2]]}


def _evidence(snapshot: dict, extracted: dict) -> dict:
    evidence = dict(extracted.get("quality") or {})
    before, incoming = snapshot["materialized"], extracted.get("body", {}).get("authors")
    if snapshot["field"] == "authors" and incoming and not authors_problem(before, title=snapshot["title"]):
        def markers(name):
            return set(re.findall(r"\b([^\W\d_]{2,})\.", name.casefold()))
        suspicious = len(before) == len(incoming) and any(
            (new.endswith(("'", "’", "‘", '"', "“", "”")) and not old.endswith(("'", "’", "‘", '"', "“", "”")))
            or markers(new) - markers(old)
            or len(re.findall(r"[^\W\d_]+", new)) < len(re.findall(r"[^\W\d_]+", old))
            for old, new in zip(before, incoming))
        if not (policy.byline_spelling_variants(before, incoming) or policy.byline_abbreviations(before, incoming)) or suspicious:
            evidence["byline_complete"] = False
            evidence["materialized_byline_conflict"] = True
    return evidence


def plan(snapshot: dict, extracted: dict) -> dict:
    """Recompute decisions from evidence; never trust a preselected replacement."""
    field = snapshot["field"]
    if field not in FIELDS:
        raise ValueError("history reconciliation is limited to PDF authors and abstract")
    value = snapshot["materialized"]
    source_type = snapshot["current"]["source_type"]
    evidence = _evidence(snapshot, extracted)
    replacement_reason = None
    incoming = extracted.get("body", {}).get(field)
    if incoming and evidence.get("text_state") == "ok" and not policy.equivalent_values(field, value, incoming):
        choice = policy.pdf_choice(field, value, incoming, source_type, "pdf_heuristic",
                                   title=snapshot["title"], evidence=evidence)
        if choice and choice[0] == "incoming":
            value, source_type, replacement_reason = incoming, "pdf_heuristic", choice[1]
    decisions = []
    candidates = list(snapshot["history"])
    if not policy.equivalent_values(field, value, snapshot["current"]["value"]):
        candidates.append(snapshot["current"])
    for candidate in candidates:
        if policy.equivalent_values(field, value, candidate["value"]):
            action, reason = "archive", "equivalent_author_typography" if field == "authors" else "equivalent_value"
        else:
            choice = policy.pdf_choice(field, value, candidate["value"], source_type, candidate["source_type"],
                                       title=snapshot["title"], evidence=evidence)
            action = "review" if policy.needs_review(field, value, candidate["value"], source_type,
                candidate["source_type"], title=snapshot["title"], evidence=evidence) else "archive"
            reason = choice[1] if choice else "unverified_source"
        decisions.append({"id": candidate["id"], "action": action, "reason": reason})
    return {"selected": value, "selected_source_type": source_type, "replacement_reason": replacement_reason,
            "changed": value != snapshot["materialized"], "decisions": decisions,
            "pending": any(row["action"] == "review" for row in decisions),
            "text_state": evidence.get("text_state", "missing_evidence"), "error": extracted.get("error")}


def apply(session: Session, snapshot: dict, extracted: dict, expected: dict) -> dict:
    """Apply a guarded preview in the caller's transaction, preserving all history."""
    decision = plan(snapshot, extracted)
    if decision != expected:
        raise ValueError("preview no longer matches the current quality rules")
    if decision["error"]:
        return {"status": "skipped", "reason": decision["error"]}
    evidence = _evidence(snapshot, extracted)
    if evidence.get("rule_version") != policy.QUALITY_VERSION:
        raise ValueError("preview uses a different quality rule version")
    ref = f"pdf-quality-history:{fingerprint([policy.QUALITY_VERSION, snapshot, extracted])}"
    source_type = decision["selected_source_type"]
    existing = metadata_sources.find_source(session, source_type, ref)
    if existing:
        return {"status": "already_applied", "changed": existing.raw["changed"], "pending": existing.raw["pending"]}
    paper = session.get(Paper, snapshot["paper_id"], with_for_update=True)
    current = prov.current_claim(session, snapshot["paper_id"], snapshot["field"])
    if current:
        session.refresh(current, with_for_update=True)
    if (paper is None or paper.deleted_at is not None or paper.title != snapshot["title"]
            or current is None or _claim(current) != {k: v for k, v in snapshot["current"].items() if k != "source_type"}
            or _protected(current)):
        raise ValueError("stale or human-protected current metadata")
    origin = session.get(PaperSource, current.source_id)
    if origin is None or origin.source_type != snapshot["current"]["source_type"] or origin.source_type not in policy.PDF_SOURCES:
        raise ValueError("current source is no longer a PDF source")
    record = session.get(PaperFile, snapshot["file"]["id"], with_for_update=True)
    if (record is None or not record.is_primary or record.paper_id != paper.id
            or any(getattr(record, key) != snapshot["file"][key] for key in ("object_key", "bucket", "sha256"))
            or evidence.get("primary_file_id") != record.id
            or not evidence.get("pdf_sha256")
            or record.sha256 and record.sha256 != evidence["pdf_sha256"]):
        raise ValueError("primary PDF changed after preview")
    if prov.read_field(paper, snapshot["field"]) != snapshot["materialized"]:
        raise ValueError("materialized metadata changed after preview")
    claims = {current.id: current}
    for candidate in snapshot["history"]:
        row = session.get(PaperFieldProvenance, candidate["id"], with_for_update=True)
        if (row is None or row.is_current or row.paper_id != paper.id or row.field != snapshot["field"]
                or _protected(row) or _claim(row) != {k: v for k, v in candidate.items() if k != "source_type"}):
            raise ValueError("historical candidate changed after preview")
        candidate_source = session.get(PaperSource, row.source_id)
        if (candidate_source is None or candidate_source.source_type != candidate["source_type"]
                or candidate_source.source_type not in policy.PDF_SOURCES):
            raise ValueError("historical source changed after preview")
        claims[row.id] = row
    source = metadata_sources.upsert_source(session, source_type=source_type, source_ref=ref,
        paper_id=paper.id, importer=IMPORTER, match_status=metadata_sources.MATCH_STATUS_MATCHED,
        raw={"quality": evidence, "snapshot": snapshot, **decision})
    # Append an evidence-bearing current claim even when its spelling is unchanged.
    # record_claim intentionally reuses identical values, so this refresh is explicit.
    selected = PaperFieldProvenance(id=new_uuid(), paper_id=paper.id, source_id=source.id,
        field=snapshot["field"], value=decision["selected"], is_current=False,
        decided_by=prov.DECIDED_STRUCTURED_OVERRIDE, confidence=current.confidence)
    session.add(selected)
    session.flush()
    prov.promote(session, selected)
    if decision["changed"]:
        prov.write_field(session, paper, snapshot["field"], decision["selected"], override=True, source_id=source.id)
    for row in decision["decisions"]:
        if row["action"] == "archive":
            claims[row["id"]].decided_by = prov.DECIDED_QUALITY_REJECTED
    source.raw = {**source.raw, "applied_current_id": selected.id}
    metadata_quality.record(session, paper.id, snapshot["field"], {
        "action": "conflict" if decision["pending"] else "resolved", "reason": decision["replacement_reason"] or "historical_pdf_evidence",
        "before": snapshot["materialized"], "candidate": extracted.get("body", {}).get(snapshot["field"]),
        "selected": decision["selected"], "previous_claim_id": current.id, "source_id": source.id,
        "evidence": evidence, "historical_decisions": decision["decisions"]})
    session.flush()
    return {"status": "applied", "changed": decision["changed"], "pending": decision["pending"],
            "archived": sum(row["action"] == "archive" for row in decision["decisions"])}
