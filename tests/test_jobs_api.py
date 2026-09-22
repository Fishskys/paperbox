"""``GET /api/jobs`` pagination and stage filtering (2026-09-23).

The WebUI's job tab walks the history **server-side**, so the list endpoint needs
an ``offset`` (it was ``limit``-only, capped at 200, which made page 2
unreachable) and a ``stage`` filter (finding the FAILED jobs worth retrying).
Both are additive: the defaults reproduce the previous behaviour exactly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.security import require_api_key
from app.db.models import IngestionJob
from app.db.session import get_db
from app.main import app
from app.services import ingestion_service as ingest

BASE = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def client(session_factory):
    """A TestClient wired to the in-memory database and a stub API key."""

    def _db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_api_key] = lambda: "test-key"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def make_job(session_factory, *, stage="COMPLETED", minutes=0, paper_id=None) -> str:
    """Insert one job whose ``created_at`` orders it against its siblings."""
    session = session_factory()
    try:
        job = IngestionJob(
            paper_id=paper_id,
            stage=stage,
            progress=100.0 if stage == "COMPLETED" else 0.0,
            created_at=BASE + timedelta(minutes=minutes),
            updated_at=BASE + timedelta(minutes=minutes),
        )
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# offset: the three jobs are oldest -> newest, the endpoint answers newest first
# --------------------------------------------------------------------------- #
def test_offset_walks_the_history_newest_first(client, session_factory):
    oldest = make_job(session_factory, minutes=0)
    middle = make_job(session_factory, minutes=1)
    newest = make_job(session_factory, minutes=2)

    page_one = client.get("/api/jobs", params={"limit": 2, "offset": 0}).json()
    page_two = client.get("/api/jobs", params={"limit": 2, "offset": 2}).json()

    assert [job["job_id"] for job in page_one["jobs"]] == [newest, middle]
    assert [job["job_id"] for job in page_two["jobs"]] == [oldest]
    # ``total`` is the size of the filtered set, never the size of the page.
    assert (page_one["total"], page_two["total"]) == (3, 3)
    assert (page_one["offset"], page_two["offset"]) == (0, 2)


def test_an_offset_past_the_end_is_empty_not_an_error(client, session_factory):
    make_job(session_factory)

    payload = client.get("/api/jobs", params={"limit": 20, "offset": 500}).json()

    assert payload["jobs"] == []
    assert payload["total"] == 1


def test_a_negative_offset_is_rejected(client):
    assert client.get("/api/jobs", params={"offset": -1}).status_code == 422


# --------------------------------------------------------------------------- #
# stage: the filter narrows the rows *and* the total the pager counts with
# --------------------------------------------------------------------------- #
def test_stage_filter_narrows_the_rows_and_the_total(client, session_factory):
    make_job(session_factory, stage="FAILED", minutes=0)
    make_job(session_factory, stage="FAILED", minutes=1)
    make_job(session_factory, stage="COMPLETED", minutes=2)

    payload = client.get("/api/jobs", params={"stage": "FAILED"}).json()

    assert payload["total"] == 2
    assert [job["stage"] for job in payload["jobs"]] == ["FAILED", "FAILED"]
    assert payload["stage"] == "FAILED"


def test_stage_filter_pages_within_its_own_set(client, session_factory):
    make_job(session_factory, stage="FAILED", minutes=0)
    make_job(session_factory, stage="FAILED", minutes=1)
    make_job(session_factory, stage="COMPLETED", minutes=2)

    payload = client.get(
        "/api/jobs", params={"stage": "FAILED", "limit": 1, "offset": 1}
    ).json()

    assert payload["total"] == 2
    assert len(payload["jobs"]) == 1
    assert payload["jobs"][0]["stage"] == "FAILED"


def test_an_unknown_stage_is_rejected_rather_than_silently_empty(client):
    """A typo must not look like "no such jobs" (the UI shows the detail)."""
    response = client.get("/api/jobs", params={"stage": "FAILEDD"})

    assert response.status_code == 422
    assert "stage" in str(response.json()["detail"]).lower()


def test_every_pipeline_stage_is_accepted(client, session_factory):
    """The whitelist covers what ``tasks.py`` actually writes."""
    for stage in ingest.STAGES:
        assert client.get("/api/jobs", params={"stage": stage}).status_code == 200


def test_stage_and_paper_id_combine(client, session_factory):
    paper_id = _make_paper(session_factory)
    make_job(session_factory, stage="FAILED", minutes=0, paper_id=paper_id)
    make_job(session_factory, stage="COMPLETED", minutes=1, paper_id=paper_id)
    make_job(session_factory, stage="FAILED", minutes=2)

    payload = client.get(
        "/api/jobs", params={"stage": "FAILED", "paper_id": paper_id}
    ).json()

    assert payload["total"] == 1
    assert payload["jobs"][0]["paper_id"] == paper_id


# --------------------------------------------------------------------------- #
# the response echoes the window, so the browser can render "第 x / y 页"
# --------------------------------------------------------------------------- #
def test_the_response_echoes_the_requested_window(client, session_factory):
    make_job(session_factory)

    payload = client.get("/api/jobs", params={"limit": 5, "offset": 0}).json()

    assert (payload["limit"], payload["offset"], payload["stage"]) == (5, 0, None)


def test_defaults_keep_the_old_behaviour(client, session_factory):
    for minute in range(3):
        make_job(session_factory, minutes=minute)

    payload = client.get("/api/jobs").json()

    assert payload["limit"] == ingest.DEFAULT_JOB_LIMIT
    assert payload["offset"] == 0
    assert payload["total"] == 3


# --------------------------------------------------------------------------- #
# service level: the SQL window is what the endpoint promised
# --------------------------------------------------------------------------- #
def test_service_offset_is_applied_in_sql(session_factory):
    make_job(session_factory, minutes=0)
    middle = make_job(session_factory, minutes=1)
    make_job(session_factory, minutes=2)

    session = session_factory()
    try:
        rows, total = ingest.list_jobs(session, limit=1, offset=1)
    finally:
        session.close()

    assert total == 3
    assert [row.id for row in rows] == [middle]


def _make_paper(session_factory) -> str:
    """A minimal live paper row, for the ``paper_id`` filter test."""
    from app.db.models import Paper, new_uuid

    session = session_factory()
    try:
        paper_id = new_uuid()
        paper = Paper(
            id=paper_id,
            title="jobs api fixture",
            fingerprint=f"sha256:jobs-api-{paper_id}",
            status="INDEXED",
        )
        session.add(paper)
        session.commit()
        return paper_id
    finally:
        session.close()
