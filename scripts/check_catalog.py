"""Read-only live checks for conventional metadata search and its facets."""

import argparse
import json
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8077")
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        before = client.get("/api/papers?limit=1").json()
        reports = []
        for params in ({"scope": "all", "q": "timing"},
                       {"scope": "title", "q": "clock", "sort": "year_desc"},
                       {"scope": "authors", "q": "Chen"},
                       {"scope": "venue", "q": "solid", "paper_type": "journal"}):
            started = time.perf_counter()
            response = client.get("/api/papers", params={**params, "facets": "true", "limit": 20})
            response.raise_for_status()
            result = response.json()
            assert result["facets"] is not None
            assert len(result["papers"]) <= 20
            reports.append({"scope": params["scope"], "total": result["total"],
                            "ms": round((time.perf_counter() - started) * 1000),
                            "facets": {name: len(items) for name, items in result["facets"].items()}})
        after = client.get("/api/papers?limit=1").json()
        assert before["total"] == after["total"]
        print(json.dumps({"status": "passed", "papers": after["total"], "queries": reports}))


if __name__ == "__main__":
    main()