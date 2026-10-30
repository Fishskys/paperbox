"""Exercise targeted upload on live services and remove all synthetic artifacts."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
import sys
import time
import uuid

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import delete, func, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.db.models import (  # noqa: E402
    Author, IngestionJob, Paper, PaperAuthor, PaperSource, PaperTag, PapersTag, Venue,
)
from app.db.session import SessionLocal  # noqa: E402
from app.services import api_key_service, object_storage, paper_service  # noqa: E402
from app.search import opensearch  # noqa: E402


def sample_pdf(marker: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    content = DecodedStreamObject()
    lines = [marker, "Abstract", "Targeted original attachment integration verification.",
             "The selected paper must retain its identifier after processing.",
             "The original is stored and the text is indexed for retrieval."]
    content.set_data(("BT /F1 12 Tf 50 740 Td 18 TL " +
                      " T* ".join(f"({line}) Tj" for line in lines) + " ET").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.add_metadata({"/Title": marker})
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def main() -> None:
    paper_id = str(uuid.uuid4())
    marker = f"Attachment verification {uuid.uuid4().hex}"
    prefix = f"attachment-test-{uuid.uuid4().hex[:10]}"
    data = sample_pdf(marker)
    with SessionLocal() as session:
        baseline = session.scalar(select(func.count()).select_from(Paper))
        existing_authors = set(session.scalars(select(Author.id)))
        existing_venues = set(session.scalars(select(Venue.id)))
        existing_tags = set(session.scalars(select(PaperTag.id)))
        _, key = api_key_service.create_key(session, prefix=prefix, role="write")
        paper_service.create_paper(session, paper_id=paper_id, title=marker,
                                   fingerprint=f"sha256:{uuid.uuid4().hex}", status="AWAITING_FILE")
        session.commit()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{settings.paper_api_port}",
                          headers={"Authorization": f"Bearer {key}"}, timeout=60) as client:
            response = client.post(f"/api/papers/{paper_id}/file",
                                   files={"file": ("verification.pdf", data, "application/pdf")})
            response.raise_for_status()
            assert response.status_code == 202
            assert response.json()["paper_id"] == paper_id
            job_id = response.json()["job_id"]
            print("Targeted upload accepted; polling the real pipeline.", flush=True)
            deadline = time.monotonic() + 600
            previous = None
            while time.monotonic() < deadline:
                response = client.get(f"/api/jobs/{job_id}")
                response.raise_for_status()
                job = response.json()
                if job["stage"] != previous:
                    print(f"Stage: {job['stage']}", flush=True)
                    previous = job["stage"]
                if job["stage"] in {"COMPLETED", "FAILED"}:
                    assert job["stage"] == "COMPLETED", job.get("error_message")
                    break
                time.sleep(1)
            else:
                raise TimeoutError("attachment pipeline did not finish")
            response = client.get(f"/api/papers/{paper_id}")
            response.raise_for_status()
            paper = response.json()
            assert paper["paper_id"] == paper_id and paper["status"] == "INDEXED"
            assert len(paper["files"]) == 1
            original = client.get(f"/api/papers/{paper_id}/file")
            original.raise_for_status()
            assert hashlib.sha256(original.content).digest() == hashlib.sha256(data).digest()
            chunks = client.get(f"/api/papers/{paper_id}/chunks")
            chunks.raise_for_status()
            assert chunks.json()["total"] > 0
            index_client = opensearch.get_client()
            for _ in range(15):
                count = index_client.count(index=settings.opensearch_alias,
                    body={"query": {"term": {"paper_id": paper_id}}})["count"]
                if count > 0:
                    break
                time.sleep(1)
            assert count > 0, "paper chunks missing from OpenSearch"
            print("Verified: same paper ID, INDEXED, one PDF, byte-identical download, DB and OpenSearch chunks.", flush=True)
    finally:
        # Do not remove a paper while its worker can still write it.
        deadline = time.monotonic() + 600
        while True:
            with SessionLocal() as session:
                active = session.scalar(select(IngestionJob.id).where(
                    IngestionJob.paper_id == paper_id,
                    IngestionJob.stage.not_in(("COMPLETED", "FAILED")),
                ).limit(1))
            if not active:
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"Worker still active; cleanup must be retried for {paper_id}")
            time.sleep(1)
        with SessionLocal() as session:
            jobs = list(session.scalars(select(IngestionJob).where(IngestionJob.paper_id == paper_id)))
            staging_keys = [job.payload.get("object_key") for job in jobs if job.payload]
            author_ids = set(session.scalars(select(PaperAuthor.author_id).where(PaperAuthor.paper_id == paper_id))) - existing_authors
            tag_ids = set(session.scalars(select(PapersTag.tag_id).where(PapersTag.paper_id == paper_id))) - existing_tags
            paper = session.get(Paper, paper_id)
            venue_id = paper.venue_id if paper else None
            opensearch.delete_by_paper_id(paper_id)
            object_storage.delete_prefix(paper_id)
            for staging_key in staging_keys:
                if staging_key:
                    object_storage.delete_object(staging_key)
            session.execute(delete(IngestionJob).where(IngestionJob.paper_id == paper_id))
            session.execute(delete(PaperSource).where(PaperSource.paper_id == paper_id))
            session.execute(delete(Paper).where(Paper.id == paper_id))
            for author_id in author_ids:
                if not session.scalar(select(PaperAuthor.id).where(PaperAuthor.author_id == author_id).limit(1)):
                    session.execute(delete(Author).where(Author.id == author_id))
            for tag_id in tag_ids:
                if not session.scalar(select(PapersTag.id).where(PapersTag.tag_id == tag_id).limit(1)):
                    session.execute(delete(PaperTag).where(PaperTag.id == tag_id))
            if venue_id and venue_id not in existing_venues:
                if not session.scalar(select(Paper.id).where(Paper.venue_id == venue_id).limit(1)):
                    session.execute(delete(Venue).where(Venue.id == venue_id))
            api_key_service.revoke_key(session, prefix)
            session.commit()
            assert session.get(Paper, paper_id) is None
            assert api_key_service.get_by_prefix(session, prefix) is None
            assert session.scalar(select(func.count()).select_from(Paper)) == baseline
        print("Synthetic paper, jobs, metadata, objects, index documents and temporary key removed.", flush=True)


if __name__ == "__main__":
    main()
