"""Run production browser acceptance with an ephemeral administrator credential."""

from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from sqlalchemy import delete, select, func

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import SessionLocal  # noqa: E402
from app.db.models import CleanupCandidate, CleanupReceipt, FigureExtractionRun, IngestionJob, Paper, PaperFigure, PaperSource, MetadataSourceReviewEvent  # noqa: E402
from app.services import api_key_service as keys  # noqa: E402


def source_snapshot(session):
    rows = session.execute(select(PaperSource.id, PaperSource.paper_id, PaperSource.match_status, PaperSource.raw)
        .where(PaperSource.match_status.in_(("pending", "ambiguous", "rejected"))).order_by(PaperSource.id)).all()
    return rows, session.scalar(select(func.count()).select_from(MetadataSourceReviewEvent))


def main() -> None:
    prefix = f"deployment-test-{uuid.uuid4().hex[:10]}"
    with SessionLocal() as session:
        _, credential = keys.create_key(session, prefix=prefix, role="admin")
        figure_paper = session.scalar(select(PaperFigure.paper_id).join(
            FigureExtractionRun, FigureExtractionRun.id == PaperFigure.run_id,
        ).join(Paper, Paper.id == PaperFigure.paper_id).where(
            FigureExtractionRun.is_current.is_(True), PaperFigure.deleted_at.is_(None), Paper.deleted_at.is_(None),
        ).limit(1))
        session.commit()
        before = source_snapshot(session)
    try:
        browser_script = "check_cleanup_deployment.mjs" if os.environ.get("WEBUI_CLEANUP_ONLY") == "1" else "check_webui_deployment.mjs"
        subprocess.run(["node", str(ROOT / "scripts" / browser_script)], cwd=ROOT,
                       env={**os.environ, "PAPERBOX_UI_TEST_KEY": credential,
                            "PAPERBOX_UI_FIGURE_PAPER": figure_paper or ""}, check=True, timeout=900)
    finally:
        with SessionLocal() as session:
            keys.revoke_key(session, prefix)
            session.commit()
            assert source_snapshot(session) == before, "Source preview changed data or review history"
        deadline = time.monotonic() + 120
        while True:
            with SessionLocal() as session:
                jobs = session.scalars(select(IngestionJob).where(IngestionJob.kind.in_(("maintenance", "cleanup")),
                    IngestionJob.payload["actor"].as_string() == prefix)).all()
                if all(job.stage in ("COMPLETED", "FAILED") for job in jobs):
                    reports = ROOT / "logs" / "eval"
                    reports.mkdir(parents=True, exist_ok=True)
                    for job in jobs:
                        report = (job.payload or {}).get("report")
                        if job.kind == "cleanup":
                            from app.services import cleanup_service
                            report = cleanup_service.report(session, job.id, limit=10000)
                        if report is not None:
                            (reports / f"{prefix}-{job.id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    for job in jobs:
                        session.execute(delete(CleanupCandidate).where(CleanupCandidate.job_id == job.id))
                        session.delete(job)
                    # These request actors are unique and have just been revoked.
                    session.execute(delete(CleanupReceipt).where(CleanupReceipt.actor == prefix))
                    session.commit()
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError("temporary maintenance tasks are still running; credential removed, tasks retained for inspection")
            time.sleep(0.5)
        print("Temporary deployment credential and read-only test tasks removed.")


if __name__ == "__main__":
    main()
