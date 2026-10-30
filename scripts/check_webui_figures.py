"""Exercise live figure management in the browser and clean up its temporary data."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select  # noqa: E402
from app.db.models import FigureExtractionRun, IngestionJob, Paper, PaperFile  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services import api_key_service as keys, figure_service, object_storage  # noqa: E402


def main() -> None:
    paper_id = str(uuid.uuid4())
    prefixes: list[str] = []
    issued: dict[str, str] = {}
    data = (ROOT / "tests/fixtures/smoke_sample.pdf").read_bytes()
    try:
        stored = object_storage.upload_original_pdf(paper_id, data)
        with SessionLocal() as session:
            for role in ("read", "write"):
                prefix = f"webui-figure-{role}-{uuid.uuid4().hex[:8]}"
                _, issued[role] = keys.create_key(session, prefix=prefix, role=role)
                prefixes.append(prefix)
            session.add(Paper(id=paper_id, fingerprint=f"webui-figures:{paper_id}",
                              title=f"WebUI figure acceptance {paper_id}", status="STORED"))
            session.flush()
            session.add(PaperFile(paper_id=paper_id, kind="original", is_primary=True,
                                  bucket=stored.bucket, object_key=stored.object_key,
                                  sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)))
            session.commit()
        env = {**os.environ, "PAPERBOX_FIGURE_TEST": json.dumps({
            "paper_id": paper_id, "keys": issued, "python": sys.executable,
        })}
        subprocess.run(["node", str(ROOT / "scripts/check_webui_figures.mjs")],
                       cwd=ROOT, env=env, check=True, timeout=240)
        assert object_storage.download_bytes(stored.object_key, stored.bucket) == data
        print("Original PDF verified byte-for-byte.", flush=True)
    finally:
        try:
            with SessionLocal() as session:
                if session.get(Paper, paper_id):
                    figure_service.delete_figures(session, paper_id)
            deadline = time.monotonic() + 120
            while True:
                with SessionLocal() as session:
                    active = session.scalar(select(IngestionJob.id).where(
                        IngestionJob.paper_id == paper_id,
                        IngestionJob.stage.not_in(("COMPLETED", "FAILED")),
                    ))
                if not active:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Temporary figure worker still active: {paper_id}")
                time.sleep(1)
            object_storage.delete_prefix(paper_id)
            with SessionLocal() as session:
                session.execute(delete(IngestionJob).where(IngestionJob.paper_id == paper_id))
                session.execute(delete(Paper).where(Paper.id == paper_id))
                session.commit()
                assert session.get(Paper, paper_id) is None
                assert session.scalar(select(FigureExtractionRun.id).where(FigureExtractionRun.paper_id == paper_id)) is None
        finally:
            with SessionLocal() as session:
                for prefix in prefixes:
                    if keys.get_by_prefix(session, prefix):
                        keys.revoke_key(session, prefix)
                session.commit()
        print("Temporary paper, figures, objects, jobs and keys removed.", flush=True)


if __name__ == "__main__":
    main()
