#!/usr/bin/env python3
"""Real PostgreSQL/OpenSearch acceptance in a temporary schema and index.

uv run python scripts/acceptance_pdf_metadata_quality.py --report logs/eval/pdf-quality-acceptance.json

No library rows, original PDFs or production search documents are modified.
Temporary schema/index cleanup runs even on failure; this never calls embeddings.
"""

from __future__ import annotations

import argparse
from functools import partial
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

from app.db.models import Base, MetadataIndexUpdate, MetadataQualityDecision, Paper, PaperChunk, PaperSource, new_uuid  # noqa: E402
from app.db.session import get_engine  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.parsing.pdf import extract_pages  # noqa: E402
from app.search import opensearch  # noqa: E402
from app.services import metadata_index_sync, metadata_manual, metadata_reextract, paper_service  # noqa: E402


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
    return {"dry_run_rollback": True, "pdf_semantics": True, "reimport_idempotent": True,
            "manual_protected": True, "repeated_authors": True, "metadata_only_index_sync": True}


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
