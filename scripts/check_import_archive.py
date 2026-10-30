"""Verify live ZIP ingestion and directory policy, then remove synthetic artifacts."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
import sys
import time
import uuid
import zipfile

import httpx
from sqlalchemy import delete, func, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.db.models import Author, IngestionJob, Paper, PaperAuthor, PaperSource, Venue  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.search import opensearch  # noqa: E402
from app.services import api_key_service, object_storage  # noqa: E402
from scripts.check_original_attachment import sample_pdf  # noqa: E402


def main() -> None:
    marker = f"Archive acceptance {uuid.uuid4().hex}"
    data = sample_pdf(marker)
    prefix = f"archive-test-{uuid.uuid4().hex[:10]}"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("paper.pdf", data)
        bundle.writestr("readme.txt", "acceptance")
        bundle.writestr("../escape.pdf", data)
    job_id = None
    paper_id = None
    with SessionLocal() as session:
        baseline = session.scalar(select(func.count()).select_from(Paper))
        author_ids = set(session.scalars(select(Author.id)))
        venue_ids = set(session.scalars(select(Venue.id)))
        _, key = api_key_service.create_key(session, prefix=prefix, role="write")
        session.commit()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{settings.paper_api_port}",
                          headers={"Authorization": f"Bearer {key}"}, timeout=180) as client:
            if not settings.local_roots:
                response = client.post("/api/papers/ingest/dir", json={"root": str(ROOT), "dry_run": True})
                assert response.status_code == 404, response.status_code
                print("Directory policy: disabled without INGEST_LOCAL_ROOTS, as configured.", flush=True)
            response = client.post("/api/papers/ingest/compressed", files={"file": ("acceptance.zip", archive.getvalue(), "application/zip")})
            response.raise_for_status()
            report = response.json()
            job_id = next((row["job_id"] for row in report["results"] if row.get("job_id")), None)
            assert report["accepted"] == 1 and report["entries_rejected"] == 1 and report["entries_ignored"] == 1, report
            assert job_id
            print("ZIP: one PDF accepted, text ignored, traversal entry rejected.", flush=True)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                response = client.get(f"/api/jobs/{job_id}"); response.raise_for_status()
                job = response.json()
                paper_id = job.get("paper_id") or paper_id
                if job["stage"] in {"COMPLETED", "FAILED"}:
                    assert job["stage"] == "COMPLETED", job.get("error_message")
                    break
                time.sleep(1)
            else:
                raise TimeoutError("ZIP pipeline did not finish")
            response = client.get(f"/api/papers/{paper_id}"); response.raise_for_status()
            assert response.json()["status"] == "INDEXED"
            response = client.get(f"/api/papers/{paper_id}/file"); response.raise_for_status()
            assert hashlib.sha256(response.content).digest() == hashlib.sha256(data).digest()
            print("ZIP pipeline completed; indexed paper and byte-identical original verified.", flush=True)
    finally:
        if job_id:
            deadline = time.monotonic() + 600
            while True:
                with SessionLocal() as session:
                    job = session.get(IngestionJob, job_id)
                    if not job or job.stage in {"COMPLETED", "FAILED"}:
                        paper_id = (job.paper_id if job else None) or paper_id
                        break
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Worker still active; retry cleanup for job {job_id}")
                time.sleep(1)
        with SessionLocal() as session:
            if paper_id:
                paper = session.get(Paper, paper_id)
                new_authors = set(session.scalars(select(PaperAuthor.author_id).where(PaperAuthor.paper_id == paper_id))) - author_ids
                venue_id = paper.venue_id if paper else None
                opensearch.delete_by_paper_id(paper_id)
                object_storage.delete_prefix(paper_id)
                job = session.get(IngestionJob, job_id)
                if job and job.payload and job.payload.get("object_key"):
                    object_storage.delete_object(job.payload["object_key"])
                session.execute(delete(IngestionJob).where(IngestionJob.paper_id == paper_id))
                session.execute(delete(PaperSource).where(PaperSource.paper_id == paper_id))
                session.execute(delete(Paper).where(Paper.id == paper_id))
                for author_id in new_authors:
                    if not session.scalar(select(PaperAuthor.id).where(PaperAuthor.author_id == author_id).limit(1)):
                        session.execute(delete(Author).where(Author.id == author_id))
                if venue_id and venue_id not in venue_ids and not session.scalar(select(Paper.id).where(Paper.venue_id == venue_id).limit(1)):
                    session.execute(delete(Venue).where(Venue.id == venue_id))
            elif job_id:
                session.execute(delete(IngestionJob).where(IngestionJob.id == job_id))
            api_key_service.revoke_key(session, prefix)
            session.commit()
            assert session.scalar(select(func.count()).select_from(Paper)) == baseline
        print("Synthetic paper, job, metadata, objects, index documents and test key removed.", flush=True)


if __name__ == "__main__":
    main()