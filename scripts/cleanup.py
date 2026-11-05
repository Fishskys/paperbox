#!/usr/bin/env python3
"""Preview selected soft-deleted papers, then execute a saved cleanup preview."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from app.db.models import Paper  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.schemas.cleanup import CleanupIn  # noqa: E402
from app.services import cleanup_service as service  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-id", action="append", type=UUID, default=[])
    parser.add_argument("--deleted-before", type=datetime.fromisoformat, help="ISO deletion-time cutoff; at most 100 papers per preview")
    parser.add_argument("--apply-preview", type=UUID, help="execute exactly the persisted preview scope")
    parser.add_argument("--retry-job", type=UUID, help="resume only unfinished resources of a failed execution")
    parser.add_argument("--request-id", type=UUID, default=None)
    args = parser.parse_args(argv)
    if (args.apply_preview and args.retry_job) or ((args.apply_preview or args.retry_job) and (args.paper_id or args.deleted_before)):
        parser.error("use a saved preview/retry OR a new explicit selection")
    if not (args.apply_preview or args.retry_job or args.paper_id or args.deleted_before):
        parser.error("specify --paper-id or --deleted-before to preview; execution requires --apply-preview")
    with SessionLocal() as session:
        try:
            source_id = args.apply_preview or args.retry_job
            if source_id:
                source = service.get(session, str(source_id))
                request = {**service.scope(source.payload["request"]), "dry_run": False,
                    "preview_job_id" if args.apply_preview else "retry_job_id": str(source_id)}
            else:
                ids = [str(identifier) for identifier in args.paper_id]
                if args.deleted_before:
                    query = select(Paper.id).where(Paper.deleted_at.is_not(None), Paper.deleted_at < args.deleted_before)
                    if ids:
                        query = query.where(Paper.id.in_(ids))
                    ids = list(session.scalars(query.order_by(Paper.deleted_at).limit(101)))
                if not ids or len(ids) > 100:
                    parser.error("selection must contain 1..100 papers; narrow the cutoff or provide explicit IDs")
                request = {"categories": ["deleted_papers"], "paper_ids": ids, "dry_run": True}
            job = service.submit(session, CleanupIn(request_id=args.request_id or uuid4(), **request), actor="cli", enqueue=False)
            service.execute(session, job.id)
            report = service.report(session, job.id, limit=1100)
            print(json.dumps(report, default=str, ensure_ascii=False))
            return 1 if job.stage == "FAILED" else 0
        except service.CleanupError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
