#!/usr/bin/env python3
"""Real PostgreSQL/OpenSearch acceptance in a temporary schema and index.

uv run python scripts/acceptance_pdf_metadata_quality.py --report logs/eval/pdf-quality-acceptance.json

No library rows, original PDFs or production search documents are modified.
Temporary schema/index cleanup runs even on failure; this never calls embeddings.
"""

from __future__ import annotations

import argparse
from functools import partial
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402
import httpx  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.schema import CreateSchema, DropSchema  # noqa: E402

from app.db.models import Base, MetadataIndexUpdate, MetadataQualityDecision, Paper, PaperChunk, PaperFile, PaperSource, new_uuid  # noqa: E402
from app.db.session import get_engine  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.parsing.pdf import extract_pages  # noqa: E402
from app.search import opensearch  # noqa: E402
from app.services import metadata_index_sync, metadata_manual, metadata_reextract, paper_service  # noqa: E402
from app.services import metadata_history, metadata_import, metadata_merge, metadata_source_review, metadata_sources, provenance_service  # noqa: E402


def check_source_review(session: Session) -> dict:
    paper = Paper(id=new_uuid(), title="Synthetic Source Review", fingerprint=new_uuid(), status="PENDING", year=2020)
    session.add(paper)
    session.flush()
    paper_service.set_paper_authors(session, paper, ["Alex Reviewer"])

    def source(raw, source_type="import_file"):
        parsed = metadata_import.parse_records([raw])[0]
        return metadata_sources.upsert_source(session, source_type=source_type, source_ref=parsed.source_ref,
            raw=raw, match_status="ambiguous")

    raw = {"title": paper.title, "authors": ["A. Reviewer"], "year": 2020, "doi": "10.5555/source-review"}
    row = source(raw)
    preview = metadata_source_review.rematch(session, row.id, actor="acceptance")
    assert not preview["applied"] and row.paper_id is None
    metadata_source_review.rematch(session, row.id, actor="acceptance", apply=True,
        revision=preview["revision"], expected_paper_id=paper.id)
    session.commit()
    assert row.paper_id == paper.id and row.raw == raw
    rejected = source({"title": "Synthetic Unrelated Source", "doi": "10.5555/source-rejected"})
    before = metadata_source_review.detail(session, rejected.id)
    after = metadata_source_review.reject(session, rejected.id, actor="acceptance", revision=before["revision"])
    assert metadata_import.import_payload(session, [rejected.raw], apply=True).unchanged == 1
    metadata_source_review.restore(session, rejected.id, actor="acceptance", revision=after["revision"])
    assert rejected.match_status == "pending"
    confirmed = source(raw, "crossref")
    snapshot = metadata_source_review.detail(session, confirmed.id)
    batch = metadata_source_review.rematch_batch(session, [
        {"source_id": confirmed.id, "revision": snapshot["revision"], "expected_paper_id": paper.id},
        {"source_id": rejected.id, "revision": before["revision"]},
    ], actor="acceptance", apply=True)
    assert batch["applied"] == 1 and batch["items"][1]["status"] == 409
    independent = source({**raw, "doi": "10.5555/source-independent"})
    snapshot = metadata_source_review.detail(session, independent.id)
    result = metadata_source_review.create_paper(session, independent.id, actor="acceptance", revision=snapshot["revision"])
    session.commit()
    shell = session.get(Paper, result["created_paper_id"])
    assert shell.status == "AWAITING_FILE" and shell.files == []
    assert independent.raw["doi"] == "10.5555/source-independent" and paper.doi == "10.5555/source-review"
    assert metadata_source_review.detail(session, row.id)["history"][0]["actor"] == "acceptance"
    return {"source_rematch_preview_and_apply": True, "source_rejection_restore": True,
            "source_batch_partial_failure": True, "source_independent_admission": True,
            "source_raw_and_audit_preserved": True}


def check_external_import(session: Session) -> dict:
    paper = Paper(id=new_uuid(), title="Synthetic External Metadata Matching", fingerprint=new_uuid(), status="PENDING")
    session.add(paper)
    session.flush()
    source = metadata_sources.upsert_source(session, paper_id=paper.id, source_type="pdf_embedded",
        source_ref=f"acceptance:{paper.id}", raw={}, match_status="matched")
    metadata_merge.merge_values(session, paper, {"title": paper.title, "authors": ["G. Avolio"], "year": 2020,
        "identifier:doi": "10.1234/acceptance-incorrect"}, source_type="pdf_embedded", source_id=source.id)
    metadata_merge.merge_values(session, paper, {"authors": ["Someone Else"]}, source_type="pdf_heuristic",
        evidence={"text_state": "ok", "byline_complete": False})
    session.commit()
    assert paper_service.paper_author_names(paper) == ["G. Avolio"]
    assert provenance_service.conflict_page(session)[1] == 0
    record = {"title": paper.title, "authors": ["Giuseppe Avolio"], "year": 2020,
              "doi": "10.1234/acceptance-correct"}
    preview = metadata_import.import_payload(session, [record])
    assert preview.matched == 1 and not preview.conflicts
    assert paper.doi == "10.1234/acceptance-incorrect"
    result = metadata_import.import_payload(session, [record], apply=True)
    session.commit()
    assert result.matched == 1 and result.created_shell == 0 and not result.conflicts
    assert paper.doi == "10.1234/acceptance-correct"
    assert paper_service.paper_author_names(paper) == ["Giuseppe Avolio"]
    assert metadata_import.import_payload(session, [record], apply=True).unchanged == 1
    assert metadata_merge.current_source_type(session, paper, "year") == "import_file"
    conflict = metadata_import.import_payload(session, [{**record, "doi": "10.1234/acceptance-other"}], apply=True)
    session.commit()
    assert conflict.ambiguous == 1 and not conflict.matched and not conflict.created_shell
    assert paper.doi == "10.1234/acceptance-correct"
    assert any(row.value == "10.1234/acceptance-incorrect" for row in
               provenance_service.field_history(session, paper.id, "identifier:doi"))
    return {"pdf_difference_history_only": True, "external_doi_correction": True,
            "external_equal_value_protected": True, "identity_conflict_preserved": True}


def run(session: Session, client, index: str) -> dict:
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((50, 50), "Synthetic Metadata Quality Acceptance", fontsize=20)
        page.insert_text((50, 90), "Alice Acceptance and Bob Example", fontsize=12)
        page.insert_text((50, 115), "Department of Electronics", fontsize=10)
        page.insert_text((50, 150), "Abstract", fontsize=12)
        page.insert_text((50, 170), "We present a compact converter and evaluate its precision.", fontsize=10)
        page.insert_text((50, 210), "1 Introduction", fontsize=12)
        document.set_metadata({"author": "user123", "subject": "IEEE Journal;2024;1;2;10.1234/example"})
        pdf = document.tobytes()
    pages = extract_pages(pdf)
    paper = Paper(id=new_uuid(), title="Synthetic Metadata Quality Acceptance", fingerprint=new_uuid(), status="PENDING")
    session.add(paper)
    session.commit()
    preview = metadata_reextract.reextract_paper_metadata(session, paper, pages=pages, pdf_bytes=pdf, dry_run=True)
    assert preview.changed and paper_service.paper_author_names(paper) == []
    assert session.scalar(select(func.count()).select_from(PaperSource)) == 0
    assert session.scalar(select(func.count()).select_from(MetadataQualityDecision)) == 0
    metadata_reextract.reextract_paper_metadata(session, paper, pages=pages, pdf_bytes=pdf)
    session.commit()
    assert paper_service.paper_author_names(paper) == ["Alice Acceptance", "Bob Example"]
    assert paper.abstract == "We present a compact converter and evaluate its precision."
    audits = session.scalar(select(func.count()).select_from(MetadataQualityDecision))
    sources = session.scalar(select(func.count()).select_from(PaperSource))
    metadata_reextract.reextract_paper_metadata(session, paper, pages=pages, pdf_bytes=pdf)
    session.commit()
    assert session.scalar(select(func.count()).select_from(MetadataQualityDecision)) == audits
    assert session.scalar(select(func.count()).select_from(PaperSource)) == sources
    legacy = PaperSource(id=new_uuid(), paper_id=paper.id, source_type="pdf_embedded",
                         source_ref=f"acceptance:{new_uuid()}", raw={"authors": ["Alice Acceptance"]}, match_status="matched")
    session.add(legacy)
    session.flush()
    provenance_service.record_claim(session, paper_id=paper.id, field="authors", value=["Alice Acceptance"], source_id=legacy.id)
    session.add(PaperFile(id=new_uuid(), paper_id=paper.id, kind="original", object_key="acceptance.pdf", bucket="acceptance",
                         content_type="application/pdf", size_bytes=len(pdf), sha256=hashlib.sha256(pdf).hexdigest(), is_primary=True))
    session.commit()
    snapshot = metadata_history.snapshots(session, [{"paper_id": paper.id, "field": "authors"}])[0]
    evidence = metadata_history.extract(snapshot, download=lambda *a, **k: pdf)
    decision = metadata_history.plan(snapshot, evidence)
    assert not decision["changed"] and not decision["pending"]
    assert metadata_history.apply(session, snapshot, evidence, decision)["status"] == "applied"
    session.commit()
    assert provenance_service.conflict_page(session)[1] == 0
    assert metadata_history.apply(session, snapshot, evidence, decision)["status"] == "already_applied"
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        document.set_metadata({"author": "A. Acceptance; B. Example"})
        initials_pdf = document.tobytes()
    initials = Paper(id=new_uuid(), title=paper.title, fingerprint=new_uuid(), status="PENDING")
    session.add(initials)
    session.commit()
    metadata_reextract.reextract_paper_metadata(session, initials, pages=pages, pdf_bytes=initials_pdf)
    session.commit()
    assert paper_service.paper_author_names(initials) == ["A. Acceptance", "B. Example"]
    assert provenance_service.conflict_page(session)[1] == 0
    heading_source = PaperSource(id=new_uuid(), paper_id=initials.id, source_type="pdf_heuristic",
        source_ref=f"acceptance:{new_uuid()}", raw={"authors": ["RANGE SENSORS"]}, match_status="matched")
    session.add(heading_source)
    session.flush()
    heading = provenance_service.record_claim(session, paper_id=initials.id, field="authors",
        value=["RANGE SENSORS"], source_id=heading_source.id)
    session.add(PaperFile(id=new_uuid(), paper_id=initials.id, kind="original", object_key="initials.pdf",
        bucket="acceptance", content_type="application/pdf", size_bytes=len(initials_pdf),
        sha256=hashlib.sha256(initials_pdf).hexdigest(), is_primary=True))
    session.commit()
    snapshot = metadata_history.snapshots(session, [{"paper_id": initials.id, "field": "authors"}])[0]
    evidence = metadata_history.extract(snapshot, download=lambda *a, **k: initials_pdf)
    decision = metadata_history.plan(snapshot, evidence)
    assert not decision["pending"] and not decision["changed"]
    metadata_history.apply(session, snapshot, evidence, decision)
    session.commit()
    assert heading.value == ["RANGE SENSORS"] and heading.decided_by == provenance_service.DECIDED_QUALITY_REJECTED
    assert heading.decision_at is None
    assert provenance_service.conflict_page(session)[1] == 0
    chunk = PaperChunk(id=new_uuid(), paper_id=paper.id, chunk_index=0, text="Preserved text", page_start=1, page_end=1)
    session.add(chunk)
    session.commit()
    original = {"text": chunk.text, "embedding": [0.25, 0.75]}
    client.index(index=index, id=chunk.id, body=original, refresh=True)
    metadata_manual.patch_metadata(session, paper, {"authors": ["Manual Author", "Manual Author"]})
    session.commit()
    assert session.get(MetadataIndexUpdate, paper.id) is not None
    metadata_reextract.reextract_paper_metadata(session, paper, pages=pages, pdf_bytes=pdf)
    session.commit()
    assert paper_service.paper_author_names(paper) == ["Manual Author", "Manual Author"]
    write = partial(opensearch.bulk_update_documents, client=client, index=index)
    with patch.object(opensearch, "bulk_update_documents", write):
        result = metadata_index_sync.flush_pending(session)
    assert result["updated"] == 1 and result["pending"] == 0
    actual = client.get(index=index, id=chunk.id)["_source"]
    assert actual["authors"] == ["Manual Author", "Manual Author"]
    assert actual["text"] == original["text"] and actual["embedding"] == original["embedding"]
    assert metadata_manual.metadata_view(session, paper)["index_sync"]["pending"] is False
    clean_title = "Synthetic PLL & converter"
    encoded_title = "Synthetic PLL &amp; converter"
    title_source = metadata_sources.upsert_source(session, paper_id=paper.id, source_type="import_file",
        source_ref=f"acceptance-title:{paper.id}", raw={"title": clean_title}, match_status="matched")
    provenance_service.set_field(session, paper, "title", encoded_title, source_id=title_source.id,
                                 decided_by=provenance_service.DECIDED_STRUCTURED_OVERRIDE, override=True)
    session.commit()
    preview = metadata_merge.merge_values(session, paper, {"title": clean_title}, source_type="import_file",
                                          source_id=title_source.id, dry_run=True)
    assert preview.applied and paper.title == encoded_title
    result = metadata_merge.merge_values(session, paper, {"title": clean_title}, source_type="import_file",
                                         source_id=title_source.id)
    session.commit()
    assert result.applied and paper.title == clean_title and title_source.raw == {"title": clean_title}
    assert any(row.value == encoded_title for row in provenance_service.field_history(session, paper.id, "title"))
    with patch.object(opensearch, "bulk_update_documents", write):
        assert metadata_index_sync.flush_pending(session, paper_id=paper.id)["pending"] == 0
    actual = client.get(index=index, id=chunk.id)["_source"]
    assert actual["title"] == clean_title and actual["text"] == original["text"] and actual["embedding"] == original["embedding"]
    assert not metadata_merge.merge_values(session, paper, {"title": clean_title}, source_type="import_file",
                                           source_id=title_source.id).applied
    assert session.get(MetadataIndexUpdate, paper.id) is None
    return {**check_source_review(session), **check_external_import(session), "dry_run_rollback": True, "pdf_semantics": True, "reimport_idempotent": True,
            "manual_protected": True, "repeated_authors": True, "metadata_only_index_sync": True,
            "historical_evidence": True, "corroborated_initials": True, "invalid_author_history_retained": True,
            "equivalent_title_cleanup": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base-url", help="also verify the running API using read-only requests")
    args = parser.parse_args()
    schema = f"pdf_quality_acceptance_{uuid4().hex}"
    index = schema.replace("_", "-")
    engine, client = get_engine(), opensearch.get_client()
    scoped = engine.execution_options(schema_translate_map={None: schema})
    report = {}
    schema_created = index_created = False
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        schema_created = True
        Base.metadata.create_all(scoped)
        client.indices.create(index=index)
        index_created = True
        with Session(scoped, expire_on_commit=False, autoflush=False) as session:
            report = run(session, client, index)
    finally:
        try:
            if index_created:
                client.indices.delete(index=index)
        finally:
            if schema_created:
                with engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
    report["cleanup"] = not client.indices.exists(index=index)
    if args.base_url:
        with httpx.Client(base_url=args.base_url, headers={"X-API-Key": settings.paper_api_key}, timeout=30) as api:
            response = api.get("/health")
            response.raise_for_status()
            assert not response.json().get("degraded", False)
            response = api.get("/api/papers", params={"limit": 1})
            response.raise_for_status()
            papers = response.json()["papers"]
            assert papers, "API metadata verification needs an existing paper"
            response = api.get(f"/api/papers/{papers[0]['paper_id']}/metadata")
            response.raise_for_status()
            metadata = response.json()
            assert isinstance(metadata["quality_decisions"], list)
            assert set(metadata["index_sync"]) == {"pending", "attempts", "last_error"}
            started = time.monotonic()
            response = api.get("/api/metadata/review", params={"limit": 1})
            response.raise_for_status()
            assert isinstance(response.json()["conflicts"], list)
            report["live_api"] = True
            report["live_review"] = {"latency_ms": round((time.monotonic() - started) * 1000),
                                     "conflicts_total": response.json()["conflicts_total"]}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return int(not all(report.values()))


if __name__ == "__main__":
    raise SystemExit(main())
