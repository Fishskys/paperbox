"""Tests for the delete flow (plan section 23).

``DELETE /api/papers/{id}`` must leave PostgreSQL, OpenSearch and MinIO in the
same state: chunk documents dropped, stored objects removed, then the row marked
deleted. These tests call the endpoint function directly with the collaborators
replaced by recording fakes, so nothing touches a live service.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import papers as papers_api
from app.search.opensearch import SearchIndexError
from app.services import object_storage


class FakeSession:
    """Only ``commit`` is used by the endpoint."""

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(
        paper=SimpleNamespace(id="p1", files=[]),
        order=[],
        chunks=7,
        objects=2,
        error=None,
        soft_deleted=[],
        session=FakeSession(),
    )

    def fake_get_paper(_session, _paper_id):
        return state.paper

    def fake_delete_chunks(paper_id, **_kwargs):
        if state.error == "opensearch":
            raise SearchIndexError("cluster unreachable")
        state.order.append("opensearch")
        return state.chunks

    def fake_delete_prefix(paper_id, bucket=None):  # noqa: ARG001
        if state.error == "minio":
            raise object_storage.ObjectStorageError("bucket unreachable")
        state.order.append("minio")
        return state.objects

    def fake_soft_delete(_session, paper):
        state.order.append("soft_delete")
        state.soft_deleted.append(paper.id)
        return paper

    monkeypatch.setattr(papers_api.papers, "get_paper", fake_get_paper)
    monkeypatch.setattr(papers_api.opensearch, "delete_by_paper_id", fake_delete_chunks)
    monkeypatch.setattr(papers_api.object_storage, "delete_prefix", fake_delete_prefix)
    monkeypatch.setattr(papers_api.papers, "soft_delete_paper", fake_soft_delete)
    return state


def test_delete_purges_index_then_objects_then_marks_deleted(env) -> None:
    response = papers_api.delete_paper("p1", session=env.session)

    assert response.status_code == 204
    assert env.order == ["opensearch", "minio", "soft_delete"]
    assert env.soft_deleted == ["p1"]
    assert env.session.commits == 1


def test_delete_returns_404_for_unknown_or_already_deleted_paper(env) -> None:
    env.paper = None

    with pytest.raises(HTTPException) as excinfo:
        papers_api.delete_paper("missing", session=env.session)

    assert excinfo.value.status_code == 404
    assert env.order == []
    assert env.session.commits == 0


def test_delete_aborts_without_marking_when_index_cleanup_fails(env) -> None:
    env.error = "opensearch"

    with pytest.raises(HTTPException) as excinfo:
        papers_api.delete_paper("p1", session=env.session)

    assert excinfo.value.status_code == 503
    assert "search index cleanup failed" in excinfo.value.detail
    assert env.order == []
    assert env.soft_deleted == []
    assert env.session.commits == 0


def test_delete_aborts_without_marking_when_object_cleanup_fails(env) -> None:
    env.error = "minio"

    with pytest.raises(HTTPException) as excinfo:
        papers_api.delete_paper("p1", session=env.session)

    assert excinfo.value.status_code == 503
    assert "object storage cleanup failed" in excinfo.value.detail
    # the idempotent index purge already ran, but the paper must stay visible
    assert env.order == ["opensearch"]
    assert env.soft_deleted == []
    assert env.session.commits == 0


def test_delete_can_be_retried_after_a_partial_failure(env) -> None:
    env.error = "minio"
    with pytest.raises(HTTPException):
        papers_api.delete_paper("p1", session=env.session)

    env.error = None
    response = papers_api.delete_paper("p1", session=env.session)

    assert response.status_code == 204
    assert env.order == ["opensearch", "opensearch", "minio", "soft_delete"]
    assert env.soft_deleted == ["p1"]
    assert env.session.commits == 1
