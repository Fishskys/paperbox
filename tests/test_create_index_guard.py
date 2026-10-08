"""``scripts/create_index.py --migrate-from`` refuses a dimension change.

``_reindex`` copies vectors verbatim, so a migration whose source index was
built for another embedding width must abort **before anything is created** --
the honest path for a new model is a full re-embed, never a copy. This file
pins the guard with a fake client only (unit tests never touch OpenSearch).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

CREATE_INDEX_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "create_index.py"
)


def load_script():
    spec = importlib.util.spec_from_file_location("paperbox_create_index", CREATE_INDEX_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuardIndices:
    """The old index exists and answers its mapping; any create is a bug."""

    def __init__(self, dimension: int) -> None:
        self._mapping = {
            "old": {
                "mappings": {
                    "properties": {
                        "embedding": {"type": "knn_vector", "dimension": dimension}
                    }
                }
            }
        }

    def exists(self, index: str) -> bool:
        return index == "old"

    def get_mapping(self, index: str) -> dict:
        return self._mapping

    def create(self, index: str, body: dict) -> None:
        raise AssertionError(
            "the dimension guard must abort before any index is created"
        )


class GuardClient:
    def __init__(self, dimension: int) -> None:
        self.indices = GuardIndices(dimension)


def test_a_dimension_change_is_refused_before_anything_is_created() -> None:
    script = load_script()
    exit_code = script.migrate(
        GuardClient(768), old_index="old", new_index="new", alias="paper_chunks_current"
    )
    assert exit_code == 2


def test_the_same_dimension_passes_the_guard(monkeypatch) -> None:
    """Same width: the guard stays quiet and the flow reaches ``ensure_index``."""

    script = load_script()

    class ReachedEnsureIndex(Exception):
        pass

    def ensure_index(client, *, index, alias):
        raise ReachedEnsureIndex(index)

    monkeypatch.setattr(script, "ensure_index", ensure_index)
    with pytest.raises(ReachedEnsureIndex):
        script.migrate(
            GuardClient(1024),
            old_index="old",
            new_index="new",
            alias="paper_chunks_current",
        )
