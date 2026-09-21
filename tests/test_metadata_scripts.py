"""The two metadata scripts (backfill + CLI import).

The scripts are thin: the backfill walks the live rows through the same services the
pipeline uses, and the CLI prints/writes the report the API returns. What matters
here is that the backfill is **idempotent** and never touches ``papers.fingerprint``,
and that the CLI can be driven without a live PostgreSQL (its session factory is
patched onto SQLite).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.db.models import (
    Paper,
    PaperFieldProvenance,
    PaperFile,
    PaperIdentifier,
    PaperSource,
)
from app.services import metadata_identifiers as ids
from app.services import paper_service
from scripts import backfill_metadata, import_metadata

DOI = "10.1109/JSSC.2015.2441234"
SHA = "a" * 64

IEEE_SAMPLE = {
    "total_records": 1,
    "articles": [
        {
            "title": "A 0.6 V Low Power SRAM with Leakage Reduction",
            "doi": DOI,
            "article_number": "7065247",
            "publication_title": "IEEE Journal of Solid-State Circuits",
            "publication_year": 2015,
            "publication_date": "July 2015",
            "content_type": "Journals",
            "volume": "62",
            "issue": "7",
            "start_page": "631",
            "end_page": "635",
            "authors": [{"full_name": "Alice Smith", "author_order": 1}],
        }
    ],
}


def make_legacy_paper(session, *, with_doi=True, with_arxiv=False) -> Paper:
    """A paper exactly as the pre-metadata pipeline left it."""
    paper = Paper(
        id=paper_service.new_uuid(),
        title="Low Power SRAM Leakage Reduction",
        abstract="An abstract",
        year=2015,
        doi=DOI if with_doi else None,
        arxiv_id="1710.07153" if with_arxiv else None,
        fingerprint=f"sha256:{paper_service.new_uuid()}",
        status="INDEXED",
    )
    session.add(paper)
    session.flush()
    paper_service.set_paper_authors(session, paper, ["Alice Smith", "Bob Jones"])
    session.add(
        PaperFile(
            id=paper_service.new_uuid(),
            paper_id=paper.id,
            kind="original",
            object_key=f"papers/{paper.id}/original.pdf",
            bucket="paperbox",
            filename="original.pdf",
            content_type="application/pdf",
            size_bytes=10,
            sha256=SHA,
        )
    )
    session.flush()
    session.expire(paper, ["files"])
    return paper


# --------------------------------------------------------------------------- #
# backfill
# --------------------------------------------------------------------------- #
def test_backfill_creates_the_heuristic_source_and_claims(db_session) -> None:
    paper = make_legacy_paper(db_session)
    fingerprint_before = paper.fingerprint

    counters = backfill_metadata.backfill_paper(db_session, paper, dry_run=False)

    assert counters["sources"] == 1
    assert counters["claims"] >= 5
    source = db_session.query(PaperSource).one()
    assert source.source_type == "pdf_heuristic"
    assert source.source_ref == f"paper:{paper.id}:heuristic"
    assert source.raw["doi"] == DOI
    assert source.match_status == "matched"
    fields = {row.field for row in db_session.query(PaperFieldProvenance).all()}
    assert {"title", "abstract", "year", "authors", "identifier:doi"} <= fields
    assert paper.fingerprint == fingerprint_before, "the fingerprint must not move"


def test_backfill_registers_the_identifiers_and_the_primary_file(db_session) -> None:
    paper = make_legacy_paper(db_session)

    backfill_metadata.backfill_paper(db_session, paper, dry_run=False)

    schemes = {
        row.scheme: row for row in db_session.query(PaperIdentifier).all()
    }
    assert set(schemes) == {"doi", "sha256"}
    assert schemes["doi"].is_primary is True
    assert schemes["sha256"].value == SHA
    assert paper_service.primary_file(paper) is not None


def test_backfill_is_idempotent(db_session) -> None:
    paper = make_legacy_paper(db_session)

    first = backfill_metadata.backfill_paper(db_session, paper, dry_run=False)
    counts = (
        db_session.query(PaperSource).count(),
        db_session.query(PaperFieldProvenance).count(),
        db_session.query(PaperIdentifier).count(),
    )
    second = backfill_metadata.backfill_paper(db_session, paper, dry_run=False)

    assert first["sources"] == 1
    assert second == {"sources": 0, "claims": 0, "identifiers": 0, "primary_files": 0}
    assert counts == (
        db_session.query(PaperSource).count(),
        db_session.query(PaperFieldProvenance).count(),
        db_session.query(PaperIdentifier).count(),
    )


def test_backfill_dry_run_writes_nothing(db_session) -> None:
    paper = make_legacy_paper(db_session)

    counters = backfill_metadata.backfill_paper(db_session, paper, dry_run=True)

    assert counters["sources"] == 1
    assert counters["identifiers"] >= 1
    assert db_session.query(PaperSource).count() == 0
    assert db_session.query(PaperFieldProvenance).count() == 0
    assert db_session.query(PaperIdentifier).count() == 0
    assert paper_service.primary_file(paper) is None


def test_backfill_skips_a_paper_without_identifiers(db_session) -> None:
    paper = make_legacy_paper(db_session, with_doi=False)

    backfill_metadata.backfill_paper(db_session, paper, dry_run=False)

    schemes = {row.scheme for row in db_session.query(PaperIdentifier).all()}
    assert schemes == {"sha256"}, "only the file digest is registrable"


def test_backfill_handles_both_identifiers(db_session) -> None:
    paper = make_legacy_paper(db_session, with_doi=True, with_arxiv=True)

    backfill_metadata.backfill_paper(db_session, paper, dry_run=False)

    rows = {row.scheme: row for row in db_session.query(PaperIdentifier).all()}
    assert rows["doi"].is_primary is True, "DOI outranks arXiv"
    assert rows["arxiv"].is_primary is False


def test_backfill_only_walks_live_papers(db_session) -> None:
    from datetime import datetime, timezone

    make_legacy_paper(db_session)
    deleted = make_legacy_paper(db_session, with_doi=False)
    deleted.deleted_at = datetime.now(timezone.utc)
    db_session.flush()

    assert [paper.id for paper in backfill_metadata.live_papers(db_session)] != [
        deleted.id
    ]
    assert len(backfill_metadata.live_papers(db_session)) == 1


def test_backfill_main_reports_a_dry_run(db_session, monkeypatch, capsys) -> None:
    make_legacy_paper(db_session)
    db_session.commit()
    monkeypatch.setattr(backfill_metadata, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        backfill_metadata.sys, "argv", ["backfill_metadata.py", "--dry-run"]
    )

    exit_code = backfill_metadata.main()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "dry run: backfilling 1 live paper(s)" in output
    assert "would backfill" in output
    assert "dry run complete, nothing was changed" in output
    assert db_session.query(PaperSource).count() == 0


def test_backfill_main_commits_and_warns_about_fingerprint_changes(
    db_session, monkeypatch, capsys
) -> None:
    paper = make_legacy_paper(db_session)
    paper.fingerprint = "title:something else|alice smith|2015"
    db_session.commit()
    monkeypatch.setattr(backfill_metadata, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(backfill_metadata.sys, "argv", ["backfill_metadata.py"])

    exit_code = backfill_metadata.main()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "papers=1" in output
    assert "fingerprints unchanged" in output, "the backfill never rewrites fingerprints"


def test_backfill_respects_the_limit(db_session, monkeypatch, capsys) -> None:
    make_legacy_paper(db_session)
    make_legacy_paper(db_session, with_doi=False)
    db_session.commit()
    monkeypatch.setattr(backfill_metadata, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        backfill_metadata.sys, "argv", ["backfill_metadata.py", "--limit", "1"]
    )

    backfill_metadata.main()

    assert "backfilling 1 live paper" in capsys.readouterr().out
    assert db_session.query(PaperSource).count() == 1


# --------------------------------------------------------------------------- #
# CLI import
# --------------------------------------------------------------------------- #
def write_sample(tmp_path) -> Path:
    path = tmp_path / "ieee.json"
    path.write_text(json.dumps(IEEE_SAMPLE), encoding="utf-8")
    return path


def test_cli_import_dry_run_reports_without_writing(
    tmp_path, session_factory, monkeypatch, capsys
) -> None:
    path = write_sample(tmp_path)
    monkeypatch.setattr(import_metadata, "SessionLocal", session_factory)
    monkeypatch.setattr(import_metadata.sys, "argv", ["import_metadata.py", str(path)])

    exit_code = import_metadata.main()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "dry run (nothing written)" in output
    assert "shell=1" in output
    session = session_factory()
    try:
        assert session.query(Paper).count() == 0
    finally:
        session.close()


def test_cli_import_applies_and_writes_a_report(
    tmp_path, session_factory, monkeypatch, capsys
) -> None:
    path = write_sample(tmp_path)
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(import_metadata, "SessionLocal", session_factory)
    monkeypatch.setattr(
        import_metadata.sys,
        "argv",
        ["import_metadata.py", str(path), "--apply", "--report", str(report_path)],
    )

    exit_code = import_metadata.main()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "mode:      applied" in output
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["created_shell"] == 1
    assert report["dry_run"] is False
    session = session_factory()
    try:
        paper = session.query(Paper).one()
        assert paper.status == paper_service.STATUS_AWAITING_FILE
        assert paper.volume == "62"
    finally:
        session.close()


def test_cli_import_matches_an_existing_paper(
    tmp_path, session_factory, monkeypatch, capsys
) -> None:
    session = session_factory()
    try:
        paper = Paper(
            id=paper_service.new_uuid(),
            title="Placeholder",
            fingerprint=f"sha256:{paper_service.new_uuid()}",
            status="INDEXED",
        )
        session.add(paper)
        session.flush()
        ids.upsert_identifier(
            session, paper_id=paper.id, scheme=ids.SCHEME_DOI, value=DOI
        )
        session.commit()
        paper_id = paper.id
    finally:
        session.close()

    path = write_sample(tmp_path)
    monkeypatch.setattr(import_metadata, "SessionLocal", session_factory)
    monkeypatch.setattr(
        import_metadata.sys,
        "argv",
        ["import_metadata.py", str(path), "--apply"],
    )

    assert import_metadata.main() == 0

    output = capsys.readouterr().out
    assert "matched=1" in output
    assert f"paper={paper_id}" in output
    session = session_factory()
    try:
        assert session.get(Paper, paper_id).volume == "62"
    finally:
        session.close()


def test_cli_import_reports_a_missing_file(capsys) -> None:
    import sys

    argv = sys.argv
    sys.argv = ["import_metadata.py", "does-not-exist.json"]
    try:
        assert import_metadata.main() == 2
    finally:
        sys.argv = argv
    assert "no such file" in capsys.readouterr().err


def test_cli_import_reports_broken_json(tmp_path, session_factory, monkeypatch, capsys) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(import_metadata, "SessionLocal", session_factory)
    monkeypatch.setattr(import_metadata.sys, "argv", ["import_metadata.py", str(path)])

    assert import_metadata.main() == 2
    assert "cannot read" in capsys.readouterr().err


def test_print_report_summarizes_conflicts(tmp_path, capsys) -> None:
    from app.services import metadata_import as importer

    report = importer.ImportReport(total=2, matched=1, ambiguous=1, dry_run=True)
    report.conflicts.append(
        {"field": "title", "kept": "kept", "rejected": "rejected", "reason": "conflict"}
    )
    report.sources.append(
        {"source_ref": "doi:10.1/x", "match_status": "matched", "paper_id": "p1", "match_method": "doi"}
    )

    import_metadata.print_report(report, tmp_path / "ieee.json")

    output = capsys.readouterr().out
    assert "conflicts: 1" in output
    assert "kept='kept'" in output
    assert "doi:10.1/x" in output


@pytest.mark.parametrize("mode", ["dry", "apply"])
def test_backfill_reports_counts_in_both_modes(db_session, mode) -> None:
    paper = make_legacy_paper(db_session)

    counters = backfill_metadata.backfill_paper(db_session, paper, dry_run=mode == "dry")

    assert counters["claims"] == 5
    assert counters["primary_files"] == (0 if mode == "dry" else 1)