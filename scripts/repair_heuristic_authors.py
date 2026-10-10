#!/usr/bin/env python3
"""Re-decide the ``authors`` field on papers where a broken heuristic won.

    uv run python scripts/repair_heuristic_authors.py            # 只看（默认 dry-run）
    uv run python scripts/repair_heuristic_authors.py --apply

背景（2026-10-10 真机事故）：首页启发式把标题碎片与摘要句子当成人名，而合并规则 4
当时写的是「``authors`` 取最长列表」—— 坏列表更长，于是盖住了 PDF 内嵌元数据里的干净
人名。规则本身已修（弱来源不再靠"更长"赢），但**已经落库的判定不会自己回滚**：库里
``paper_authors`` 里那些垃圾名字还在，`/api/metadata/review` 也会一直把它们报成冲突。

本脚本按修好的规则重放一次：对每篇"当前 authors 声明来自 ``pdf_heuristic``、且存在一份
结构化来源的 authors 声明"的论文，用那份结构化值调用合并引擎（走 ``merge_values``，
与正常导入同一条路径），引擎负责翻 ``is_current``、写历史行、并重建 ``paper_authors``
（``prov.set_field`` 内部会调 ``set_paper_authors``）。

另外单独列出"当前 authors 来自启发式、但库里没有更好的结构化值、且名字形状不合法"的
论文：这些**不自动改**（脚本没有更好的来源可用），只报出来供人工复核或重新导入。

幂等：跑过一次之后当前声明就是结构化的，再跑不再命中。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.models import Paper, PaperFieldProvenance, PaperSource  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services import metadata_merge as merge  # noqa: E402
from app.services import provenance_service as prov  # noqa: E402
from app.services.metadata_service import _plausible_author  # noqa: E402

LOGS = ROOT / "logs" / "app"
#: 优先用哪种结构化来源的值。内嵌元数据来自 PDF 自己的作者栏，最贴近真相。
PREFERRED = ("pdf_embedded", "manual", "ieee_api", "arxiv_api", "crossref", "import_file")


def _claim_source_type(session, source_id) -> str | None:
    if not source_id:
        return None
    source = session.get(PaperSource, source_id)
    return (source.source_type or "").strip().lower() or None if source else None


def _structured_candidate(session, paper_id: str):
    """The best non-current structured ``authors`` claim for a paper, if any."""
    rows = session.execute(
        select(PaperFieldProvenance).where(
            PaperFieldProvenance.paper_id == paper_id,
            PaperFieldProvenance.field == prov.FIELD_AUTHORS,
            PaperFieldProvenance.is_current.is_(False),
        )
    ).scalars().all()
    candidates = []
    for row in rows:
        source_type = _claim_source_type(session, row.source_id)
        if source_type and merge.is_structured(source_type) and row.value:
            rank = PREFERRED.index(source_type) if source_type in PREFERRED else len(PREFERRED)
            candidates.append((rank, source_type, row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    rank, source_type, row = candidates[0]
    return source_type, row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="写库；不带则只报告（默认）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少篇（0 = 全部）")
    parser.add_argument("--out", default="", help="报告 JSON 的路径")
    args = parser.parse_args()

    session = SessionLocal()
    repaired: list[dict] = []
    manual: list[dict] = []
    untouched: list[dict] = []
    try:
        papers = session.execute(
            select(Paper).where(Paper.deleted_at.is_(None))
        ).scalars().all()

        for paper in papers:
            current = prov.current_claim(session, paper.id, prov.FIELD_AUTHORS)
            current_type = _claim_source_type(session, current.source_id) if current else None
            if not merge.is_heuristic(current_type):
                continue
            current_value = list(current.value or [])
            candidate = _structured_candidate(session, paper.id)
            if candidate is None:
                junk = [name for name in current_value if not _plausible_author(str(name))]
                if junk:
                    manual.append(
                        {
                            "paper_id": paper.id,
                            "title": paper.title,
                            "reason": "启发式当权且没有更好的结构化来源，名单里有形状不合法的项",
                            "junk": junk,
                            "value": current_value,
                        }
                    )
                continue
            source_type, row = candidate
            wanted = list(row.value or [])
            if prov._same_value(current_value, wanted):
                continue
            # 只在有**证据**时才修：当前名单里确实混进了形状不合法的项。反例：
            # "2022 02 17 2D for Electronics" 的启发式名单是 4 个真名，而内嵌只有
            # 1 个（Max Lemme）—— 那种情况按规则 4 修过去反而是丢数据。
            junk_now = [name for name in current_value if not _plausible_author(str(name))]
            # 备选值自己也得干净：真机上见过内嵌元数据是 "msi" 这种垃圾，而启发式那份
            # 反而是 14 个真名（只有连字符伪影），按规则硬修过去就毁数据了。
            candidate_junk = [name for name in wanted if not _plausible_author(str(name))]
            if candidate_junk:
                manual.append(
                    {
                        "paper_id": paper.id,
                        "title": paper.title,
                        "reason": "结构化备选值本身形状不合法，两边都不可信",
                        "junk": junk_now,
                        "candidate_junk": candidate_junk,
                        "value": current_value,
                    }
                )
                continue
            if not junk_now:
                untouched.append(
                    {
                        "paper_id": paper.id,
                        "title": paper.title,
                        "reason": "启发式当权但名单看着干净，不动（内嵌值可能更短）",
                        "value": current_value,
                        "structured_alternative": wanted,
                        "structured_source": source_type,
                    }
                )
                continue
            entry = {
                "paper_id": paper.id,
                "title": paper.title,
                "from": current_type,
                "to": source_type,
                "before": current_value,
                "after": wanted,
            }
            if args.apply:
                report = merge.merge_values(
                    session,
                    paper,
                    {prov.FIELD_AUTHORS: wanted},
                    source_type=source_type,
                    source_id=row.source_id,
                )
                decision = report.decisions[0]
                entry["action"] = decision.action
                entry["reason"] = decision.reason
                entry["applied"] = decision.action in (
                    merge.ACTION_OVERRIDDEN,
                    merge.ACTION_SPECIAL,
                )
                # ``prov.set_field`` 内部已重建 ``paper_authors``（authors 是结构化字段），
                # 不需要再手工补镜像。
            repaired.append(entry)
            if args.limit and len(repaired) + len(manual) >= args.limit:
                break

        if args.apply:
            session.commit()
        print(f"可修复（启发式当权 + 有结构化备选）: {len(repaired)} 篇")
        for entry in repaired:
            mark = entry.get("action", "dry-run")
            print(f"  [{mark}] {entry['title'][:58]}")
            print(f"        {entry['from']} -> {entry['to']}")
            print(f"        旧: {entry['before']}")
            print(f"        新: {entry['after']}")
        print(f"维持原判（启发式当权、名单看着干净、不拿更短的结构化值覆盖）: {len(untouched)} 篇")
        for entry in untouched:
            print(f"  - {entry['title'][:58]}  {len(entry['value'])} 人 vs {entry['structured_source']} {len(entry['structured_alternative'])} 人")
        print(f"需人工复核（没有更好的来源，名字仍不合法）: {len(manual)} 篇")
        for entry in manual:
            print(f"  - {entry['title'][:58]}  垃圾项: {entry['junk']}")

        out = Path(args.out) if args.out else LOGS / "repair-heuristic-authors.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "applied": args.apply,
                    "repaired": repaired,
                    "left_untouched": untouched,
                    "manual_review": manual,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"报告: {out}")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
