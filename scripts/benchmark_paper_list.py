"""Read-only timing and query plans for the live paper list (no synthetic rows)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

from sqlalchemy import event, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import SessionLocal, get_engine  # noqa: E402
from app.schemas.paper import PaperOut  # noqa: E402
from app.services import paper_service  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args()
    engine = get_engine()
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        context.paperbox_benchmark_start = time.perf_counter()

    def after(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters,
                round((time.perf_counter() - context.paperbox_benchmark_start) * 1000, 3)))

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    try:
        for iteration in range(args.repeat):
            for status in (None, "INDEXED", "AWAITING_FILE"):
                with SessionLocal() as session:
                    session.execute(text("SET TRANSACTION READ ONLY"))
                    statements.clear()
                    started = time.perf_counter()
                    rows, total = paper_service.list_papers(session, status=status, limit=20)
                    listed = time.perf_counter()
                    output = [PaperOut.model_validate(paper_service.serialize_paper(row)) for row in rows]
                    finished = time.perf_counter()
                    measured = list(statements)
                    queries = []
                    for sql, _, duration in measured:
                        match = re.search(r"\bFROM\s+(\w+)", sql, re.IGNORECASE)
                        queries.append({"table": match.group(1) if match else "subquery",
                            "count": "count(" in sql.lower(), "ms": duration})
                    print(json.dumps({"iteration": iteration + 1, "status": status,
                        "total": total, "returned": len(output),
                        "list_ms": round((listed - started) * 1000, 3),
                        "serialize_ms": round((finished - listed) * 1000, 3),
                        "queries": queries}), flush=True)
                    if args.explain and iteration == 0:
                        for sql, parameters, _ in measured[:2]:
                            plan = session.connection().exec_driver_sql(
                                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, parameters,
                            ).scalar_one()
                            print(json.dumps({"status": status, "explain": plan}), flush=True)
    finally:
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)


if __name__ == "__main__":
    main()