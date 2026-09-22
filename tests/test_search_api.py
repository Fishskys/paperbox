"""``POST /api/search`` error mapping (2026-09-22).

The handler is a thin shell around :func:`app.services.search_service.search_papers`
and the only thing it owns is the mapping from a backend failure to HTTP: a broken
OpenSearch or embedding backend must reach the caller as **503 "search backend
unavailable"**, not as an unhandled exception (which arrives as a 500).

The regression these tests pin: ``app/api/search.py`` catches
``search_service.SearchError``, while the class itself is defined in
``app.search.hybrid``. Without the re-export in the service module the ``except``
clause raised ``AttributeError`` on the failure path -- the 503 never happened and
no test noticed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.security import require_api_key
from app.db.session import get_db
from app.main import app
from app.search import hybrid
from app.services import search_service
from tests.test_local_source import factory  # noqa: F401 - fixture


@pytest.fixture
def client(factory):  # noqa: F811 - fixture comes from the import above
    """A TestClient wired to the in-memory database and a stub API key."""

    def _db():
        session = factory()
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


def test_the_service_namespace_exposes_the_search_error() -> None:
    """``api/search.py`` translates this exact attribute into a 503."""
    assert search_service.SearchError is hybrid.SearchError


def test_a_backend_failure_is_a_503_not_a_500(client, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise hybrid.SearchError("search index 'paper_chunks_v2' does not exist")

    monkeypatch.setattr(search_service, "search_papers", boom)

    response = client.post(
        "/api/search", json={"query": "low power SRAM", "mode": "hybrid"}
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "search backend unavailable"


def test_a_duplicate_fingerprint_message_is_not_confused_with_a_backend_error(
    client, monkeypatch
) -> None:
    """``ValueError`` stays a 422 -- the two paths must not be merged."""

    def boom(*args, **kwargs):
        raise ValueError("top_k must be positive")

    monkeypatch.setattr(search_service, "search_papers", boom)

    response = client.post("/api/search", json={"query": "sram", "mode": "keyword"})

    assert response.status_code == 422
