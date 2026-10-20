"""Re-extraction: apply the discovery layers to an existing paper (no reindex).

The point of these tests is the two guarantees the CLI/endpoint rest on:
dry-run writes nothing, and apply only *fills* what is blank (never overwrites),
idempotently.
"""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from app.db.models import Paper, PaperFile, PaperIdentifier, new_uuid
from app.parsing.pdf import extract_pages
from app.services import metadata_reextract, metadata_sources, provenance_service
from tests.test_job_progress import factory  # noqa: F401 - fixture


def pdf_with_article_id(article_id: str = "11617481") -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": "An IEEE paper", "/IEEE Article ID": article_id})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_paper(session, *, title="A Paper", with_file=True, **cols) -> Paper:
    paper = Paper(
        id=new_uuid(),
        title=title,
        fingerprint=f"sha256:{new_uuid()}",
        status="PENDING",
        **cols,
    )
    session.add(paper)
    if with_file:
        session.add(
            PaperFile(
                id=new_uuid(),
                paper_id=paper.id,
                kind="original",
                object_key=f"papers/{paper.id}/original.pdf",
                bucket="paperbox",
                filename="11617481.pdf",
                content_type="application/pdf",
                size_bytes=10,
            )
        )
    session.commit()
    return paper


def article_claims(session, paper):
    return provenance_service.current_claim(
        session, paper.id, "identifier:ieee_article_number"
    )


def article_rows(session, paper):
    return (
        session.query(PaperIdentifier)
        .filter_by(paper_id=paper.id, scheme="ieee_article_number")
        .all()
    )


# --------------------------------------------------------------------------- #
# single paper
# --------------------------------------------------------------------------- #
def test_dry_run_reports_but_writes_nothing(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = make_paper(session)
    data = pdf_with_article_id()

    result = metadata_reextract.reextract_paper_metadata(
        session, paper, pages=extract_pages(data), pdf_bytes=data, filename="11617481.pdf",
        dry_run=True,
    )

    assert "identifier:ieee_article_number" in [c.field for c in result.changes]
    # nothing was written
    assert article_rows(session, paper) == []
    assert article_claims(session, paper) is None


def test_apply_writes_the_article_number(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = make_paper(session)
    data = pdf_with_article_id()

    result = metadata_reextract.reextract_paper_metadata(
        session, paper, pages=extract_pages(data), pdf_bytes=data, filename="11617481.pdf",
        dry_run=False,
    )
    session.commit()

    assert result.changed
    rows = article_rows(session, paper)
    assert rows and rows[0].value == "11617481"
    assert article_claims(session, paper).value == "11617481"


def test_second_apply_is_a_no_op(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = make_paper(session)
    data = pdf_with_article_id()

    metadata_reextract.reextract_paper_metadata(
        session, paper, pages=extract_pages(data), pdf_bytes=data, filename="x.pdf", dry_run=False
    )
    session.commit()

    again = metadata_reextract.reextract_paper_metadata(
        session, paper, pages=extract_pages(data), pdf_bytes=data, filename="x.pdf", dry_run=False
    )
    session.commit()

    assert again.changes == []


def test_it_keeps_a_doi_from_an_equally_structured_source(factory) -> None:  # noqa: ANN001
    """The paper's DOI came from a structured import; the PDF says another DOI.

    Rule R2 never lets one structured source silently replace another, so the DOI
    is kept and the disagreement is recorded -- while the *new* article number is
    still filled. (A weak ``pdf_heuristic`` DOI would be overridden by the embedded
    one, which is the intended direction of R2.)
    """
    from app.services import metadata_merge

    session = factory()
    paper = make_paper(session)
    imported = metadata_sources.upsert_source(
        session,
        source_type=metadata_sources.SOURCE_TYPE_IMPORT_FILE,
        source_ref="doi:10.1109/EXISTING.2020.1",
        raw={"doi": "10.1109/EXISTING.2020.1"},
        paper_id=paper.id,
        match_status=metadata_sources.MATCH_STATUS_MATCHED,
    )
    metadata_merge.merge_values(
        session,
        paper,
        {"identifier:doi": "10.1109/EXISTING.2020.1"},
        source_type=metadata_sources.SOURCE_TYPE_IMPORT_FILE,
        source_id=imported.id,
        confidence=1.0,
    )
    session.commit()

    # PDF whose /Subject carries a *different* DOI, plus an article number.
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata(
        {
            "/Title": "T",
            "/Subject": "A venue;2026;1;1;10.1109/DIFFERENT.2026.2",
            "/IEEE Article ID": "11617481",
        }
    )
    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()

    result = metadata_reextract.reextract_paper_metadata(
        session, paper, pages=extract_pages(data), pdf_bytes=data, filename="x.pdf", dry_run=False
    )
    session.commit()

    # The structured DOI is untouched (equal-trust sources do not overwrite); the
    # mirror column stores the normalized (lower-case) form, which is the same DOI.
    assert (paper.doi or "").lower() == "10.1109/existing.2020.1"
    # The rejected DOI is a recorded conflict, not an applied change.
    assert "identifier:doi" not in [c.field for c in result.changes]
    # The *new* identifier (article number) is still filled.
    assert article_rows(session, paper)


def test_missing_file_is_skipped_by_the_batch(factory) -> None:  # noqa: ANN001
    session = factory()
    make_paper(session, with_file=False)

    summary = metadata_reextract.reextract_library(
        session, apply=False, download=lambda key: b""
    )

    assert summary.papers == 1
    assert summary.skipped_no_file == 1
    assert summary.changed_papers == 0


# --------------------------------------------------------------------------- #
# whole-library driver
# --------------------------------------------------------------------------- #
def test_batch_dry_run_reports_and_writes_nothing(factory) -> None:  # noqa: ANN001
    session = factory()
    p1 = make_paper(session)
    p2 = make_paper(session)
    data = pdf_with_article_id("11617481")

    summary = metadata_reextract.reextract_library(
        session,
        apply=False,
        refresh_index=False,
        download=lambda key: data,
        detail=True,
    )

    assert summary.papers == 2
    assert summary.changed_papers == 2
    assert summary.added_identifiers.get("identifier:ieee_article_number") == 2
    # nothing written
    assert article_rows(session, p1) == []
    assert article_rows(session, p2) == []


def test_batch_apply_only_passes_through_without_reindex(factory, monkeypatch) -> None:  # noqa: ANN001
    """apply writes the metadata; with refresh_index=True it also rewrites the
    index snapshot of the affected paper (opensearch patched, no live cluster)."""
    session = factory()
    p1 = make_paper(session)
    data = pdf_with_article_id("11617481")

    refreshed = []

    def fake_refresh(session_, paper):
        refreshed.append(paper.id)
        return 7

    monkeypatch.setattr(metadata_reextract, "_refresh_index_for", fake_refresh)

    summary = metadata_reextract.reextract_library(
        session,
        apply=True,
        refresh_index=True,
        download=lambda key: data,
    )

    assert summary.changed_papers == 1
    assert article_rows(session, p1)
    assert refreshed == [p1.id]
    assert summary.index_documents == 7


# --------------------------------------------------------------------------- #
# the admin endpoint
# --------------------------------------------------------------------------- #
def test_endpoint_dry_run_reports_without_writing(session_factory, monkeypatch) -> None:  # noqa: ANN001
    from fastapi.testclient import TestClient

    from app.core.security import require_admin
    from app.db.session import get_db
    from app.main import app
    from app.services import object_storage

    session = session_factory()
    try:
        make_paper(session)  # one paper with a stored original
    finally:
        session.close()

    monkeypatch.setattr(object_storage, "download_bytes", lambda key: pdf_with_article_id())

    def _db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: "admin-key"
    try:
        client = TestClient(app)
        response = client.post("/api/metadata/reextract", json={"dry_run": True})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["papers"] == 1
    assert body["changed_papers"] == 1
    assert body["added_identifiers"].get("identifier:ieee_article_number") == 1
    # dry run wrote nothing
    check = session_factory()
    try:
        assert check.query(PaperIdentifier).filter_by(scheme="ieee_article_number").all() == []
    finally:
        check.close()


def test_single_paper_endpoint_dry_run(session_factory, monkeypatch) -> None:  # noqa: ANN001
    from fastapi.testclient import TestClient

    from app.core.security import require_write
    from app.db.session import get_db
    from app.main import app
    from app.services import object_storage

    session = session_factory()
    try:
        paper = make_paper(session)
        paper_id = paper.id
    finally:
        session.close()

    monkeypatch.setattr(object_storage, "download_bytes", lambda key: pdf_with_article_id())

    def _db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_write] = lambda: "writer"
    try:
        client = TestClient(app)
        response = client.post(f"/api/papers/{paper_id}/metadata/reextract", json={"dry_run": True})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["papers"] == 1
    assert body["added_identifiers"].get("identifier:ieee_article_number") == 1


def test_reextract_one_no_file_is_skipped(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = make_paper(session, with_file=False)

    summary = metadata_reextract.reextract_one(session, paper, download=lambda key: b"")

    assert summary.papers == 1
    assert summary.skipped_no_file == 1
    assert summary.changed_papers == 0
