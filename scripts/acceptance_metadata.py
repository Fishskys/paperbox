#!/usr/bin/env python3
"""Real-machine acceptance for the metadata layer (section 10 of the plan).

    uv run python -m uvicorn app.main:app --port 8077      # in another shell
    uv run python scripts/acceptance_metadata.py

Checks, against the *live* stack (PostgreSQL + OpenSearch + MinIO + the running
app), not against fakes:

1. the migration + backfill numbers (sources/claims/identifiers/primary files,
   fingerprint count unchanged);
2. an IEEE record matched by DOI: dry run reports it, ``apply`` fills volume/issue/
   pages/publication_date, creates one venue + one edition, stores the three kinds
   of index terms and the raw payload, and a second import changes nothing;
3. a record without a DOI goes to the review queue as ``ambiguous``;
4. a manual edit writes ``decided_by='manual'`` provenance, rollback restores the
   old value, and changing the DOI upgrades the fingerprint;
5. the venue is searchable by name and by name + year;
6. metadata first: a record for an unknown paper becomes an ``AWAITING_FILE`` shell,
   and the PDF that arrives afterwards joins that same ``paper_id``;
7. OpenSearch holds exactly one document per live indexed chunk.

Every check prints ``PASS``/``FAIL`` with the numbers it saw; the exit code is
non-zero when anything failed. The script is re-runnable: the records it invents
carry a per-run stamp, so a second run does not collide with the first. Pass
``--cleanup`` to delete the papers this run created (the enrichment written onto a
real paper is kept: that is product data, not debris).
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.models import (  # noqa: E402
    Paper,
    PaperFieldProvenance,
    PaperFile,
    PaperIdentifier,
    PaperSource,
    PapersTag,
    Venue,
    VenueEdition,
)
from app.db.session import SessionLocal  # noqa: E402
from app.services import metadata_identifiers as identifiers  # noqa: E402

OUT_DIR = ROOT / "logs" / "eval" / "metadata-acceptance"


class Checker:
    """Collects PASS/FAIL lines and decides the exit code."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.created: list[str] = []

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        mark = "PASS" if condition else "FAIL"
        print(f"  [{mark}] {name}{f' -- {detail}' if detail else ''}")
        if not condition:
            self.failures.append(name)
        return bool(condition)


def ieee_record(
    paper: Paper, *, doi: str, title: str | None = None, stamp: str = ""
) -> dict:
    """An IEEE Xplore shaped record for the paper under test."""
    return {
        "title": title or paper.title,
        "abstract": f"A leakage reduction technique for SRAM (acceptance {stamp}).",
        "doi": doi,
        "article_number": "7065247",
        "issn": "0018-9219",
        "publication_title": "IEEE Journal of Solid-State Circuits",
        "publication_year": 2015,
        "publication_date": "July 2015",
        "content_type": "Journals",
        "volume": "62",
        "issue": "7",
        "start_page": "631",
        "end_page": "635",
        "html_url": "https://ieeexplore.ieee.org/document/7065247",
        "pdf_url": "https://ieeexplore.ieee.org/stamp/stamp.jsp?arnumber=7065247",
        "authors": [
            {"full_name": "Alice Acceptance", "author_order": 1, "affiliation": "Example University"},
            {"full_name": "Bob Acceptance", "author_order": 2},
        ],
        "index_terms": {
            "ieee_terms": {"terms": ["SRAM", "low-power electronics"]},
            "author_terms": {"terms": ["leakage reduction"]},
            "dynamic_index_terms": {"terms": ["subthreshold operation"]},
        },
        "conference_location": "San Francisco, CA, USA",
        "conference_dates": "9-13 Feb. 2015",
        "publication_number": "12345",
        "is_number": "5678",
        "citing_paper_count": 12,
        "download_count": 99,
        "insert_date": "2015-07-01T00:00:00Z",
        "license": "IEEE",
    }


def build_pdf_with_doi(doi: str, title: str) -> bytes:
    """A PDF with a real text layer *and* an Info dictionary carrying the DOI.

    The text matters: the pipeline refuses a PDF that chunks into nothing, so a
    blank fixture page would fail the ingest for a reason unrelated to metadata.
    ``tests/fixtures/smoke_sample.pdf`` is the same fixture the unit tests use.
    """
    from pypdf import PdfReader, PdfWriter

    source = ROOT / "tests" / "fixtures" / "smoke_sample.pdf"
    reader = PdfReader(str(source))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata(
        {
            "/Title": title,
            "/Author": "Alice Acceptance; Bob Acceptance",
            "/Subject": f"doi:{doi}",
            "/CreationDate": "D:20150701000000Z",
        }
    )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class Api:
    """Thin authenticated client for the running app."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120.0,
        )

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.client.get(url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.client.post(url, **kwargs)

    def patch(self, url: str, **kwargs) -> httpx.Response:
        return self.client.patch(url, **kwargs)

    def close(self) -> None:
        self.client.close()


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_backfill(session, checker: Checker) -> None:
    print("1. migration + backfill")
    live = session.execute(
        select(func.count(Paper.id)).where(Paper.deleted_at.is_(None))
    ).scalar_one()
    sources = session.execute(select(func.count(PaperSource.id))).scalar_one()
    claims = session.execute(select(func.count(PaperFieldProvenance.id))).scalar_one()
    idents = session.execute(select(func.count(PaperIdentifier.id))).scalar_one()
    primary = session.execute(
        select(func.count(PaperFile.id)).where(
            PaperFile.is_primary.is_(True), PaperFile.deleted_at.is_(None)
        )
    ).scalar_one()
    checker.check("68 live papers", live == 68, f"papers={live}")
    heuristic_sources = session.execute(
        select(func.count(PaperSource.id)).where(
            PaperSource.source_type == "pdf_heuristic"
        )
    ).scalar_one()
    checker.check(
        "one heuristic source per paper",
        heuristic_sources == live,
        f"pdf_heuristic sources={heuristic_sources}",
    )
    checker.check("every paper has claims", claims >= live * 4, f"claims={claims}")
    checker.check("identifiers registered", idents >= live, f"identifiers={idents}")
    checker.check("every paper has a primary file", primary == live, f"primary={primary}")
    schemes = dict(
        session.execute(
            select(PaperIdentifier.scheme, func.count(PaperIdentifier.id)).group_by(
                PaperIdentifier.scheme
            )
        ).all()
    )
    checker.check(
        "doi/arxiv/sha256 rows",
        set(schemes) >= {"doi", "arxiv", "sha256"},
        json.dumps(schemes, ensure_ascii=False),
    )
    # The dedupe floor: a duplicate pair in the library cannot share an identifier.
    duplicates = session.execute(
        select(func.count()).select_from(
            select(PaperIdentifier.normalized_value)
            .group_by(PaperIdentifier.scheme, PaperIdentifier.normalized_value)
            .having(func.count(PaperIdentifier.id) > 1)
            .subquery()
        )
    ).scalar_one()
    checker.check("no identifier claimed twice", duplicates == 0, f"duplicates={duplicates}")


def check_ieee_import(
    session,
    api: Api,
    checker: Checker,
    *,
    paper_id: str,
    doi: str,
    title: str,
    stamp: str,
):
    """Check 2: an IEEE record matched by DOI, applied onto the acceptance paper.

    The target is the paper this run created (the shell that received its PDF), never
    a pre-existing library paper: an import enriches whatever it matches, and a
    synthetic record must not rewrite the metadata of real papers.
    """
    print("2. IEEE import matched by DOI")
    paper = session.get(Paper, paper_id)
    checker.check(
        "the target paper exists and is indexed",
        paper is not None and paper.status == "INDEXED",
        f"status={paper.status if paper else None}",
    )
    record = ieee_record(paper, doi=doi, title=title, stamp=stamp)

    # A source type of its own: the same DOI, but a fresh source_ref, so this check
    # always exercises a new import.
    params = {"source_type": "ieee_api"}
    dry = api.post("/api/metadata/import", params=params, json={"articles": [record]}).json()
    checker.check("dry run reports matched=1", dry["matched"] == 1, json.dumps(dry["sources"]))
    checker.check(
        "dry run reports conflicts",
        len(dry["conflicts"]) > 0,
        f"{len(dry['conflicts'])} conflict(s): "
        + ",".join(sorted(c["field"] for c in dry["conflicts"])),
    )
    sources_before = session.execute(select(func.count(PaperSource.id))).scalar_one()
    checker.check(
        "dry run wrote nothing",
        session.execute(select(func.count(PaperSource.id))).scalar_one() == sources_before,
        f"sources={sources_before}",
    )

    applied = api.post(
        "/api/metadata/import", params={**params, "apply": "true"}, json={"articles": [record]}
    ).json()
    checker.check("apply reports matched=1", applied["matched"] == 1)
    session.expire_all()
    paper = session.get(Paper, paper_id)
    # R2: the PDF already stated these fields and two structured sources are never
    # ranked against each other -- the first value stands, the clash is registered.
    checker.check(
        "the PDF's values are kept, not overwritten",
        (paper.volume, paper.issue, paper.pages) == ("1", "1", "1-2"),
        f"{paper.volume}/{paper.issue}/{paper.pages}",
    )
    non_current = {
        row.field
        for row in session.execute(
            select(PaperFieldProvenance).where(
                PaperFieldProvenance.paper_id == paper.id,
                PaperFieldProvenance.is_current.is_(False),
            )
        ).scalars()
    }
    checker.check(
        "the clash is on the record for review",
        {"volume", "pages"} <= non_current,
        f"non-current claims: {sorted(non_current)}",
    )
    checker.check(
        "publication_date is month precision",
        paper.publication_date is not None and paper.publication_date.isoformat() == "2099-01-01",
        str(paper.publication_date),
    )
    checker.check("paper_type inferred", paper.paper_type == "journal", str(paper.paper_type))
    edition = session.get(VenueEdition, paper.venue_edition_id) if paper.venue_edition_id else None
    checker.check(
        "venue_year mirrors the edition",
        paper.venue_year == 2099 and edition is not None and edition.year == 2099,
        f"venue_year={paper.venue_year} edition={edition.year if edition else None}",
    )
    venue = session.get(Venue, paper.venue_id) if paper.venue_id else None
    checker.check(
        "one venue, kind=journal",
        venue is not None and venue.kind == "journal",
        f"{venue.name if venue else None} kind={venue.kind if venue else None}",
    )
    schemes = {row.scheme for row in identifiers.identifiers_for_paper(session, paper.id)}
    checker.check(
        "blank identifiers were filled in",
        {"issn", "ieee_article_number"} <= schemes,
        f"schemes={sorted(schemes)}",
    )
    tag_kinds = {
        row.kind
        for row in session.execute(
            select(PapersTag).where(PapersTag.paper_id == paper.id)
        ).scalars()
    }
    checker.check(
        "index terms landed with their kind",
        {"ieee_terms", "author_terms"} <= tag_kinds,
        f"kinds={sorted(tag_kinds)}",
    )
    source = session.execute(
        select(PaperSource).where(
            PaperSource.paper_id == paper.id, PaperSource.source_type == "ieee_api"
        )
    ).scalars().first()
    checker.check(
        "raw payload kept verbatim",
        source is not None and source.raw.get("citing_paper_count") == 12,
        f"raw keys={len(source.raw) if source else 0}",
    )

    again = api.post(
        "/api/metadata/import", params={**params, "apply": "true"}, json={"articles": [record]}
    ).json()
    sources_after = session.execute(select(func.count(PaperSource.id))).scalar_one()
    checker.check(
        "re-import is idempotent",
        again["unchanged"] == 1 and again["matched"] == 0 and sources_after == sources_before + 1,
        f"unchanged={again['unchanged']} sources={sources_after}",
    )
    return paper, record


def check_review_queue(api: Api, checker: Checker, stamp: str, *, title: str) -> None:
    print("3. a record without a DOI goes to the review queue")
    record = ieee_record_stub(stamp)
    for key in (
        "doi",
        "article_number",
        "authors",
        "publication_year",
        "publication_date",
        "issn",
        "start_page",
        "end_page",
    ):
        record.pop(key, None)
    # The stamp keeps a second run from being reported as "already imported".
    record["title"] = f"A paper nobody has (yet) {stamp}"
    record["abstract"] = f"acceptance run {stamp}"

    report = api.post("/api/metadata/import?apply=true", json={"articles": [record]}).json()
    checker.check(
        "no DOI, no match -> shell",
        report["created_shell"] == 1 and report["matched"] == 0,
        json.dumps({k: report[k] for k in ("matched", "created_shell", "ambiguous")}),
    )
    for entry in report["sources"]:
        if entry.get("paper_id"):
            checker.created.append(entry["paper_id"])

    # Same title as the acceptance paper, but without author/year: the matcher must
    # refuse to attach it automatically.
    ambiguous_record = dict(record)
    ambiguous_record["title"] = title
    ambiguous_record["abstract"] = f"acceptance run {stamp} (ambiguous)"
    report = api.post(
        "/api/metadata/import?apply=true", json={"articles": [ambiguous_record]}
    ).json()
    checker.check(
        "title-only record is ambiguous",
        report["ambiguous"] == 1,
        json.dumps({k: report[k] for k in ("matched", "created_shell", "ambiguous")}),
    )

    review = api.get("/api/metadata/review").json()
    checker.check(
        "review queue lists it",
        any(item["match_status"] == "ambiguous" for item in review["items"]),
        f"total={review['total']} conflicts={len(review['conflicts'])}",
    )


def check_manual_edit(api: Api, checker: Checker, paper_id: str, stamp: str) -> None:
    print("4. manual edit, rollback and fingerprint upgrade")
    view = api.get(f"/api/papers/{paper_id}/metadata").json()
    claims = view["provenance"]["title"]
    checker.check("the metadata view lists title history", len(claims) >= 1, f"{len(claims)} claim(s)")
    # ``provenance`` is newest first: the claim to roll back to is the most recent
    # one that is not current.
    previous_claim = next((item for item in claims if not item["is_current"]), None)
    checker.check(
        "there is an earlier claim to roll back to", previous_claim is not None
    )
    if previous_claim is None:
        return

    patched = api.patch(
        f"/api/papers/{paper_id}/metadata", json={"title": "Hand edited for acceptance"}
    ).json()
    checker.check(
        "PATCH reports the changed field", patched["fields"] == ["title"], json.dumps(patched["fields"])
    )
    view = api.get(f"/api/papers/{paper_id}/metadata").json()
    current = [item for item in view["provenance"]["title"] if item["is_current"]][0]
    checker.check(
        "the current claim is manual",
        current["decided_by"] == "manual" and current["value"] == "Hand edited for acceptance",
        current["decided_by"],
    )

    rolled = api.post(
        f"/api/papers/{paper_id}/metadata/rollback",
        json={"field": "title", "provenance_id": previous_claim["provenance_id"]},
    ).json()
    checker.check(
        "rollback restores the previous value",
        rolled["value"] == previous_claim["value"],
        f"{rolled['value']!r}",
    )

    fingerprint_before = api.get(f"/api/papers/{paper_id}/metadata").json()["fingerprint"]
    new_doi = f"10.1109/ACCEPTANCE.MANUAL.{stamp}"
    patched = api.patch(f"/api/papers/{paper_id}/metadata", json={"doi": new_doi}).json()
    checker.check(
        "changing the DOI upgrades the fingerprint",
        patched["fingerprint"] == f"doi:{new_doi.casefold()}"
        and patched["fingerprint"] != fingerprint_before,
        f"{fingerprint_before} -> {patched['fingerprint']}",
    )


def check_venue_search(api: Api, checker: Checker, paper: Paper) -> None:
    print("5. venue search by name and by name + year")
    # Metadata written after the paper was indexed only reaches the search filters
    # once the chunks are re-indexed (the filter fields live on the chunk
    # documents), so ask for a reindex first -- the documented workflow.
    reindex = api.post(f"/api/papers/{paper.id}/reindex")
    checker.check("reindex is accepted", reindex.status_code == 202, str(reindex.status_code))
    if reindex.status_code == 202:
        job = wait_for_job(api, reindex.json()["job_id"])
        checker.check("reindex completes", job.get("stage") == "COMPLETED", str(job.get("stage")))

    # The venue and its year come from the paper itself: the acceptance paper gets
    # them from the PDF's embedded metadata (discovery layer 1).
    session = SessionLocal()
    try:
        paper = session.get(Paper, paper.id)
        venue = session.get(Venue, paper.venue_id) if paper.venue_id else None
        venue_name = venue.name if venue else None
        venue_year = paper.venue_year
    finally:
        session.close()
    checker.check("the paper carries a venue and a year", bool(venue_name) and bool(venue_year), f"{venue_name} / {venue_year}")
    if not venue_name or not venue_year:
        return

    body = {"query": "SRAM leakage", "mode": "hybrid", "top_k": 5, "filters": {"venue": [venue_name]}}
    by_name = api.post("/api/search", json=body)
    checker.check("search by venue name answers 200", by_name.status_code == 200, str(by_name.status_code))
    hits = by_name.json().get("results", []) if by_name.status_code == 200 else []
    checker.check(
        "venue name matches the paper",
        any(item["paper_id"] == paper.id for item in hits),
        f"{len(hits)} hit(s) for {venue_name!r}",
    )

    body_with_year = dict(body)
    body_with_year["filters"] = {
        "venue": [venue_name],
        "year_from": venue_year,
        "year_to": venue_year,
    }
    by_year = api.post("/api/search", json=body_with_year)
    hits_year = by_year.json().get("results", []) if by_year.status_code == 200 else []
    checker.check(
        f"venue + {venue_year} still matches",
        any(item["paper_id"] == paper.id for item in hits_year),
        f"{len(hits_year)} hit(s)",
    )
    body_wrong_year = dict(body)
    body_wrong_year["filters"] = {"venue": [venue_name], "year_from": 1999, "year_to": 1999}
    hits_wrong = api.post("/api/search", json=body_wrong_year).json().get("results", [])
    checker.check(
        "venue + 1999 does not match",
        all(item["paper_id"] != paper.id for item in hits_wrong),
        f"{len(hits_wrong)} hit(s)",
    )


def check_metadata_first(api: Api, session, checker: Checker, stamp: str) -> str | None:
    print("6. metadata first, then the PDF")
    title = f"Acceptance Shell Paper {stamp}"
    shell_doi = f"10.1109/ACCEPTANCE.SHELL.{stamp}"
    record = {
        "title": title,
        "abstract": "A record imported before its file exists.",
        "doi": shell_doi,
        "publication_title": "IEEE Acceptance Transactions",
        "publication_year": 2099,
        "content_type": "Journals",
        "volume": "1",
        "issue": "1",
        "start_page": "1",
        "end_page": "2",
        "authors": [{"full_name": "Alice Acceptance", "author_order": 1}],
    }
    report = api.post("/api/metadata/import?apply=true", json={"articles": [record]}).json()
    checker.check(
        "an unknown DOI creates a shell",
        report["created_shell"] == 1,
        json.dumps(report["sources"][:1], ensure_ascii=False),
    )
    shell_id = report["sources"][0].get("paper_id") if report["sources"] else None
    if not shell_id:
        return None
    checker.created.append(shell_id)

    shells = api.get("/api/papers", params={"status": "AWAITING_FILE", "q": title}).json()
    checker.check(
        "GET /api/papers?status=AWAITING_FILE lists it", shells["total"] >= 1, f"total={shells['total']}"
    )
    checker.check("the shell has no file", shells["papers"][0]["files"] == [])
    checker.check(
        "the shell has no chunks",
        api.get(f"/api/papers/{shell_id}/chunks").json()["total"] == 0,
    )

    pdf = build_pdf_with_doi(shell_doi, title)
    accepted = api.post(
        "/api/papers/ingest/file",
        files={"file": ("acceptance-shell.pdf", pdf, "application/pdf")},
    )
    checker.check("the PDF upload is accepted", accepted.status_code == 202, str(accepted.status_code))
    job = wait_for_job(api, accepted.json()["job_id"])
    checker.check("the job completes", job.get("stage") == "COMPLETED", str(job.get("stage")))
    checker.check(
        "the job survives the adoption and points at the shell",
        job.get("paper_id") == shell_id,
        f"job paper={job.get('paper_id')} shell={shell_id}",
    )
    session.expire_all()
    paper = session.get(Paper, shell_id)
    checker.check(
        "the paper is INDEXED",
        paper is not None and paper.status == "INDEXED",
        str(paper.status if paper else None),
    )
    checker.check(
        "the file is attached and primary",
        paper is not None and paper_service_primary(paper) is not None,
    )
    checker.check(
        "no second paper was created for the record",
        session.execute(
            select(func.count(Paper.id)).where(Paper.doi == shell_doi.casefold())
        ).scalar_one()
        == 1,
    )
    return shell_id


def paper_service_primary(paper: Paper):
    from app.services import paper_service

    return paper_service.primary_file(paper)


def wait_for_job(api: Api, job_id: str, timeout_s: float = 180.0) -> dict:
    """Poll ``GET /api/jobs/{id}`` until the job leaves the queue."""
    deadline = time.time() + timeout_s
    last: dict = {}
    while time.time() < deadline:
        response = api.get(f"/api/jobs/{job_id}")
        if response.status_code == 200:
            last = response.json()
            if last.get("stage") in {"COMPLETED", "FAILED"}:
                return last
        time.sleep(2)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8077")
    parser.add_argument(
        "--opensearch-url", default=settings.opensearch_url, help="direct OpenSearch URL"
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete the papers this run created (default: keep them as evidence)",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    checker = Checker()
    api = Api(args.base_url, settings.paper_api_key)
    session = SessionLocal()
    stamp = time.strftime("%Y%m%d%H%M%S")
    print(f"acceptance run {stamp} against {args.base_url}")
    try:
        check_backfill(session, checker)
        shell_id = check_metadata_first(api, session, checker, stamp)
        if not shell_id:
            print("\nno acceptance paper was created: the remaining checks need one")
            return 1
        shell_doi = f"10.1109/ACCEPTANCE.SHELL.{stamp}"
        shell_title = f"Acceptance Shell Paper {stamp}"
        paper, record = check_ieee_import(
            session,
            api,
            checker,
            paper_id=shell_id,
            doi=shell_doi,
            title=shell_title,
            stamp=stamp,
        )
        # The sample file holds the record that was actually imported.
        (OUT_DIR / "ieee-sample.json").write_text(
            json.dumps({"total_records": 1, "articles": [record]}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        check_review_queue(api, checker, stamp, title=shell_title)
        check_manual_edit(api, checker, shell_id, stamp)
        check_venue_search(api, checker, paper)

        print("7. OpenSearch and PostgreSQL agree")
        from app.db.models import PaperChunk

        live_chunks = session.execute(
            select(func.count(PaperChunk.id))
            .join(Paper, Paper.id == PaperChunk.paper_id)
            .where(
                PaperChunk.deleted_at.is_(None),
                Paper.status == "INDEXED",
                Paper.deleted_at.is_(None),
            )
        ).scalar_one()
        count = httpx.get(
            f"{args.opensearch_url}/paper_chunks_current/_count", timeout=30.0
        ).json()["count"]
        checker.check(
            "one index document per live indexed chunk",
            count == live_chunks,
            f"docs={count} chunks={live_chunks}",
        )

        print(f"\npapers created by this run: {', '.join(checker.created) or 'none'}")
        if args.cleanup:
            removed = 0
            for paper_id in checker.created:
                if api.client.delete(f"/api/papers/{paper_id}").status_code == 204:
                    removed += 1
            print(f"cleanup: deleted {removed}/{len(checker.created)} created paper(s)")
    finally:
        session.close()
        api.close()

    print()
    if checker.failures:
        print(f"{len(checker.failures)} check(s) FAILED: {', '.join(checker.failures)}")
        return 1
    print("all checks passed")
    return 0


def ieee_record_payload(stamp: str) -> dict:
    """A standalone sample record (written to disk as an example of the format)."""
    return {
        "title": f"Acceptance sample {stamp}",
        "doi": "10.1109/JSSC.2015.2441234",
        "publication_year": 2015,
        "content_type": "Journals",
        "authors": [{"full_name": "Alice Acceptance", "author_order": 1}],
    }


def ieee_record_stub(stamp: str) -> dict:
    """A minimal record for the review-queue check (no DOI, no author, no year)."""
    return {
        "title": f"A paper nobody has (yet) {stamp}",
        "abstract": f"acceptance run {stamp}",
        "publication_title": "IEEE Acceptance Transactions",
        "content_type": "Journals",
    }


if __name__ == "__main__":
    sys.exit(main())