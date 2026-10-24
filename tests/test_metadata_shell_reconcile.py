"""Repair metadata-only shells created before complete DOI filenames were decisive."""

from __future__ import annotations

from app.db.models import IngestionJob, Paper, PaperFile, PaperSource, new_uuid
from app.services import metadata_shell, metadata_shell_reconcile, metadata_sources, paper_service


def _paper_with_file(session, *, filename: str, title: str = "Broken PDF title") -> Paper:
    paper = paper_service.create_paper(
        session,
        title=title,
        fingerprint=f"sha256:{new_uuid()}",
        status="INDEXED",
    )
    session.add(
        PaperFile(
            id=new_uuid(),
            paper_id=paper.id,
            kind="original",
            object_key=f"papers/{paper.id}/original.pdf",
            bucket="paperbox",
            filename=filename,
            content_type="application/pdf",
            size_bytes=10,
            sha256="a" * 64,
            is_primary=True,
        )
    )
    session.flush()
    return paper


def _shell(session, *, doi: str, title: str = "Authoritative title") -> Paper:
    paper, _source = metadata_shell.create_shell(
        session,
        {
            "title": title,
            "year": 2025,
            "authors": ["Alice Smith"],
            "identifier:doi": doi,
        },
        source_type=metadata_sources.SOURCE_TYPE_IMPORT_FILE,
        source_ref=f"doi:{doi}",
        raw={
            "title": title,
            "publication_year": 2025,
            "authors": [{"full_name": "Alice Smith", "author_order": 1}],
            "doi": doi,
            "publication_title": "Example Journal",
            "issn": "1234-5678",
            "article_number": "1234567",
        },
    )
    return paper


def test_dry_run_reports_the_exact_plan_without_writing(db_session) -> None:
    doi = "10.1109/example.2025.1234567"
    target = _paper_with_file(
        db_session, filename="10.1109_example.2025.1234567.pdf"
    )
    shell = _shell(db_session, doi=doi)

    report = metadata_shell_reconcile.reconcile_shells(db_session)

    assert (report.merged, report.deleted, report.skipped, report.failed) == (1, 0, 0, 0)
    assert report.items[0]["paper_id"] == target.id
    assert db_session.get(Paper, shell.id) is not None
    assert target.doi is None
    assert db_session.query(PaperSource).filter_by(paper_id=shell.id).count() == 1


def test_apply_merges_the_source_and_deletes_the_shell(db_session) -> None:
    doi = "10.1109/example.2025.1234567"
    target = _paper_with_file(
        db_session, filename="10.1109_example.2025.1234567.pdf"
    )
    shell = _shell(db_session, doi=doi)

    report = metadata_shell_reconcile.reconcile_shells(db_session, apply=True)

    assert (report.merged, report.deleted, report.failed) == (1, 0, 0)
    assert db_session.get(Paper, shell.id) is None
    assert target.doi == doi
    assert target.title == "Authoritative title"
    assert db_session.query(PaperSource).filter_by(paper_id=target.id).count() == 1
    assert target.venue is not None and target.venue.issn == "1234-5678"


def test_apply_deletes_an_unmatched_empty_shell(db_session) -> None:
    shell = _shell(db_session, doi="10.1109/missing.2025.1")

    report = metadata_shell_reconcile.reconcile_shells(db_session, apply=True)

    assert (report.merged, report.deleted, report.failed) == (0, 1, 0)
    assert db_session.get(Paper, shell.id) is None


def test_a_shell_with_an_ingestion_job_is_skipped(db_session) -> None:
    shell = _shell(db_session, doi="10.1109/pending.2025.1")
    db_session.add(
        IngestionJob(
            id=new_uuid(),
            paper_id=shell.id,
            kind="ingest",
            stage="RECEIVED",
            progress=0,
            payload={},
        )
    )
    db_session.flush()

    report = metadata_shell_reconcile.reconcile_shells(db_session, apply=True)

    assert (report.skipped, report.deleted, report.failed) == (1, 0, 0)
    assert db_session.get(Paper, shell.id) is not None
