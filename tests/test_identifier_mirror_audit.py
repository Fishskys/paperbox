"""The read-only audit of the ``papers.doi`` / ``arxiv_id`` mirror columns.

The mirrors are application-maintained, so this watchdog must (a) report every
kind of drift and (b) stay strictly read-only. It runs on in-memory SQLite.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import consistency as consistency_api
from app.db.models import Paper, PaperIdentifier, new_uuid
from app.main import app
from app.services import identifier_mirror_audit
from tests.test_job_progress import factory  # noqa: F401 - fixture


def make_paper(session, *, doi=None, arxiv_id=None, deleted=False) -> Paper:
    from datetime import datetime, timezone

    paper = Paper(
        id=new_uuid(),
        title="A Paper",
        fingerprint=f"sha256:{new_uuid()}",
        status="INDEXED",
        doi=doi,
        arxiv_id=arxiv_id,
    )
    if deleted:
        paper.deleted_at = datetime.now(timezone.utc)
    session.add(paper)
    session.commit()
    return paper


def add_identifier(session, paper_id, scheme, value, normalized=None) -> None:
    session.add(
        PaperIdentifier(
            id=new_uuid(),
            paper_id=paper_id,
            scheme=scheme,
            value=value,
            normalized_value=normalized if normalized is not None else value,
            is_primary=(scheme == "doi"),
        )
    )
    session.commit()


def kinds(report):
    return {drift.kind for drift in report.drifts}


# --------------------------------------------------------------------------- #
# the audit
# --------------------------------------------------------------------------- #
def test_a_consistent_library_reports_nothing(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = make_paper(session, doi="10.1/x", arxiv_id="2401.00001")
    add_identifier(session, paper.id, "doi", "10.1/x")
    add_identifier(session, paper.id, "arxiv", "2401.00001")
    session.close()

    report = identifier_mirror_audit.audit_identifier_mirrors(factory)

    assert report.consistent, report.as_dict()
    assert report.drift_totals == {}
    assert report.mirrors_set.get("doi") == 1
    assert report.mirrors_set.get("arxiv") == 1
    assert report.identifiers_total.get("doi") == 1


def test_mirror_set_without_identifier_is_flagged(factory) -> None:  # noqa: ANN001
    session = factory()
    make_paper(session, doi="10.1/orphan")
    session.close()

    report = identifier_mirror_audit.audit_identifier_mirrors(factory)

    assert "mirror_without_identifier" in kinds(report)
    assert report.drift_totals["mirror_without_identifier"] == 1
    assert not report.consistent


def test_identifier_without_mirror_is_flagged(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = make_paper(session)  # no mirror value
    add_identifier(session, paper.id, "doi", "10.1/only-row")
    session.close()

    report = identifier_mirror_audit.audit_identifier_mirrors(factory)

    assert "identifier_without_mirror" in kinds(report)
    assert report.drift_totals["identifier_without_mirror"] == 1


def test_value_mismatch_is_flagged(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = make_paper(session, doi="10.1/mirror")
    add_identifier(session, paper.id, "doi", "10.1/different")
    session.close()

    report = identifier_mirror_audit.audit_identifier_mirrors(factory)

    assert "value_mismatch" in kinds(report)


def test_duplicate_identifier_is_flagged(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = make_paper(session, doi="10.1/a")
    add_identifier(session, paper.id, "doi", "10.1/a")
    add_identifier(session, paper.id, "doi", "10.1/b")
    session.close()

    report = identifier_mirror_audit.audit_identifier_mirrors(factory)

    assert "duplicate_identifier" in kinds(report)


def test_identifier_on_a_deleted_paper_is_an_orphan(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = make_paper(session, doi="10.1/gone", deleted=True)
    add_identifier(session, paper.id, "doi", "10.1/gone")
    session.close()

    report = identifier_mirror_audit.audit_identifier_mirrors(factory)

    assert "orphan_identifier" in kinds(report)


def test_missing_normalized_value_is_flagged(factory) -> None:  # noqa: ANN001
    session = factory()
    paper = make_paper(session)
    add_identifier(session, paper.id, "doi", "10.1/raw", normalized="")
    session.close()

    report = identifier_mirror_audit.audit_identifier_mirrors(factory)

    assert "missing_normalized_value" in kinds(report)


def test_the_listing_is_capped_but_totals_stay_exact(factory) -> None:  # noqa: ANN001
    session = factory()
    for _ in range(5):
        make_paper(session, doi="10.1/orphan")
    session.close()

    report = identifier_mirror_audit.audit_identifier_mirrors(factory, limit=2)

    assert len(report.drifts) == 2
    assert report.truncated is True
    assert report.drift_totals["mirror_without_identifier"] == 5


# --------------------------------------------------------------------------- #
# the endpoint
# --------------------------------------------------------------------------- #
class _FakeAuditor:
    def __init__(self, report) -> None:
        self._report = report

    def __call__(self, *, limit: int):  # noqa: ANN001
        return self._report


def test_endpoint_returns_the_report(factory) -> None:  # noqa: ANN001
    session = factory()
    make_paper(session, doi="10.1/x")
    session.close()

    real = identifier_mirror_audit.audit_identifier_mirrors(factory)
    app.dependency_overrides[consistency_api.get_mirror_auditor] = lambda: _FakeAuditor(real)
    try:
        client = TestClient(app)
        response = client.get("/api/consistency/doi")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    # This fake library has a mirror without an identifier -> drift reported.
    assert body["consistent"] is False
    assert body["drift_totals"]["mirror_without_identifier"] == 1
