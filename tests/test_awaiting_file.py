"""Metadata first, PDF later (import order B, section 10).

The promise being tested: importing a record that is not in the library yet creates
a **shell** paper, and when its PDF finally arrives the pipeline reuses that same
``paper_id`` instead of creating a second paper -- so the identifiers, provenance
and review history of the import stay attached to one paper.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db.models import (
    IngestionJob,
    Paper,
    PaperChunk,
    PaperFile,
    PaperSource,
    new_uuid,
)
from app.parsing.pdf import PageText
from app.services import ingestion_service as ingest
from app.services import metadata_identifiers as ids
from app.services import metadata_shell, metadata_sources, paper_service, provenance_service
from app.workers import tasks

DOI = "10.1109/JSSC.2020.1234567"

FIRST_PAGE = (
    "Low Power SRAM Leakage Reduction for Deep Submicron Designs\n"
    "Alice Smith, Bob Jones\n"
    "Department of Electrical Engineering, Example University\n"
    "Abstract—This paper presents a leakage reduction technique. 2015\n"
    "Index Terms—SRAM, leakage\n"
)
SHELL_TITLE = "Low Power SRAM Leakage Reduction for Deep Submicron Designs"


def make_pdf(info: dict[str, str] | None = None) -> bytes:
    """A tiny real PDF (built with pypdf) carrying an optional Info dictionary."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if info:
        writer.add_metadata(info)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_temp_paper(session, *, title="low power sram.pdf") -> Paper:
    """The throwaway paper an ingest creates before it knows any better."""
    paper = paper_service.create_paper(
        session,
        title=title,
        fingerprint=f"sha256:{new_uuid()}",
        status=paper_service.STATUS_PENDING,
    )
    return paper


def make_job(session, **payload) -> IngestionJob:
    job = IngestionJob(
        id=new_uuid(),
        kind="ingest",
        stage="STORED",
        progress=30.0,
        payload={"source_type": "file", "filename": "low power sram.pdf", **payload},
        started_at=datetime.now(timezone.utc),
    )
    session.add(job)
    session.flush()
    return job


def make_shell(session, values=None, **kwargs) -> Paper:
    paper, _source = metadata_shell.create_shell(
        session,
        values if values is not None else {"title": "Low Power SRAM Leakage Reduction"},
        source_type=metadata_sources.SOURCE_TYPE_IMPORT_FILE,
        source_ref=kwargs.pop("source_ref", f"doi:{DOI}"),
        raw={"title": "Low Power SRAM Leakage Reduction"},
        **kwargs,
    )
    return paper


# --------------------------------------------------------------------------- #
# creating a shell
# --------------------------------------------------------------------------- #
def test_a_shell_has_metadata_but_no_file_and_no_chunks(db_session) -> None:
    shell = make_shell(
        db_session,
        {
            "title": "Low Power SRAM Leakage Reduction",
            "abstract": "An abstract",
            "year": 2015,
            "authors": ["Alice Smith"],
            "identifier:doi": DOI,
        },
    )

    assert shell.status == paper_service.STATUS_AWAITING_FILE
    assert paper_service.is_shell(shell) is True
    assert shell.doi == DOI.casefold(), "the mirrored column is filled for convenience"
    assert db_session.query(PaperFile).filter(PaperFile.paper_id == shell.id).count() == 0
    assert db_session.query(PaperChunk).filter(PaperChunk.paper_id == shell.id).count() == 0
    assert paper_service.paper_author_names(shell) == ["Alice Smith"]


def test_a_shell_fingerprint_comes_from_the_primary_identifier(db_session) -> None:
    shell = make_shell(db_session, {"title": "T", "identifier:doi": DOI})

    assert shell.fingerprint == f"doi:{DOI.casefold()}"


def test_a_shell_without_identifiers_falls_back_to_the_title_ladder(db_session) -> None:
    shell = make_shell(
        db_session,
        {"title": "Low Power SRAM", "authors": ["Alice Smith"], "year": 2015},
    )

    assert shell.fingerprint == "title:low power sram|alice smith|2015"


def test_a_shell_records_its_source_and_provenance(db_session) -> None:
    shell = make_shell(db_session, {"title": "T", "identifier:doi": DOI})

    sources = metadata_sources.sources_for_paper(db_session, shell.id)
    assert len(sources) == 1
    assert sources[0].source_type == metadata_sources.SOURCE_TYPE_IMPORT_FILE
    assert sources[0].match_status == metadata_sources.MATCH_STATUS_MATCHED

    claim = provenance_service.current_claim(db_session, shell.id, "title")
    assert claim is not None and claim.source_id == sources[0].id


def test_a_shell_is_registered_in_the_identifier_table(db_session) -> None:
    shell = make_shell(db_session, {"title": "T", "identifier:doi": DOI})

    rows = ids.identifiers_for_paper(db_session, shell.id)
    assert [(row.scheme, row.is_primary) for row in rows] == [("doi", True)]


def test_a_title_less_record_still_gets_a_title(db_session) -> None:
    values = metadata_shell.shell_values_from_match({}, fallback_title="paper.pdf")

    assert values["title"] == "paper.pdf"
    shell = make_shell(db_session, values)
    assert shell.title == "paper.pdf"


def test_shell_ids_only_lists_papers_without_a_file(db_session) -> None:
    shell = make_shell(db_session, {"title": "Waiting"})
    make_temp_paper(db_session, title="Indexed")

    waiting = metadata_shell.shell_ids(db_session)

    assert [paper.id for paper in waiting] == [shell.id]


def test_a_shell_can_be_found_by_its_doi(db_session) -> None:
    shell = make_shell(db_session, {"title": "T", "identifier:doi": DOI})

    from app.services import metadata_matcher

    result = metadata_matcher.match_record(
        db_session, metadata_matcher.MatchInput(identifiers={"doi": DOI})
    )

    assert result.matched and result.paper_id == shell.id


# --------------------------------------------------------------------------- #
# adopting a shell when the PDF arrives
# --------------------------------------------------------------------------- #
def test_adopt_moves_the_file_and_drops_the_throwaway_row(db_session, monkeypatch) -> None:
    shell = make_shell(db_session, {"title": SHELL_TITLE, "year": 2015})
    temp = make_temp_paper(db_session)
    record = paper_service.register_original_file(
        db_session,
        temp,
        object_key=f"papers/{temp.id}/original.pdf",
        bucket="paperbox",
        filename="low power sram.pdf",
        content_type="application/pdf",
        size_bytes=10,
        sha256="a" * 64,
    )
    moved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tasks.object_storage,
        "move_object",
        lambda source, target, **kwargs: moved.append((source, target)) or True,
    )

    adopted = metadata_shell.adopt_paper(db_session, temp, shell)

    assert adopted.id == shell.id
    assert db_session.get(Paper, temp.id) is None, "the throwaway row is gone"
    row = db_session.query(PaperFile).filter(PaperFile.paper_id == shell.id).one()
    assert row.object_key == f"papers/{shell.id}/original.pdf"
    assert moved == [(f"papers/{temp.id}/original.pdf", f"papers/{shell.id}/original.pdf")]
    assert shell.status == paper_service.STATUS_PENDING, "the shell is no longer waiting"
    assert row.id == record.id


def test_adoption_keeps_the_running_job_and_re_points_it(db_session, monkeypatch) -> None:
    """The job row must survive: it is the only handle ``GET /api/jobs/{id}`` has."""
    from app.db.models import IngestionJob
    from app.services import ingestion_service as ingest

    shell = make_shell(db_session, {"title": SHELL_TITLE, "year": 2015})
    temp = make_temp_paper(db_session)
    paper_service.register_original_file(
        db_session,
        temp,
        object_key=f"papers/{temp.id}/original.pdf",
        bucket="paperbox",
        filename="a.pdf",
        content_type="application/pdf",
        size_bytes=10,
    )
    job = ingest.create_job(
        db_session, source_type="file", filename="low power sram.pdf"
    )
    job.paper_id = temp.id
    db_session.flush()
    monkeypatch.setattr(tasks.object_storage, "move_object", lambda *a, **kw: True)

    metadata_shell.adopt_paper(db_session, temp, shell)

    assert db_session.get(IngestionJob, job.id) is not None
    assert db_session.get(IngestionJob, job.id).paper_id == shell.id


def test_adoption_keeps_the_file_readable_when_minio_refuses(db_session, monkeypatch) -> None:
    shell = make_shell(db_session, {"title": "T"})
    temp = make_temp_paper(db_session)
    original_key = f"papers/{temp.id}/original.pdf"
    paper_service.register_original_file(
        db_session,
        temp,
        object_key=original_key,
        bucket="paperbox",
        filename="a.pdf",
        content_type="application/pdf",
        size_bytes=10,
    )
    monkeypatch.setattr(tasks.object_storage, "move_object", lambda *a, **kw: False)

    metadata_shell.adopt_paper(db_session, temp, shell)

    row = db_session.query(PaperFile).filter(PaperFile.paper_id == shell.id).one()
    assert row.object_key == original_key, "the old key is kept, the file still works"


# --------------------------------------------------------------------------- #
# the pipeline resolving which paper a PDF belongs to
# --------------------------------------------------------------------------- #
def test_embedded_doi_makes_the_pdf_join_its_shell(db_session, monkeypatch) -> None:
    shell = make_shell(
        db_session,
        {
            "title": "Low Power SRAM Leakage Reduction",
            "year": 2015,
            "authors": ["Alice Smith"],
            "identifier:doi": DOI,
        },
    )
    temp = make_temp_paper(db_session)
    record = paper_service.register_original_file(
        db_session,
        temp,
        object_key=f"papers/{temp.id}/original.pdf",
        bucket="paperbox",
        filename="low power sram.pdf",
        content_type="application/pdf",
        size_bytes=10,
    )
    job = make_job(db_session)
    monkeypatch.setattr(tasks.object_storage, "move_object", lambda *a, **kw: True)

    resolution = tasks._resolve_target_paper(
        db_session,
        job,
        temp,
        record.object_key,
        record,
        make_pdf({"/Subject": f"doi:{DOI}"}),
        [PageText(page=1, text=FIRST_PAGE)],
    )

    assert resolution.paper.id == shell.id
    assert resolution.decision == paper_service.PRIMARY_ACTION_PRIMARY
    assert resolution.previous_status == paper_service.STATUS_AWAITING_FILE
    assert db_session.get(Paper, temp.id) is None
    assert db_session.query(Paper).filter(Paper.deleted_at.is_(None)).count() == 1
    assert job.payload["reused_paper_id"] == shell.id
    assert job.payload["match_method"] == "doi"


def test_title_author_year_makes_the_pdf_join_its_shell(db_session, monkeypatch) -> None:
    """No embedded metadata: the first-page heuristics have to do the matching."""
    shell = make_shell(
        db_session,
        {
            "title": SHELL_TITLE,
            "year": 2015,
            "authors": ["Alice Smith", "Bob Jones"],
        },
    )
    temp = make_temp_paper(db_session)
    record = paper_service.register_original_file(
        db_session,
        temp,
        object_key=f"papers/{temp.id}/original.pdf",
        bucket="paperbox",
        filename="low power sram.pdf",
        content_type="application/pdf",
        size_bytes=10,
    )
    job = make_job(db_session)
    monkeypatch.setattr(tasks.object_storage, "move_object", lambda *a, **kw: True)

    resolution = tasks._resolve_target_paper(
        db_session,
        job,
        temp,
        record.object_key,
        record,
        make_pdf(),
        [PageText(page=1, text=FIRST_PAGE)],
    )

    assert resolution.paper.id == shell.id
    assert job.payload["match_method"] == "title_year_author"


def test_an_unrelated_pdf_keeps_its_own_paper(db_session, monkeypatch) -> None:
    make_shell(db_session, {"title": "Something completely different", "year": 1999})
    temp = make_temp_paper(db_session)
    record = paper_service.register_original_file(
        db_session,
        temp,
        object_key=f"papers/{temp.id}/original.pdf",
        bucket="paperbox",
        filename="low power sram.pdf",
        content_type="application/pdf",
        size_bytes=10,
    )
    job = make_job(db_session)

    resolution = tasks._resolve_target_paper(
        db_session,
        job,
        temp,
        record.object_key,
        record,
        make_pdf(),
        [PageText(page=1, text=FIRST_PAGE)],
    )

    assert resolution.paper.id == temp.id
    assert db_session.get(Paper, temp.id) is not None


def test_a_second_version_joins_the_indexed_paper(db_session, monkeypatch) -> None:
    """A preprint arriving after the published PDF is stored, not indexed."""
    existing = make_temp_paper(db_session, title="Low Power SRAM Leakage Reduction")
    existing.status = paper_service.STATUS_INDEXED
    published = paper_service.register_original_file(
        db_session,
        existing,
        object_key=f"papers/{existing.id}/published.pdf",
        bucket="paperbox",
        filename="published.pdf",
        content_type="application/pdf",
        size_bytes=10,
        kind=paper_service.FILE_KIND_PUBLISHED_PDF,
    )
    paper_service.apply_primary_selection(db_session, existing, incoming=published)
    ids.upsert_identifier(
        db_session, paper_id=existing.id, scheme=ids.SCHEME_DOI, value=DOI
    )
    ids.refresh_primary(db_session, existing.id)

    temp = make_temp_paper(db_session, title="low power sram preprint.pdf")
    record = paper_service.register_original_file(
        db_session,
        temp,
        object_key=f"papers/{temp.id}/original.pdf",
        bucket="paperbox",
        filename="preprint.pdf",
        content_type="application/pdf",
        size_bytes=10,
        kind=paper_service.FILE_KIND_ARXIV_PDF,
    )
    job = make_job(db_session)
    monkeypatch.setattr(tasks.object_storage, "move_object", lambda *a, **kw: True)

    resolution = tasks._resolve_target_paper(
        db_session,
        job,
        temp,
        record.object_key,
        record,
        make_pdf({"/Subject": f"doi:{DOI}"}),
        [PageText(page=1, text=FIRST_PAGE)],
    )

    assert resolution.paper.id == existing.id
    assert resolution.decision == paper_service.PRIMARY_ACTION_NON_PRIMARY
    assert resolution.previous_status == paper_service.STATUS_INDEXED
    files = paper_service.list_paper_files(db_session, existing.id)
    assert {item.kind for item in files} == {"published_pdf", "arxiv_pdf"}
    assert paper_service.primary_file(existing).kind == "published_pdf"


def test_non_primary_completion_records_why_nothing_was_indexed(db_session) -> None:
    paper = make_temp_paper(db_session, title="Indexed paper")
    paper.status = paper_service.STATUS_INDEXED
    job = make_job(db_session)

    tasks._finish_non_primary(db_session, job, paper, paper_service.STATUS_INDEXED)

    assert job.stage == ingest.STAGE_COMPLETED
    assert job.progress == ingest.PROGRESS_COMPLETED
    assert job.payload["indexed"] is False
    assert job.payload["reason"] == "non_primary_version"
    assert job.paper_id == paper.id
    assert paper.status == paper_service.STATUS_INDEXED, "the indexed paper is untouched"
    assert job.finished_at is not None


def test_the_job_records_the_source_it_reused(db_session, monkeypatch) -> None:
    shell = make_shell(
        db_session,
        {"title": SHELL_TITLE, "year": 2015, "authors": ["Alice Smith"]},
    )
    source_id = metadata_sources.sources_for_paper(db_session, shell.id)[0].id
    temp = make_temp_paper(db_session)
    record = paper_service.register_original_file(
        db_session,
        temp,
        object_key=f"papers/{temp.id}/original.pdf",
        bucket="paperbox",
        filename="low power sram.pdf",
        content_type="application/pdf",
        size_bytes=10,
    )
    job = make_job(db_session)
    monkeypatch.setattr(tasks.object_storage, "move_object", lambda *a, **kw: True)

    tasks._resolve_target_paper(
        db_session,
        job,
        temp,
        record.object_key,
        record,
        make_pdf(),
        [PageText(page=1, text=FIRST_PAGE)],
    )

    row = db_session.query(PaperFile).filter(PaperFile.paper_id == shell.id).one()
    assert row.source_id == source_id, "the file inherits the source of the record"


def test_first_source_id_is_none_without_sources(db_session) -> None:
    paper = make_temp_paper(db_session)

    assert tasks._first_source_id(db_session, paper) is None
    assert db_session.query(PaperSource).count() == 0