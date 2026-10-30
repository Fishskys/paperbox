"""Exercise real native recall modes and remove only this script's search logs."""

import json
from pathlib import Path
import sys
import uuid

import httpx
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.models import SearchQuery  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402


def main():
    ids = []
    reports = []
    pages = []
    try:
        with httpx.Client(base_url="http://127.0.0.1:8077", timeout=180) as client:
            for mode, offset, rerank in (("keyword", 0, False), ("keyword", 20, False), ("semantic", 0, False), ("hybrid", 0, True)):
                request_id = f"webui-search-test-{uuid.uuid4().hex}"
                ids.append(request_id)
                response = client.post("/api/search", headers={"X-Request-ID": request_id}, json={
                    "query": "timing circuit", "mode": mode, "recall_k": 35, "top_k": 20,
                    "offset": offset, "rewrite": False, "rerank": rerank,
                })
                response.raise_for_status()
                body = response.json()
                assert body["backend"] == "native" and body["mode"] == mode
                assert body["recalled_papers"] <= 35
                assert body["rewrite"]["applied"] is False
                if mode == "keyword":
                    pages.append({row["paper_id"] for row in body["results"]})
                reports.append({"mode": mode, "offset": offset, "returned": len(body["results"]),
                    "recalled": body["recalled_papers"], "rerank": body["rerank"]["status"], "ms": body["took_ms"]})
                print(json.dumps(reports[-1]), flush=True)
            assert pages[0].isdisjoint(pages[1]), "duplicate papers across the same recall window"
    finally:
        with SessionLocal() as session:
            session.execute(delete(SearchQuery).where(SearchQuery.request_id.in_(ids)))
            session.commit()
    print("Native recall modes and page separation verified; temporary search logs removed.")


if __name__ == "__main__":
    main()