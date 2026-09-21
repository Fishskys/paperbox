#!/usr/bin/env python3
"""Backfill the metadata layer for papers that predate it.

    uv run python scripts/backfill_metadata.py --dry-run
    uv run python scripts/backfill_metadata.py

What it does, per live paper (section 2.9 of the metadata plan):

* creates the ``paper_sources`` row of the first-page heuristics
  (``source_type='pdf_heuristic'``, ``source_ref='paper:<id>:heuristic'``) with the
  values that are already on the row as its ``raw`` snapshot;
* writes a ``paper_field_provenance`` claim for every field the paper has
  (title/abstract/year/authors/venue/doi/arxiv_id), so "who said this" is
  answerable from now on;
* registers ``papers.doi`` / ``papers.arxiv_id`` in ``paper_identifiers`` and the
  file digest as ``scheme='sha256'``;
* marks the primary file (``paper_files.is_primary``) so the primary-version rule
  has a starting point;
* leaves venue/edition and tags alone (the historical rows never had them) and
  **never touches ``papers.fingerprint``**.

Idempotent: running it twice changes nothing the second time (every write is keyed
on an existing row or skipped when the value is already recorded).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.models import Paper, PaperFieldProvenance, PaperIdentifier, PaperSource  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services import metadata_identifiers as identifiers  # noqa: E402
from app.services import metadata_merge as merge  # noqa: E402
from app.services import metadata_sources as sources  # noqa: E402
from app.services import paper_service, provenance_service  # noqa: E402

#: Fields the backfill claims, in the order they are written.
FIELDS: tuple[str, ...] = (
    "title",
    "abstract",
    "year",
    "authors",
    "identifier:doi",
    "identifier:arxiv",
)


def live_papers(session) -> list[Paper]:
    """Every live paper, oldest first (stable output for a dry run)."""
    return list(
        session.execute(
            select(Paper)
            .where(Paper.deleted_at.is_(None))
            .order_by(Paper.created_at.asc())
        ).scalars()
    )


def heuristic_raw(paper: Paper) -> dict:
    """The snapshot of what the heuristics put on this row."""
    return {
        "title": paper.title,
        "abstract": paper.abstract,
        "year": paper.year,
        "authors": paper_service.paper_author_names(paper),
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
    }


def current_values(paper: Paper) -> dict[str, object]:
    """The claim values this paper already holds (skip what is not there)."""
    values: dict[str, object] = {}
    if (paper.title or "").strip():
        values["title"] = paper.title
    if (paper.abstract or "").strip():
        values["abstract"] = paper.abstract
    if paper.year:
        values["year"] = paper.year
    authors = paper_service.paper_author_names(paper)
    if authors:
        values["authors"] = authors
    if paper.doi:
        values["identifier:doi"] = paper.doi
    if paper.arxiv_id:
        values["identifier:arxiv"] = paper.arxiv_id
    return values


def _owned_by_another_paper(session, scheme: str, value: str, paper_id: str) -> bool:
    """Whether this identifier is already claimed by a *different* paper.

    ``paper_identifiers`` allows one row per ``(scheme, normalized_value)``: when two
    live papers carry the same DOI/arXiv id the library has a duplicate, and the
    backfill must report it instead of failing on the unique index.
    """
    normalized = identifiers.normalize_identifier(scheme, value)
    if not normalized:
        return False
    row = identifiers.find_identifier(session, scheme, normalized)
    return row is not None and row.paper_id != paper_id


def backfill_paper(session, paper: Paper, *, dry_run: bool) -> dict[str, int]:
    """Backfill one paper; returns the counters it contributed."""
    counters = {"sources": 0, "claims": 0, "identifiers": 0, "primary_files": 0, "conflicts": 0}
    source_ref = sources.paper_heuristic_ref(paper.id)
    existing = sources.find_source(
        session, sources.SOURCE_TYPE_PDF_HEURISTIC, source_ref
    )
    if existing is None:
        counters["sources"] = 1
        if not dry_run:
            existing = sources.upsert_source(
                session,
                source_type=sources.SOURCE_TYPE_PDF_HEURISTIC,
                source_ref=source_ref,
                raw=heuristic_raw(paper),
                paper_id=paper.id,
                match_status=sources.MATCH_STATUS_MATCHED,
                match_method="heuristic",
                match_confidence=0.5,
                importer="scripts/backfill_metadata.py",
            )
    source_id = existing.id if existing is not None else None

    values = current_values(paper)
    for field, value in values.items():
        if dry_run:
            claim = provenance_service.current_claim(session, paper.id, field)
            if claim is None:
                counters["claims"] += 1
            continue
        before = provenance_service.current_claim(session, paper.id, field)
        claim = provenance_service.record_claim(
            session,
            paper_id=paper.id,
            field=field,
            value=value,
            source_id=source_id,
            confidence=0.5,
            make_current=before is None,
        )
        if before is None and claim is not None:
            counters["claims"] += 1

    # Identifiers: the two mirror columns plus the file digest.
    known = {
        row.scheme for row in identifiers.identifiers_for_paper(session, paper.id)
    }
    for scheme, value in (
        (identifiers.SCHEME_DOI, paper.doi),
        (identifiers.SCHEME_ARXIV, paper.arxiv_id),
    ):
        if not value or scheme in known:
            continue
        if _owned_by_another_paper(session, scheme, value, paper.id):
            # The dedupe floor: one identifier belongs to one paper, so a second
            # paper claiming the same DOI/arXiv id is a real duplicate that needs a
            # human, not a second identifier row.
            counters["conflicts"] = counters.get("conflicts", 0) + 1
            print(
                f"  WARNING: {scheme} {value} already belongs to another paper "
                f"(skipped for {paper.id})"
            )
            continue
        counters["identifiers"] += 1
        if not dry_run:
            identifiers.upsert_identifier(
                session,
                paper_id=paper.id,
                scheme=scheme,
                value=value,
                first_source_id=source_id,
            )
    file_record = paper_service.original_file(paper)
    digest = getattr(file_record, "sha256", None) if file_record is not None else None
    if digest and identifiers.SCHEME_SHA256 not in known:
        if _owned_by_another_paper(
            session, identifiers.SCHEME_SHA256, digest, paper.id
        ):
            counters["conflicts"] = counters.get("conflicts", 0) + 1
        else:
            counters["identifiers"] += 1
            if not dry_run:
                identifiers.upsert_identifier(
                    session,
                    paper_id=paper.id,
                    scheme=identifiers.SCHEME_SHA256,
                    value=digest,
                    first_source_id=source_id,
                )
    if not dry_run and (paper.doi or paper.arxiv_id):
        identifiers.refresh_primary(session, paper.id)

    # The primary file of the paper (single-file papers: the one it has).
    if not dry_run and paper_service.primary_file(paper) is None:
        files = paper_service.list_paper_files(session, paper.id)
        if files:
            files[0].is_primary = True
            session.flush()
            counters["primary_files"] = 1

    if not dry_run:
        session.flush()
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    parser.add_argument("--limit", type=int, default=None, help="stop after N papers")
    args = parser.parse_args()

    session = SessionLocal()
    totals = {
        "papers": 0,
        "sources": 0,
        "claims": 0,
        "identifiers": 0,
        "primary_files": 0,
        "conflicts": 0,
    }
    fingerprints: dict[str, str] = {}
    try:
        papers = live_papers(session)
        if args.limit is not None:
            papers = papers[: max(0, args.limit)]
        print(f"{'dry run: ' if args.dry_run else ''}backfilling {len(papers)} live paper(s)")
        for paper in papers:
            before = paper.fingerprint
            counters = backfill_paper(session, paper, dry_run=args.dry_run)
            totals["papers"] += 1
            for key, value in counters.items():
                totals[key] = totals.get(key, 0) + value
            after = paper.fingerprint
            if before != after:
                fingerprints[paper.id] = f"{before} -> {after}"
            print(
                f"  {'would backfill' if args.dry_run else 'ok'} {paper.id} "
                f"({counters['claims']} claim(s), {counters['identifiers']} identifier(s))"
            )
        if not args.dry_run:
            session.commit()
        print(
            f"papers={totals['papers']} sources={totals['sources']} claims={totals['claims']} "
            f"identifiers={totals['identifiers']} primary_files={totals['primary_files']} "
            f"duplicate_identifiers={totals['conflicts']}"
        )
        if fingerprints:
            print(f"WARNING: {len(fingerprints)} fingerprint(s) changed:")
            for paper_id, change in fingerprints.items():
                print(f"  {paper_id}: {change}")
        elif not args.dry_run:
            print("fingerprints unchanged (as expected)")
        if args.dry_run:
            print("dry run complete, nothing was changed")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())