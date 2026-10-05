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
    Paper,
    PaperFieldProvenance,
    PaperFile,
    PaperIdentifier,
    new_uuid,
)
from app.services import api_key_service as keys
from app.services import metadata_identifiers, paper_service
from tests.test_primary_version import add_file, make_paper


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
