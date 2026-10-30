"""Run production browser acceptance with an ephemeral administrator credential."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import SessionLocal  # noqa: E402
from app.db.models import FigureExtractionRun, Paper, PaperFigure  # noqa: E402
from app.services import api_key_service as keys  # noqa: E402


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
    try:
        subprocess.run(["node", str(ROOT / "scripts/check_webui_deployment.mjs")], cwd=ROOT,
                       env={**os.environ, "PAPERBOX_UI_TEST_KEY": credential,
                            "PAPERBOX_UI_FIGURE_PAPER": figure_paper or ""}, check=True, timeout=360)
    finally:
        with SessionLocal() as session:
            keys.revoke_key(session, prefix)
            session.commit()
        print("Temporary deployment credential removed.")


if __name__ == "__main__":
    main()
