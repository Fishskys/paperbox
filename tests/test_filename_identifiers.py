"""Discovery layer 3: the arXiv id a *file name* carries.

The corpus convention is ``<arxiv_id>__<topic>.pdf``, but a PDF without an arXiv
stamp and with a Word/LaTeX ``/Info`` title otherwise ends up with no identifier at
all (7 of the 30 corpus papers on 2026-09-30 were repaired by hand because of
this). The name is a weak hint: it may only *fill* an unknown identifier, never
override one, and it never speaks about title/authors/year.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperFieldProvenance, PaperFile, PaperIdentifier, PaperSource, new_uuid
from app.services import (
    metadata_identifiers,
    metadata_merge,
    metadata_service,
    metadata_sources,
    provenance_service,
)
from app.workers import tasks
from tests.test_job_progress import factory  # noqa: F401 - fixture


# --------------------------------------------------------------------------- #
# extraction (pure)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("1706.03762__计算机.pdf", "1706.03762"),
        ("1512.03385__计算机.pdf", "1512.03385"),
        ("2105.11453.pdf", "2105.11453"),
        ("2105.11453v2.pdf", "2105.11453"),  # versions are dropped
        ("2301.07543__人文社科.json", "2301.07543"),  # any extension
        ("/mnt/c/some/dir/2604.07387__数字.pdf", "2604.07387"),  # full path
        ("ARXIV_2402.07138_v3.pdf", "2402.07138"),
        ("paper_2608.13472_v1__半导体.pdf", "2608.13472"),
        ("1706.03762V2.PDF", "1706.03762"),  # case-insensitive
    ],
)
def test_arxiv_id_is_taken_from_the_file_name(filename: str, expected: str) -> None:
    assert metadata_service.arxiv_id_from_filename(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "",
        None,
        "paper.pdf",
        "low_power_sram.pdf",
        "notes-2024.12345.pdf",  # month 24 does not exist -> not an identifier
        "report-0000.12345.pdf",  # month 00 does not exist
        "table.1.2345.pdf",  # not enough digits before the dot
        "readings_1234567.pdf",
        "doi-10.1109.2024.1234.pdf",  # a DOI, not an arXiv id
    ],
)
def test_names_without_an_arxiv_id_yield_nothing(filename: str | None) -> None:
    assert metadata_service.arxiv_id_from_filename(filename) is None


def test_file_name_claims_only_ever_carry_the_identifier() -> None:
    assert metadata_service.filename_claim_values("1706.03762__计算机.pdf") == {
        "identifier:arxiv": "1706.03762"
    }
    assert metadata_service.filename_claim_values("low_power_sram.pdf") == {}


# --------------------------------------------------------------------------- #
# merge: a file name is weak, so a structured source may correct it
# --------------------------------------------------------------------------- #
def test_the_file_name_source_is_classified_as_weak() -> None:
    assert metadata_merge.is_heuristic(metadata_sources.SOURCE_TYPE_FILENAME)
    assert not metadata_merge.is_structured(metadata_sources.SOURCE_TYPE_FILENAME)


def test_a_file_name_claim_fills_a_blank_identifier(db_session: Session) -> None:
    paper = _paper(db_session)
    source = _source(db_session, paper, metadata_sources.SOURCE_TYPE_FILENAME)
    claims = metadata_service.filename_claim_values("2105.11453__半导体.pdf")
    report = metadata_merge.merge_values(
        db_session,
        paper,
        claims,
        source_type=metadata_sources.SOURCE_TYPE_FILENAME,
        source_id=source.id,
        confidence=0.4,
    )
    assert [d.field for d in report.decisions] == ["identifier:arxiv"]
    assert paper.arxiv_id == "2105.11453"
    rows = db_session.query(PaperIdentifier).filter_by(paper_id=paper.id, scheme="arxiv").all()
    assert [row.normalized_value for row in rows] == ["2105.11453"]


def test_a_structured_source_overrides_a_file_name_claim(db_session: Session) -> None:
    paper = _paper(db_session)
    weak = _source(db_session, paper, metadata_sources.SOURCE_TYPE_FILENAME)
    structured = _source(db_session, paper, metadata_sources.SOURCE_TYPE_PDF_EMBEDDED)
    metadata_merge.merge_values(
        db_session,
        paper,
        metadata_service.filename_claim_values("2105.11453__guess.pdf"),
        source_type=metadata_sources.SOURCE_TYPE_FILENAME,
        source_id=weak.id,
        confidence=0.4,
    )
    report = metadata_merge.merge_values(
        db_session,
        paper,
        {"identifier:arxiv": "2105.11499"},
        source_type=metadata_sources.SOURCE_TYPE_PDF_EMBEDDED,
        source_id=structured.id,
        confidence=1.0,
    )
    assert [d.action for d in report.decisions] == [metadata_merge.ACTION_OVERRIDDEN]
    assert paper.arxiv_id == "2105.11499"


def test_a_file_name_claim_does_not_replace_an_existing_heuristic_value(db_session: Session) -> None:
    """Two weak sources disagreeing is a conflict to log, not a silent overwrite."""
    paper = _paper(db_session)
    heuristic = _source(db_session, paper, metadata_sources.SOURCE_TYPE_PDF_HEURISTIC)
    weak = _source(db_session, paper, metadata_sources.SOURCE_TYPE_FILENAME)
    metadata_merge.merge_values(
        db_session,
        paper,
        {"identifier:arxiv": "2105.11453"},
        source_type=metadata_sources.SOURCE_TYPE_PDF_HEURISTIC,
        source_id=heuristic.id,
        confidence=0.5,
    )
    report = metadata_merge.merge_values(
        db_session,
        paper,
        metadata_service.filename_claim_values("2301.07543__other.pdf"),
        source_type=metadata_sources.SOURCE_TYPE_FILENAME,
        source_id=weak.id,
        confidence=0.4,
    )
    assert [d.action for d in report.decisions] == [metadata_merge.ACTION_CONFLICT]
    assert paper.arxiv_id == "2105.11453"


# --------------------------------------------------------------------------- #
# pipeline: layer 3 runs, and only while nothing else claimed an identifier
# --------------------------------------------------------------------------- #
def test_backfill_records_a_file_name_source_when_the_pdf_knows_nothing(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = _paper(session)

    tasks._backfill_metadata(session, paper, [], None, filename="1706.03762__计算机.pdf")

    assert paper.arxiv_id == "1706.03762"
    sources = session.query(PaperSource).filter_by(paper_id=paper.id).all()
    assert [source.source_type for source in sources] == [metadata_sources.SOURCE_TYPE_FILENAME]
    assert sources[0].source_ref == metadata_sources.paper_filename_ref(paper.id)
    assert sources[0].raw["filename"] == "1706.03762__计算机.pdf"
    claim = provenance_service.current_claim(session, paper.id, "identifier:arxiv")
    assert claim is not None and claim.value == "1706.03762"


def test_backfill_skips_layer_three_when_an_arxiv_id_is_already_known(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = _paper(session, arxiv_id="1512.03385")

    tasks._backfill_metadata(session, paper, [], None, filename="1706.03762__计算机.pdf")

    assert paper.arxiv_id == "1512.03385"
    assert session.query(PaperSource).filter_by(paper_id=paper.id).count() == 0
    assert session.query(PaperIdentifier).filter_by(paper_id=paper.id).count() == 0


def test_backfill_skips_a_guess_that_belongs_to_another_paper(factory) -> None:  # noqa: ANN001
    """A name contradicting the identity map is a duplicate signal, not a hint."""
    session = factory()
    owner = _paper(session)
    _identifier(session, owner, "1706.03762")
    fresh = _paper(session)

    tasks._backfill_metadata(session, fresh, [], None, filename="1706.03762__计算机.pdf")

    assert fresh.arxiv_id is None
    assert session.query(PaperSource).filter_by(paper_id=fresh.id).count() == 0
    assert provenance_service.current_claim(session, fresh.id, "identifier:arxiv") is None
    assert [row.normalized_value for row in _arxiv_rows(session, owner)] == ["1706.03762"]


def test_backfill_offers_a_guess_nobody_holds_yet(factory) -> None:  # noqa: ANN001
    session = factory()
    owner = _paper(session)
    _identifier(session, owner, "1706.03762")
    fresh = _paper(session)

    tasks._backfill_metadata(session, fresh, [], None, filename="2105.11453__半导体.pdf")

    assert fresh.arxiv_id == "2105.11453"
    assert session.query(PaperSource).filter_by(paper_id=fresh.id).count() == 1


def test_backfill_without_a_file_name_touches_nothing(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = _paper(session)

    tasks._backfill_metadata(session, paper, [], None, filename=None)

    assert paper.arxiv_id is None
    assert session.query(PaperSource).filter_by(paper_id=paper.id).count() == 0


def test_source_filename_prefers_the_primary_file_row(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = _paper(session)
    session.add(
        PaperFile(
            id=new_uuid(),
            paper_id=paper.id,
            kind="original",
            object_key=f"papers/{paper.id}/original.pdf",
            bucket="paperbox",
            filename="1706.03762__计算机.pdf",
            content_type="application/pdf",
            size_bytes=10,
            is_primary=True,
        )
    )
    session.flush()
    job = _job(session, paper.id, payload={"filename": "uploaded-name.pdf"})

    assert tasks._source_filename(paper, job) == "1706.03762__计算机.pdf"


def test_source_filename_falls_back_to_the_job_payload(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = _paper(session)
    job = _job(session, paper.id, payload={"filename": "2301.07543__人文.pdf"})

    assert tasks._source_filename(paper, job) == "2301.07543__人文.pdf"


def test_source_filename_is_none_when_nothing_carries_a_name(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = _paper(session)
    job = _job(session, paper.id, payload={})

    assert tasks._source_filename(paper, job) is None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _source(session: Session, paper: Paper, source_type: str) -> PaperSource:
    """A source row, as ``upsert_source`` would create it before merging.

    Rule 2 classifies the *existing* claim through its source row, so a merge
    without one can never be overridden -- the production path always writes the
    row first, and these tests follow it.
    """
    source = PaperSource(
        id=new_uuid(),
        paper_id=paper.id,
        source_type=source_type,
        source_ref=f"{source_type}:{paper.id}",
        raw={},
        match_status="matched",
    )
    session.add(source)
    session.flush()
    return source


def _identifier(session: Session, paper: Paper, arxiv_id: str) -> PaperIdentifier:
    row = PaperIdentifier(
        id=new_uuid(),
        paper_id=paper.id,
        scheme="arxiv",
        value=arxiv_id,
        normalized_value=arxiv_id,
        is_primary=True,
    )
    session.add(row)
    session.flush()
    metadata_identifiers.mirror_legacy_columns(session, paper)
    return row


def _arxiv_rows(session: Session, paper: Paper) -> list[PaperIdentifier]:
    return (
        session.query(PaperIdentifier)
        .filter_by(paper_id=paper.id, scheme="arxiv")
        .all()
    )


def _paper(session: Session, **overrides) -> Paper:
    values = {
        "id": new_uuid(),
        "title": "A Paper",
        "fingerprint": f"sha256:{new_uuid()}",
        "status": "PENDING",
    }
    values.update(overrides)
    paper = Paper(**values)
    session.add(paper)
    session.flush()
    return paper


def _job(session: Session, paper_id: str, *, payload: dict):
    from app.db.models import IngestionJob

    job = IngestionJob(
        id=new_uuid(),
        paper_id=paper_id,
        kind="ingest",
        stage="RECEIVED",
        payload={"source_type": "local_path", "source": "/tmp/x.pdf", **payload},
    )
    session.add(job)
    session.flush()
    return job


def test_provenance_rows_are_named_after_the_new_source(factory) -> None:  # noqa: ANN001
    """The ledger has to say *file name*, not ``pdf_heuristic``."""
    session = factory()
    paper = _paper(session)
    tasks._backfill_metadata(session, paper, [], None, filename="2402.07138__软件.pdf")

    rows = (
        session.query(PaperFieldProvenance)
        .filter_by(paper_id=paper.id, field="identifier:arxiv")
        .all()
    )
    assert rows, "layer 3 must leave a provenance row"
    source = session.get(PaperSource, rows[0].source_id)
    assert source.source_type == metadata_sources.SOURCE_TYPE_FILENAME
