#!/usr/bin/env python
"""按访问日志回填 ``paper_field_provenance.decision_at``（2026-10-10）。

背景：``decision_at`` 是新增列（迁移 ``a41f7c2d9b30``），在这之前的裁决没有记时间。
本脚本从**应用访问日志**里把裁决请求的时刻找出来补上 —— 不猜、不按 ``decided_at`` 冒充，
拿不到证据的行就留在 ``NULL`` 并报出来。

判据（只认证据）：
* 日志行必须同时匹配「时间戳 + ``POST /api/papers/<uuid>/metadata/{conflicts/dismiss,rollback}`` + 2xx」——
  非 2xx（例如探测用的假 paper_id 拿到的 404）不算裁决；
* 每篇论文的**裁决请求条数**必须与它**待回填的裁决行条数**相等，才能按时间/顺序一一对上；
  对不上就整篇跳过（宁可留空，也不把时间安到错误的行上）。

用法::

    uv run python scripts/backfill_decision_times.py               # 只看会改什么（默认）
    uv run python scripts/backfill_decision_times.py --apply       # 真写
    uv run python scripts/backfill_decision_times.py --log logs/app/paperbox-api.log.1

幂等：只碰 ``decision_at IS NULL`` 且 ``decided_by='dismissed'`` 的行，重复跑不会覆盖已有时间。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.models import Paper, PaperFieldProvenance  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services import provenance_service as prov  # noqa: E402

DEFAULT_LOG = REPO_ROOT / "logs" / "app" / "paperbox-api.log"

#: 一行访问日志：时间戳 + 裁决请求 + 状态码。只认 2xx（见模块 docstring）。
VERDICT_REQUEST = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})"
    r".*\"POST /api/papers/(?P<paper>[0-9a-fA-F-]{36})/metadata/"
    r"(?P<kind>conflicts/dismiss|rollback) HTTP/1\.1\" (?P<status>\d{3})"
)


def parse_requests(paths: list[Path]) -> dict[str, list[dict[str, object]]]:
    """从日志里取出「按时序排列」的裁决请求，按 paper_id 归组。"""
    requests: dict[str, list[dict[str, object]]] = {}
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = VERDICT_REQUEST.match(line)
            if match is None or not match.group("status").startswith("2"):
                continue
            moment = datetime.strptime(
                match.group("ts"), "%Y-%m-%dT%H:%M:%S%z"
            ).astimezone(timezone.utc)
            requests.setdefault(match.group("paper"), []).append(
                {"at": moment, "kind": match.group("kind"), "line": line.strip()[:160]}
            )
    for items in requests.values():
        items.sort(key=lambda item: item["at"])
    return requests


def open_candidates(session) -> dict[str, list[PaperFieldProvenance]]:
    """待回填的行：已裁定（``dismissed``）但还不知道是什么时候裁的。"""
    statement = (
        select(PaperFieldProvenance)
        .join(Paper, Paper.id == PaperFieldProvenance.paper_id)
        .where(
            PaperFieldProvenance.decision_at.is_(None),
            PaperFieldProvenance.decided_by == prov.DECIDED_DISMISSED,
            Paper.deleted_at.is_(None),
        )
        .order_by(PaperFieldProvenance.paper_id, PaperFieldProvenance.field)
    )
    grouped: dict[str, list[PaperFieldProvenance]] = {}
    for row in session.execute(statement).scalars().all():
        grouped.setdefault(row.paper_id, []).append(row)
    return grouped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真写库（默认只报告）")
    parser.add_argument("--log", type=Path, default=None, help=f"访问日志（默认 {DEFAULT_LOG}）")
    parser.add_argument("--json", type=Path, default=None, help="把报告也写一份到该路径")
    args = parser.parse_args()

    logs = [args.log] if args.log else [DEFAULT_LOG, DEFAULT_LOG.with_suffix(".log.1")]
    requests = parse_requests([path for path in logs if path is not None])

    session = SessionLocal()
    try:
        candidates = open_candidates(session)
        planned: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []

        for paper_id, rows in sorted(candidates.items()):
            found = requests.get(paper_id, [])
            if len(found) != len(rows):
                skipped.append(
                    {
                        "paper_id": paper_id,
                        "rows": len(rows),
                        "requests": len(found),
                        "why": "请求数与待回填行数不等，无法一一对应",
                    }
                )
                continue
            for row, request in zip(rows, found):
                planned.append(
                    {
                        "provenance_id": row.id,
                        "paper_id": paper_id,
                        "field": row.field,
                        "decision_at": request["at"].isoformat(),
                        "request": request["kind"],
                        "local": request["at"].astimezone().isoformat(),
                    }
                )

        print(f"日志：{[str(p) for p in logs if p and p.exists()]}")
        print(f"日志里的裁决请求：{sum(len(v) for v in requests.values())} 条，覆盖 {len(requests)} 篇")
        print(f"待回填的裁决行：{sum(len(v) for v in candidates.values())} 条，覆盖 {len(candidates)} 篇")
        print(f"可精确回填：{len(planned)} 条；跳过：{len(skipped)} 篇")
        for item in planned:
            print(
                f"  · {item['paper_id'][:8]} {item['field']:<16} "
                f"{item['decision_at']}  ← {item['request']}"
            )
        for item in skipped:
            print(f"  ! 跳过 {item['paper_id'][:8]}：{item['why']}（行 {item['rows']} / 请求 {item['requests']}）")

        if args.apply:
            by_id = {row.id: row for rows in candidates.values() for row in rows}
            for item in planned:
                row = by_id[item["provenance_id"]]
                prov.record_human_decision(row, at=datetime.fromisoformat(str(item["decision_at"])))
            session.commit()
            print(f"已写入 {len(planned)} 条 decision_at")
        else:
            print("（试运行：没有写库；加 --apply 才写）")

        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(
                json.dumps(
                    {"planned": planned, "skipped": skipped, "applied": bool(args.apply)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"报告：{args.json}")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
