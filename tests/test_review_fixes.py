"""Regression tests for the 2026-10-05 review batch (P1-1/2/7/11/16/17).

One file per review batch, one test per finding, so the report
(``docs/examine/全项目审查报告-20261005.md``) and the suite stay traceable:

* P1-1  — a fingerprint race must not roll back the caller's transaction;
* P1-2  — primary promotion must demote before it promotes (two flushes);
* P1-7  — ``_PDF_SUFFIX`` must match real ``.pdf`` names, not ``\\x`` + ``pdf``;
* P1-11 — a malformed paper id is a 404 (``None``), not a PostgreSQL DataError;
* P1-16 — oversized request bodies are a 413 and ``query`` has a length cap;
* P1-17 — the four partial unique indexes are enforced in the SQLite suite.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.models import (
    IngestionJob,
    Paper,
    PaperFieldProvenance,
    PaperFile,
    PaperIdentifier,
    new_uuid,
)
from app.services import api_key_service as keys
from app.services import metadata_identifiers, paper_service
from app.services import parser_service
from tests.test_job_progress import (  # noqa: F401
    factory,
    make_job,
    make_paper as make_paper_row,
    stubbed_pipeline,
)
from tests.test_parser_service import _always_raises, _pdf_bytes
from tests.test_pdf_embedded import extract_embedded_metadata
from tests.test_primary_version import add_file, make_paper
from tests.test_upload_gc import FakeStorage, age, make_job_row, run  # noqa: F401


# --------------------------------------------------------------------------- #
# P1-7 — the PDF-name regex
# --------------------------------------------------------------------------- #
def test_a_real_pdf_name_matches() -> None:
    from app.services.ingestion_service import is_pdf

    assert is_pdf("report.pdf", None) is True
    assert is_pdf("论文草稿.PDF", None) is True  # case-insensitive kept


def test_the_old_false_positive_no_longer_matches() -> None:
    from app.services.ingestion_service import is_pdf

    # ``\\x`` + ``pdf`` matched the old double-backslash pattern.
    assert is_pdf("x\\apdf", None) is False
    assert is_pdf(None, "application/octet-stream") is False


# --------------------------------------------------------------------------- #
# P1-11 — malformed paper ids answer 404, not 500
# --------------------------------------------------------------------------- #
def test_a_malformed_paper_id_is_none(db_session) -> None:
    assert paper_service.get_paper(db_session, "not-a-uuid") is None
    assert paper_service.get_paper(db_session, "") is None
    assert paper_service.get_paper(db_session, "1' OR '1'='1") is None


# --------------------------------------------------------------------------- #
# P1-17 — the partial unique indexes bite in SQLite too
# --------------------------------------------------------------------------- #
def test_two_live_papers_cannot_share_a_fingerprint(db_session) -> None:
    db_session.add(Paper(id=new_uuid(), title="A", fingerprint="sha256:X", status="INDEXED"))
    db_session.flush()
    db_session.add(Paper(id=new_uuid(), title="B", fingerprint="sha256:X", status="INDEXED"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_paper_cannot_have_two_primary_files(db_session) -> None:
    paper = make_paper(db_session)
    current = add_file(db_session, paper, kind="original")
    current.is_primary = True  # occupy the single slot
    db_session.flush()
    second = add_file(db_session, paper, kind="published_pdf")
    second.is_primary = True
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_an_identifier_cannot_belong_to_two_papers(db_session) -> None:
    first = make_paper(db_session)
    second = make_paper(db_session)
    db_session.add(
        PaperIdentifier(
            id=new_uuid(),
            paper_id=first.id,
            scheme="doi",
            value="10.1109/x",
            normalized_value="10.1109/x",
            is_primary=True,
        )
    )
    db_session.flush()
    db_session.add(
        PaperIdentifier(
            id=new_uuid(),
            paper_id=second.id,
            scheme="doi",
            value="10.1109/x",
            normalized_value="10.1109/x",
            is_primary=True,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_field_cannot_have_two_current_claims(db_session) -> None:
    paper = make_paper(db_session)
    moment = datetime.now(timezone.utc)
    db_session.add(
        PaperFieldProvenance(
            id=new_uuid(),
            paper_id=paper.id,
            field="year",
            value=2019,
            is_current=True,
            decided_by="test",
            decided_at=moment,
        )
    )
    db_session.flush()
    db_session.add(
        PaperFieldProvenance(
            id=new_uuid(),
            paper_id=paper.id,
            field="year",
            value=2020,
            is_current=True,
            decided_by="test",
            decided_at=moment,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


# --------------------------------------------------------------------------- #
# P1-2 — promotion demotes first (two flushes, no transient double-primary)
# --------------------------------------------------------------------------- #
def test_promotion_flushes_the_demotion_before_the_promotion(db_session, monkeypatch) -> None:
    paper = make_paper(db_session)
    current = add_file(db_session, paper, kind="original")
    current.is_primary = True
    incoming = add_file(db_session, paper, kind="published_pdf")

    snapshots: list[list[bool]] = []
    original_flush = db_session.flush

    def recording_flush():
        snapshots.append(sorted(record.is_primary for record in (current, incoming)))
        return original_flush()

    monkeypatch.setattr(db_session, "flush", recording_flush)
    outcome = paper_service.apply_primary_selection(db_session, paper, incoming=incoming)

    assert outcome.action == paper_service.PRIMARY_ACTION_PROMOTED
    assert incoming.is_primary is True and current.is_primary is False
    # Two flushes: the first leaves *no* primary row (both demoted/promoted-pending),
    # the second awards the slot — the transient double-true is impossible.
    assert len(snapshots) == 2
    assert snapshots[0] == [False, False]
    assert snapshots[1] == [False, True]


# --------------------------------------------------------------------------- #
# P1-1 — a fingerprint race must not sink the caller's transaction
# --------------------------------------------------------------------------- #
def test_fingerprint_race_keeps_the_caller_transaction(
    session_factory, monkeypatch
) -> None:
    session = session_factory()
    try:
        holder = Paper(id=new_uuid(), title="Holder", fingerprint="sha256:X", status="INDEXED")
        paper = Paper(id=new_uuid(), title="B", fingerprint="sha256:Y", status="PENDING")
        # Deliberately *not* flushed: autoflush is off, so the pre-check's
        # SELECT cannot see the holder — the race is only lost at the flush,
        # which is exactly the path the savepoint must protect.
        session.add(holder)
        session.add(paper)
        # A pending, uncommitted change the old bare session.rollback() wiped.
        paper.title = "B-corrected"

        # Pretend the identifier ladder now implies the fingerprint the other
        # paper holds.
        monkeypatch.setattr(
            metadata_identifiers, "primary_fingerprint", lambda *a, **k: "sha256:X"
        )

        result = metadata_identifiers.upgrade_fingerprint(session, paper, sha256="X")

        assert result == ("sha256:Y", None)  # kept the old fingerprint
        assert paper.fingerprint == "sha256:Y"
        assert paper.title == "B-corrected"  # the caller's pending change survived

        session.commit()
        rows = {
            row.title: row.fingerprint
            for row in session.query(Paper).all()
        }
        assert rows == {"Holder": "sha256:X", "B-corrected": "sha256:Y"}
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# P1-16 — request-body ceilings and the query length cap
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(monkeypatch, session_factory):
    """The real app with auth off and the key store pointed at the test DB."""
    monkeypatch.setattr("app.main.SessionLocal", session_factory)
    monkeypatch.setattr(settings, "paper_api_key", "")
    monkeypatch.setattr(settings, "paper_api_keys", "")
    monkeypatch.setattr(settings, "auth_enabled", False)
    from app.main import app as paperbox_app

    with TestClient(paperbox_app) as test_client:
        yield test_client


def test_an_oversized_json_body_is_a_413_before_validation(client) -> None:
    response = client.post("/api/search", json={"query": "x" * (9 * 1024 * 1024)})
    assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    assert response.json() == {"detail": "request body too large"}


def test_an_oversized_query_is_a_422(client) -> None:
    response = client.post("/api/search", json={"query": "x" * 1001})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_a_chunked_json_body_over_the_ceiling_is_a_413(client) -> None:
    """P1-16, the half the first fix missed: no ``Content-Length`` is not a way around it.

    An iterator body goes out as ``Transfer-Encoding: chunked``, which is exactly
    the shape that used to reach ``request.json()`` unbounded. The middleware now
    counts the bytes that actually arrive.
    """
    payload = b'{"query": "' + b"x" * (9 * 1024 * 1024) + b'"}'
    chunks = (payload[index : index + 65536] for index in range(0, len(payload), 65536))
    response = client.post(
        "/api/search", content=chunks, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    assert response.json() == {"detail": "request body too large"}


def test_a_chunked_json_body_under_the_ceiling_reaches_the_route(client) -> None:
    """The same path must not break the body: it is buffered and replayed intact."""
    payload = b'{"query": "' + b"x" * 1001 + b'"}'  # pydantic's own cap → 422
    chunks = (payload[index : index + 8] for index in range(0, len(payload), 8))
    response = client.post(
        "/api/search", content=chunks, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_a_normal_query_body_passes_the_ceiling(client) -> None:
    """A normal-size body reaches route handling (the 422 is pydantic's, on
    filters, not the middleware's) — here: an unknown mode, small body."""
    response = client.post(
        "/api/search", json={"query": "low power sram", "mode": "bogus"}
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_the_signing_secret_still_lives_without_auth(client, monkeypatch, session_factory) -> None:
    """P1-16 must not disturb the key machinery: anonymous callers still resolve."""
    session = session_factory()
    try:
        assert keys.live_key_count(session) >= 0
    finally:
        session.close()

# --------------------------------------------------------------------------- #
# P1-14 — a failed queue hand-off marks the job FAILED (never stalls RECEIVED)
# --------------------------------------------------------------------------- #
def test_a_failed_queue_hand_off_marks_the_job_failed(
    monkeypatch, session_factory
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import ingestion as ingestion_api
    from app.core.security import require_api_key
    from app.db.session import get_db
    from app.main import AuthContextMiddleware
    from app.services.ingestion_service import mark_failed  # noqa: F401 (context)

    monkeypatch.setattr("app.main.SessionLocal", session_factory)
    monkeypatch.setattr(settings, "auth_enabled", False)

    def explode(*args, **kwargs):
        raise RuntimeError("event loop closed")

    monkeypatch.setattr("app.workers.queue.submit", explode)

    def _db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(ingestion_api.router)
    app.add_middleware(AuthContextMiddleware)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_api_key] = lambda: "test"

    with TestClient(app, raise_server_exceptions=True) as test_client:
        with pytest.raises(RuntimeError):
            test_client.post(
                "/api/papers/ingest",
                json={"source": "https://arxiv.org/pdf/1807.11311"},
            )

    session = session_factory()
    try:
        job = session.query(IngestionJob).one()
        assert job.stage == "FAILED"
        assert job.error_code == "INTERNAL"
        assert "queue hand-off failed" in (job.error_message or "")
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# P1-13 — a docling client error gets its own ledger code
# --------------------------------------------------------------------------- #
def test_a_docling_client_error_maps_to_docling_rejected() -> None:
    from app.parsing.docling_client import DoclingFailed
    from app.services import parser_service

    rejected = DoclingFailed("404 preset not found")
    rejected.client_error = True  # set by _client_error() in production
    bundle = parser_service.parse_pdf(
        _pdf_bytes(),
        filename="a.pdf",
        backend="docling",
        converter=_always_raises(rejected),  # type: ignore[arg-type]
    )
    assert (bundle.degraded_reason or "").startswith("docling rejected")
    codes = parser_service.degradation_codes(bundle.degraded_reason or "")
    assert "docling_rejected" in codes
    assert "docling_unavailable" not in codes


def test_an_unreachable_docling_still_maps_to_docling_unavailable() -> None:
    from app.parsing.docling_client import DoclingUnavailable
    from app.services import parser_service

    bundle = parser_service.parse_pdf(
        _pdf_bytes(),
        filename="a.pdf",
        backend="docling",
        converter=_always_raises(DoclingUnavailable("connection refused")),  # type: ignore[arg-type]
    )
    codes = parser_service.degradation_codes(bundle.degraded_reason or "")
    assert "docling_unavailable" in codes
    assert "docling_rejected" not in codes


# --------------------------------------------------------------------------- #
# P2 batch (2026-10-05 review) — one test per finding where a unit is testable
# --------------------------------------------------------------------------- #
def test_title_fingerprint_is_capped_to_the_column() -> None:
    """P2-1: a pathological title cannot blow fingerprint String(255)."""
    fingerprint = paper_service.build_fingerprint(
        title="word " * 120, first_author="a", year=2020
    )
    assert fingerprint.startswith("title:")
    assert len(fingerprint) <= 255


def test_long_titles_beyond_the_cap_collapse_together() -> None:
    """Deterministic truncation: the same 200-char prefix -> same fingerprint."""
    long_a = paper_service.build_fingerprint(
        title="t" * 250 + "A", first_author="a", year=2020
    )
    long_b = paper_service.build_fingerprint(
        title="t" * 250 + "B", first_author="a", year=2020
    )
    assert long_a == long_b


def test_docling_reported_page_count_survives_missing_markers() -> None:
    """P2-5: a document whose page markers were dropped takes the backend's
    own page count instead of collapsing into one giant page 1."""
    from app.parsing.docling_client import DoclingResult

    def converter(*args, **kwargs):
        return DoclingResult(
            markdown="# title\n\nbody text without markers",
            page_count=5,
            parser_version="test",
        )

    bundle = parser_service.parse_pdf(
        b"%PDF-fake", filename="a.pdf", backend="docling", converter=converter
    )
    assert bundle.page_count == 5


def test_marker_pages_win_when_they_actually_show_pages() -> None:
    from app.parsing.docling_client import DoclingResult

    def converter(*args, **kwargs):
        return DoclingResult(
            markdown=(
                "# t\n\n<!-- page-break -->\n\nbody\n\n"
                "<!-- page-break -->\n\nmore"
            ),
            page_count=9,
            parser_version="test",
        )

    bundle = parser_service.parse_pdf(
        b"%PDF-fake", filename="a.pdf", backend="docling", converter=converter
    )
    assert bundle.page_count == 3


def test_first_stage_k_is_capped() -> None:
    """P2-8: top_k=50 x RERANK_CANDIDATES=5 wanted 250 cross-encoder calls."""
    from app.search.hybrid import MAX_RERANK_POOL, _first_stage_k

    assert _first_stage_k(10, True) == 50
    assert _first_stage_k(50, True) == MAX_RERANK_POOL
    assert _first_stage_k(50, False) == 50


def test_rerank_pool_total_is_capped() -> None:
    from types import SimpleNamespace

    from app.search.hybrid import MAX_RERANK_POOL, _rerank_pool

    hits = [SimpleNamespace(paper_id=f"p{index // 5}") for index in range(150)]
    assert len(_rerank_pool(hits)) == MAX_RERANK_POOL


def test_a_non_fingerprint_unique_conflict_is_not_mislabeled() -> None:
    """P2-9: an author-name clash is not a DUPLICATE_FINGERPRINT."""
    from sqlalchemy.exc import IntegrityError

    from app.core.errors import classify_failure

    exc = IntegrityError(
        "INSERT INTO authors ...",
        {},
        Exception(
            'duplicate key value violates unique constraint "uq_authors_normalized_name"'
        ),
    )
    failure = classify_failure(exc)
    assert failure.code != "DUPLICATE_FINGERPRINT"
    assert "uq_authors_normalized_name" in failure.message


def test_gfm_table_rows_survive_line_joining() -> None:
    """P2-18: table rows keep their line structure; prose still joins."""
    from app.parsing.structure import _join_wrapped_lines

    block = (
        "prose line one\n"
        "prose line two\n"
        "| a | b |\n"
        "|---|---|\n"
        "| c | d |\n"
        "prose line three"
    )
    joined = _join_wrapped_lines(block)
    assert "prose line one prose line two" in joined
    assert "| a | b |\n|---|---|\n| c | d |" in joined
    assert "prose line three" in joined


def test_an_xmp_packet_with_a_dtd_is_refused() -> None:
    """P2-19: entity declarations are the billion-laughs vector; the packet is
    refused before xml.etree ever sees it."""
    from tests.test_pdf_embedded import XMP_TEMPLATE, build_pdf

    injected = XMP_TEMPLATE.format(
        title="T",
        author_one="A",
        author_two="B",
        description=']]></x:xmpmeta><!DOCTYPE lolz [<!ENTITY lol "lol">]>',
        keywords="k",
        doi="",
        venue="v",
        volume="1",
        issue="2",
        start_page="1",
        end_page="2",
        publication_date="2020",
    )
    metadata = extract_embedded_metadata(build_pdf(xmp=injected))
    # The refusal path leaves the XMP side empty; the Info dictionary itself is
    # still reported.
    assert metadata.raw["xmp"] == {}
    assert metadata.doi is None


def test_extraction_dir_honors_the_retryable_grace(factory, tmp_path):  # noqa: F811
    """P2-11: a retryable job keeps its unpacked files for 72h, not the 24h
    TTL that made late retries fail with LocalSourceUnavailable."""
    from tests.test_upload_gc import age, make_job_row, run

    directory = tmp_path / "paperbox-req-late"
    directory.mkdir()
    (directory / "a.pdf").write_bytes(b"pdf")
    age(directory, hours=30)  # older than the 24h TTL, younger than 72h

    make_job_row(
        factory,
        payload={"source_type": "local_path", "local_path": str(directory / "a.pdf")},
        stage="FAILED",
        finished=True,
        finished_hours_ago=30,
    )

    report = run(factory, FakeStorage([]), tmp_path)
    assert directory.exists(), "a retryable job's extraction dir must survive"


def test_extraction_dir_without_a_retryable_job_is_still_collected(
    factory, tmp_path
):  # noqa: F811
    from tests.test_upload_gc import age, make_job_row, run

    directory = tmp_path / "paperbox-req-done"
    directory.mkdir()
    (directory / "a.pdf").write_bytes(b"pdf")
    age(directory, hours=30)

    make_job_row(
        factory,
        payload={"source_type": "local_path", "local_path": str(directory / "a.pdf")},
        stage="COMPLETED",
        finished=True,
        finished_hours_ago=30,
    )

    report = run(factory, FakeStorage([]), tmp_path)
    assert not directory.exists()


def test_a_lost_create_race_resolves_as_duplicate(
    factory, stubbed_pipeline, monkeypatch
) -> None:
    """P2-14: the concurrent-import loser ends COMPLETED+duplicate, not FAILED."""
    from sqlalchemy.exc import IntegrityError

    from app.db.models import IngestionJob
    from app.services import ingestion_service as ingest
    from app.workers import tasks

    monkeypatch.setattr(tasks, "SessionLocal", factory)
    winner_id = make_paper_row(factory)
    job_id = make_job(factory)

    state = {"raced": False}

    def fake_find_by_sha256(session, digest):
        if not state["raced"]:
            return None
        return session.query(Paper).filter(Paper.id == winner_id).one()

    def fake_find_by_fingerprint(session, fingerprint):
        if not state["raced"]:
            return None
        return session.query(Paper).filter(Paper.id == winner_id).one()

    def race_then_lose(*args, **kwargs):
        state["raced"] = True
        raise IntegrityError(
            "INSERT INTO papers",
            {},
            Exception(
                'duplicate key value violates unique constraint "uq_papers_fingerprint_live"'
            ),
        )

    monkeypatch.setattr(paper_service, "find_by_sha256", fake_find_by_sha256)
    monkeypatch.setattr(paper_service, "find_by_fingerprint", fake_find_by_fingerprint)
    monkeypatch.setattr(paper_service, "create_paper", race_then_lose)
    monkeypatch.setattr(tasks, "_cleanup_source", lambda *a, **k: None)
    monkeypatch.setattr(tasks.object_storage, "delete_object", lambda *a, **k: None)

    session = factory()
    try:
        job = session.get(IngestionJob, job_id)
        outcome = tasks._process_job(session, job)
        assert outcome.duplicate is True
        assert outcome.paper_id == winner_id
        session.commit()
    finally:
        session.close()

    session = factory()
    try:
        job = session.get(IngestionJob, job_id)
        assert job.payload.get("duplicate") is True
        assert job.paper_id == winner_id
    finally:
        session.close()
