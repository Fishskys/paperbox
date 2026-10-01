#!/usr/bin/env python3
"""Search pipelines: the deployment-state objects the native backend needs (plan §7 T-E4).

    uv run python scripts/ensure_search_pipelines.py           # create / refresh
    uv run python scripts/ensure_search_pipelines.py --check    # read-only drift check
    uv run python scripts/ensure_search_pipelines.py --json

A **search pipeline** is a cluster object, not an ``.env`` value: it is what
turns a single ``hybrid`` request into a fused result set (RRF, ``rank_constant``
60 -- the twin of ``app/search/ranking.rrf_fuse``). The ``native`` retrieval
backend (``SEARCH_BACKEND=native``) references ``paperbox-rrf60`` by name and gets
a 400 from the cluster if it is missing, so the object is part of the deployment,
like the index mapping.

The bodies themselves live in ``app/search/native.py`` (``PIPELINE_BODIES``):
the app and the SRW eval bypass (``scripts/srw_setup.py``) must agree on what the
pipeline does, and one definition is the only way to keep them agreeing. This
script is the writer; ``--check`` is what ``scripts/healthcheck.py`` calls.

Idempotent: an identical body is left alone, a drifted one is rewritten, a
missing one is created. Deleting is deliberately **not** offered -- pipelines
referenced by an SRW experiment cannot be deleted at all (the cluster answers
500 and names the experiment), and a stale unused pipeline costs nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.search.native import ensure_pipelines  # noqa: E402  (path bootstrap above)
from app.search.opensearch import get_client  # noqa: E402
from app.search.hybrid import SearchError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift without writing anything (exit 1 when drifted)",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()

    try:
        report = ensure_pipelines(dry_run=args.check)
    except SearchError as exc:
        print(f"FAIL: {exc}")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report:
            mark = {"created": "+", "updated": "~", "kept": "="}[item["action"]]
            print(f"  {mark} {item['name']}: {item['action']}")
        drifted = [item["name"] for item in report if item["changed"]]
        if args.check and drifted:
            print(f"DRIFT: {', '.join(drifted)} (run without --check to rewrite)")
        else:
            print(f"  {len(report)} pipelines in sync")

    if args.check and any(item["changed"] for item in report):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
