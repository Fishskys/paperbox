"""The startup three-way dimension check (2026-10-07).

The dimension used to be stated in three places that never talked: the model
the container actually runs, ``EMBEDDING_DIMENSION`` and the live index
mapping. What is pinned here: the two probes never raise (unreachable is a
warning, ``None``), the mapping reader understands both mapping shapes, and
``check_dimension_consistency`` raises *only* on a measured mismatch -- a
misconfigured deployment must not come up, but the app may still start before
its containers do.
"""

from __future__ import annotations

import pytest

from app.services import embedding_service
from app.search import mappings, opensearch


# --------------------------------------------------------------------------- #
# mapping_embedding_dimension: one reader for both mapping shapes
# --------------------------------------------------------------------------- #


def test_get_mapping_shape_is_read() -> None:
    mapping = {
        "paper_chunks_v3": {
            "mappings": {"properties": {"embedding": {"type": "knn_vector", "dimension": 1024}}}
        }
    }
    assert opensearch.mapping_embedding_dimension(mapping) == 1024


def test_build_mapping_shape_is_read() -> None:
    body = mappings.build_mapping()
    assert opensearch.mapping_embedding_dimension(body) == (
        body["mappings"]["properties"]["embedding"]["dimension"]
    )


def test_a_mapping_without_the_embedding_field_is_unknown() -> None:
    assert opensearch.mapping_embedding_dimension({}) is None
    is_none = opensearch.mapping_embedding_dimension(
        {"i": {"mappings": {"properties": {"text": {"type": "text"}}}}}
    )
    assert is_none is None


def test_a_non_integer_dimension_is_unknown() -> None:
    mapping = {"i": {"mappings": {"properties": {"embedding": {"dimension": "1024"}}}}}
    assert opensearch.mapping_embedding_dimension(mapping) is None


# --------------------------------------------------------------------------- #
# the probes: unreachable / unknown is None, never an exception
# --------------------------------------------------------------------------- #


def test_container_probe_returns_the_reported_dimension(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"model": "m", "dimension": 768}

    monkeypatch.setattr(
        embedding_service.httpx, "get", lambda url, timeout: FakeResponse()
    )
    assert embedding_service.container_dimension() == 768


def test_container_probe_swallows_a_dead_container(monkeypatch) -> None:
    def boom(url, timeout):
        raise ConnectionError("refused")

    monkeypatch.setattr(embedding_service.httpx, "get", boom)
    assert embedding_service.container_dimension() is None


def test_container_probe_treats_non_200_as_unknown(monkeypatch) -> None:
    class FakeResponse:
        status_code = 503

        def json(self):  # pragma: no cover - must not be reached
            raise AssertionError("a non-200 body must not be parsed")

    monkeypatch.setattr(
        embedding_service.httpx, "get", lambda url, timeout: FakeResponse()
    )
    assert embedding_service.container_dimension() is None


def test_container_probe_treats_a_null_dimension_as_unknown(monkeypatch) -> None:
    """The container reports null until its model is lazily loaded."""

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"model": "m", "dimension": None}

    monkeypatch.setattr(
        embedding_service.httpx, "get", lambda url, timeout: FakeResponse()
    )
    assert embedding_service.container_dimension() is None


def test_index_probe_skips_a_missing_index(monkeypatch) -> None:
    monkeypatch.setattr(opensearch, "index_exists", lambda client, index: False)

    class Fail:
        def __getattr__(self, name):  # pragma: no cover - must not be reached
            raise AssertionError("a missing index must not be probed")

    monkeypatch.setattr(opensearch, "get_client", lambda: Fail())
    assert embedding_service.index_dimension() is None


def test_index_probe_returns_the_mapping_dimension(monkeypatch) -> None:
    monkeypatch.setattr(opensearch, "index_exists", lambda client, index: True)

    class FakeIndices:
        def get_mapping(self, index):
            return {
                index: {
                    "mappings": {
                        "properties": {"embedding": {"type": "knn_vector", "dimension": 1024}}
                    }
                }
            }

    class FakeClient:
        indices = FakeIndices()

    monkeypatch.setattr(opensearch, "get_client", lambda: FakeClient())
    assert embedding_service.index_dimension(index="paper_chunks_v3") == 1024


def test_index_probe_swallows_a_dead_opensearch(monkeypatch) -> None:
    monkeypatch.setattr(opensearch, "index_exists", lambda client, index: True)

    class FakeIndices:
        def get_mapping(self, index):
            raise RuntimeError("opensearch is down")

    class FakeClient:
        indices = FakeIndices()

    monkeypatch.setattr(opensearch, "get_client", lambda: FakeClient())
    assert embedding_service.index_dimension() is None


# --------------------------------------------------------------------------- #
# the check: only a measured mismatch stops the process
# --------------------------------------------------------------------------- #


def test_agreeing_sides_pass(monkeypatch) -> None:
    monkeypatch.setattr(embedding_service, "container_dimension", lambda: 1024)
    monkeypatch.setattr(embedding_service, "index_dimension", lambda: 1024)
    report = embedding_service.check_dimension_consistency()
    assert report == {"expected": 1024, "container": 1024, "index": 1024}


def test_unknown_sides_pass(monkeypatch) -> None:
    """Containers down or model not loaded: warn and come up."""
    monkeypatch.setattr(embedding_service, "container_dimension", lambda: None)
    monkeypatch.setattr(embedding_service, "index_dimension", lambda: None)
    report = embedding_service.check_dimension_consistency()
    assert report["container"] is None and report["index"] is None


def test_a_container_mismatch_stops_startup(monkeypatch) -> None:
    """The model produces 768 dims while EMBEDDING_DIMENSION says 1024."""
    monkeypatch.setattr(embedding_service, "container_dimension", lambda: 768)
    monkeypatch.setattr(embedding_service, "index_dimension", lambda: None)
    with pytest.raises(RuntimeError, match="EMBEDDING_DIMENSION"):
        embedding_service.check_dimension_consistency()


def test_an_index_mismatch_stops_startup(monkeypatch) -> None:
    """The live index was built for another width -- writes would all fail."""
    monkeypatch.setattr(embedding_service, "container_dimension", lambda: 1024)
    monkeypatch.setattr(embedding_service, "index_dimension", lambda: 768)
    with pytest.raises(RuntimeError, match="OPENSEARCH_INDEX"):
        embedding_service.check_dimension_consistency()