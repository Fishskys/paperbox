"""Read-only audit of author / venue metadata quality across the live library.

    uv run python scripts/audit_author_venue_quality.py
    uv run python scripts/audit_author_venue_quality.py --json

WHAT IT LOOKS FOR (per paper, authors and venue):

* author
  - ``blank``            an empty / whitespace-only author name
  - ``concatenated``     a single "name" that is really several names joined
                         (``;`` / `` and `` / comma between what look like names)
  - ``duplicate``        the same normalized author twice on one paper
  - ``placeholder``      a suspicious token (``authors``, ``anonymous``, a bare
                         number) that a mis-parse produced instead of a name
  - ``order_gap``        author_order values that are not 0..n-1 (gaps/dupes)
* venue
  - ``blank``            a venue row with an empty display name
  - ``concatenated``     a venue name carrying a ``;`` (looks like a joined string)
  - ``duplicate_keys``   two different display names normalizing to the same key
  - ``papers_missing``   live papers with no venue (informational, not a bug)

STRICTLY READ-ONLY: it never writes or repairs. Exits non-zero when it finds any
real problem (``--no-fail`` to report only). Authors are matched the same way the
``author`` filter does (``normalized_name``).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.models import Author, Paper, PaperAuthor, Venue  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.paper_service import normalize_text  # noqa: E402

#: Tokens that are never a real author name -- a mis-parse wrote them.
_PLACEHOLDER_TOKENS = {"authors", "author", "anonymous", "unknown", "n/a", "none"}


def _looks_concatenated_name(name: str) -> bool:
    """Heuristic: does one author string really hold several names?"""
    text = (name or "").strip()
    if not text:
        return False
    if ";" in text:
        return True
    if re.search(r"\s+and\s+", text, re.IGNORECASE) and text.count(" ") >= 2:
        return True
    # "Lastname, Firstname, Lastname, Firstname" style: 3+ commas.
    if text.count(",") >= 3:
        return True
    return False


def _is_placeholder(name: str) -> bool:
    text = (name or "").strip().casefold()
    if text in _PLACEHOLDER_TOKENS:
        return True
    if text.isdigit():
        return True
    return False


def audit() -> dict:
    session = SessionLocal()
    try:
        live = session.execute(
            select(Paper.id, Paper.title, Paper.venue_id, Paper.status).where(
                Paper.deleted_at.is_(None)
            )
        ).all()
        papers = {str(row[0]): row for row in live}

        # authors per paper (with order), via the link table
        links = session.execute(
            select(
                PaperAuthor.paper_id,
                PaperAuthor.author_order,
                Author.name,
                Author.normalized_name,
            ).join(Author, PaperAuthor.author_id == Author.id)
        ).all()
        names_by_paper: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
        for paper_id, order, name, normalized in links:
            names_by_paper[str(paper_id)].append((int(order), name, normalized))

        venue_rows = session.execute(select(Venue.id, Venue.name, Venue.normalized_name)).all()
        venues = {str(row[0]): row for row in venue_rows}
        venue_key_to_names: dict[str, set[str]] = defaultdict(set)
        for _, name, normalized in venue_rows:
            venue_key_to_names[normalized].add(name)

        findings: list[dict] = []

        def add(kind, subject_kind, subject_id, detail, sample=None):
            findings.append(
                {
                    "kind": kind,
                    "subject": subject_kind,
                    "subject_id": subject_id,
                    "detail": detail,
                    "sample": sample,
                }
            )

        # ---- per-paper author checks -------------------------------------
        for paper_id, (_, title, venue_id, _status) in papers.items():
            rows = sorted(names_by_paper.get(paper_id, []))
            seen_norm: Counter[str] = Counter()
            orders = []
            for order, name, normalized in rows:
                orders.append(order)
                seen_norm[normalized] += 1
                if not (name or "").strip():
                    add("author.blank", "paper", paper_id, "empty author name on the paper", title)
                if _looks_concatenated_name(name):
                    add("author.concatenated", "paper", paper_id,
                        f"one author string holds several names: {name!r}", title)
                if _is_placeholder(name):
                    add("author.placeholder", "paper", paper_id,
                        f"suspicious author name: {name!r}", title)
            for normalized, count in seen_norm.items():
                if count > 1:
                    add("author.duplicate", "paper", paper_id,
                        f"author appears {count}x (normalized={normalized!r})", title)
            if orders and orders != list(range(len(orders))):
                add("author.order_gap", "paper", paper_id,
                    f"author_order values are {orders}, not 0..{len(orders)-1}", title)
            if venue_id is None:
                add("venue.papers_missing", "paper", paper_id,
                    "live paper has no venue", title)

        # ---- venue checks -------------------------------------------------
        for venue_id, name, normalized in venue_rows:
            if not (name or "").strip():
                add("venue.blank", "venue", venue_id, "venue has an empty display name")
            if ";" in (name or ""):
                add("venue.concatenated", "venue", venue_id,
                    f"venue name carries a ';': {name!r}", name)
        for key, names in venue_key_to_names.items():
            if len(names) > 1:
                add("venue.duplicate_keys", "venue", key,
                    f"{len(names)} display names share normalized key {key!r}: {sorted(names)}")

        # ---- summary ------------------------------------------------------
        totals = Counter(f["kind"] for f in findings)
        return {
            "papers_live": len(papers),
            "venues": len(venue_rows),
            "author_links": len(links),
            "findings_total": len(findings),
            "by_kind": dict(sorted(totals.items())),
            "findings": findings,
        }
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the raw JSON report")
    parser.add_argument("--no-fail", action="store_true", help="exit 0 even if issues are found")
    args = parser.parse_args()

    report = audit()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"papers: {report['papers_live']}  venues: {report['venues']}  "
              f"author links: {report['author_links']}")
        if not report["by_kind"]:
            print("issues: none")
        else:
            print(f"issues: {report['by_kind']}")
            for f in report["findings"]:
                print(f"  - {f['kind']} [{f['subject']} {f['subject_id']}] {f['detail']}")

    real = {k for k in report["by_kind"] if k != "venue.papers_missing"}
    if report.get("findings_total") and real and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
