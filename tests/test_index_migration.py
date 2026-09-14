"""Analyzer mapping + alias migration helpers (SPEC-P1 section H1).

No cluster is contacted: the mapping body and the ``_aliases`` / gate payloads
are pure functions, and the migration flow is exercised against a recording
fake client.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.core.config import settings
from app.search import mappings, opensearch

ROOT = Path(__file__).resolve().parents[1]


def load_create_index():
    """Import ``scripts/create_index.py`` without running ``main()``."""
    spec = importlib.util.spec_from_file_location(
        "create_index_script", ROOT / "scripts" / "create_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


create_index = load_create_index()


# --------------------------------------------------------------------------- #
# mapping: CJK analyzer on the free-text fields
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", ["title", "text", "section_title"])
def test_free_text_fields_use_the_cjk_analyzer(field: str) -> None:
    properties = mappings.build_mapping()["mappings"]["properties"]

    assert properties[field]["type"] == "text"
    assert properties[field]["analyzer"] == "cjk"
    assert properties[field]["search_analyzer"] == "cjk"


def test_cjk_is_the_shared_analyzer_constant() -> None:
    assert mappings.TEXT_ANALYZER == "cjk"
    assert set(mappings.ANALYZED_FIELDS) == {"title", "section_title", "text"}


@pytest.mark.parametrize(
    "field",
    ["chunk_id", "paper_id", "authors", "venue", "doi", "arxiv_id", "tags", "section"],
)
def test_keyword_fields_are_untouched(field: str) -> None:
    properties = mappings.build_mapping()["mappings"]["properties"]

    assert properties[field] == {"type": "keyword"}


def test_int_fields_and_embedding_are_unchanged() -> None:
    properties = mappings.build_mapping()["mappings"]["properties"]

    for field in ("year", "page_start", "page_end", "chunk_index"):
        assert properties[field] == {"type": "integer"}

    embedding = properties["embedding"]
    assert embedding["type"] == "knn_vector"
    assert embedding["dimension"] == settings.embedding_dimension
    assert embedding["method"] == {
        "name": "hnsw",
        "space_type": "l2",
        "engine": "lucene",
        "parameters": {"ef_construction": 128, "m": 16},
    }


def test_index_settings_still_enable_knn() -> None:
    settings_body = mappings.build_mapping()["settings"]["index"]

    assert settings_body["knn"] is True
    assert settings_body["number_of_shards"] == 1
    assert settings_body["number_of_replicas"] == 0


# --------------------------------------------------------------------------- #
# migration payloads
# --------------------------------------------------------------------------- #
def test_reindex_body_copies_the_source_verbatim() -> None:
    body = opensearch.build_reindex_body("paper_chunks_v1", "paper_chunks_v2")

    assert body == {
        "source": {"index": "paper_chunks_v1"},
        "dest": {"index": "paper_chunks_v2"},
    }
    # No script / no _source filtering: embeddings travel with the document.
    assert "script" not in body["source"]
    assert "_source" not in body["source"]


def test_alias_swap_body_removes_then_adds_with_write_index() -> None:
    body = opensearch.build_alias_swap_body(
        "paper_chunks_v1", "paper_chunks_v2", "paper_chunks_current"
    )

    assert body == {
        "actions": [
            {"remove": {"index": "paper_chunks_v1", "alias": "paper_chunks_current"}},
            {
                "add": {
                    "index": "paper_chunks_v2",
                    "alias": "paper_chunks_current",
                    "is_write_index": True,
                }
            },
        ]
    }


def test_alias_swap_body_only_adds_when_the_alias_is_unbound() -> None:
    body = opensearch.build_alias_swap_body("", "paper_chunks_v2", "paper_chunks_current")

    assert body["actions"] == [
        {
            "add": {
                "index": "paper_chunks_v2",
                "alias": "paper_chunks_current",
                "is_write_index": True,
            }
        }
    ]


def test_alias_swap_body_does_not_remove_the_target_from_itself() -> None:
    body = opensearch.build_alias_swap_body(
        "paper_chunks_v2", "paper_chunks_v2", "paper_chunks_current"
    )

    assert [list(action)[0] for action in body["actions"]] == ["add"]


@pytest.mark.parametrize(
    ("old_count", "new_count", "expected"),
    [(10, 10, True), (0, 0, True), (10, 9, False), (9, 10, False), (10, 0, False)],
)
def test_alias_swap_is_gated_on_equal_document_counts(
    old_count: int, new_count: int, expected: bool
) -> None:
    assert opensearch.alias_swap_is_safe(old_count, new_count) is expected


# --------------------------------------------------------------------------- #
# migration flow against a recording fake client
# --------------------------------------------------------------------------- #
class FakeIndices:
    def __init__(self, state: dict) -> None:
        self.state = state

    def exists(self, index: str) -> bool:
        return index in self.state["indices"]

    def create(self, index: str, body: dict) -> None:
        self.state["indices"].add(index)
        self.state["created"].append(index)

    def get_alias(self, name: str):
        targets = self.state["aliases"].get(name)
        if not targets:
            from opensearchpy.exceptions import NotFoundError

            raise NotFoundError(404, "alias missing")
        return {index: {} for index in targets}

    def update_aliases(self, body: dict) -> None:
        self.state["alias_calls"].append(body)
        for action in body["actions"]:
            for kind, payload in action.items():
                alias = payload["alias"]
                index = payload["index"]
                targets = self.state["aliases"].setdefault(alias, set())
                if kind == "remove":
                    targets.discard(index)
                else:
                    targets.add(index)

    def refresh(self, index: str) -> None:
        self.state["refreshed"].append(index)

    def get_mapping(self, index: str):
        return {index: {"mappings": {"properties": {}}}}


class FakeTasks:
    def get(self, task_id: str):
        return {"completed": True, "task": {"status": {"created": 3, "total": 3}}}


class FakeClient:
    def __init__(self, *, counts: dict, completed_tasks=None) -> None:
        self.state = {
            "indices": set(counts),
            "aliases": {"paper_chunks_current": {"paper_chunks_v1"}},
            "created": [],
            "alias_calls": [],
            "refreshed": [],
            "reindex_calls": [],
        }
        self.counts = dict(counts)
        self.indices = FakeIndices(self.state)
        self.tasks = FakeTasks()

    def count(self, index: str):
        from opensearchpy.exceptions import NotFoundError

        if index not in self.state["indices"]:
            raise NotFoundError(404, "missing")
        return {"count": self.counts.get(index, 0)}

    def reindex(self, body: dict, wait_for_completion: bool = False, refresh: bool = False):
        self.state["reindex_calls"].append(body)
        # Emulate the server-side copy: the new index ends up with the source count.
        self.counts[body["dest"]["index"]] = self.counts.get(body["source"]["index"], 0)
        return {"task": "task-1"}


def test_migrate_reindexes_then_swaps_the_alias() -> None:
    client = FakeClient(counts={"paper_chunks_v1": 3})

    code = create_index.migrate(
        client,
        old_index="paper_chunks_v1",
        new_index="paper_chunks_v2",
        alias="paper_chunks_current",
        poll_interval=0.0,
    )

    assert code == 0
    assert client.state["reindex_calls"] == [
        {"source": {"index": "paper_chunks_v1"}, "dest": {"index": "paper_chunks_v2"}}
    ]
    assert client.state["alias_calls"] == [
        {
            "actions": [
                {"remove": {"index": "paper_chunks_v1", "alias": "paper_chunks_current"}},
                {
                    "add": {
                        "index": "paper_chunks_v2",
                        "alias": "paper_chunks_current",
                        "is_write_index": True,
                    }
                },
            ]
        }
    ]
    assert client.state["aliases"]["paper_chunks_current"] == {"paper_chunks_v2"}
    # The old index is kept for rollback.
    assert "paper_chunks_v1" in client.state["indices"]


def test_migrate_skips_the_copy_when_counts_already_match() -> None:
    client = FakeClient(counts={"paper_chunks_v1": 3, "paper_chunks_v2": 3})

    code = create_index.migrate(
        client,
        old_index="paper_chunks_v1",
        new_index="paper_chunks_v2",
        alias="paper_chunks_current",
        poll_interval=0.0,
    )

    assert code == 0
    assert client.state["reindex_calls"] == []
    assert client.state["aliases"]["paper_chunks_current"] == {"paper_chunks_v2"}


def test_migrate_aborts_before_the_swap_when_counts_differ() -> None:
    client = FakeClient(counts={"paper_chunks_v1": 3})
    # The copy silently loses a document (e.g. a mapping rejection).
    client.reindex = lambda body, wait_for_completion=False, refresh=False: (
        client.counts.update({body["dest"]["index"]: 2}) or {"task": "task-1"}
    )

    code = create_index.migrate(
        client,
        old_index="paper_chunks_v1",
        new_index="paper_chunks_v2",
        alias="paper_chunks_current",
        poll_interval=0.0,
    )

    assert code == 1
    assert client.state["alias_calls"] == []
    assert client.state["aliases"]["paper_chunks_current"] == {"paper_chunks_v1"}


def test_migrate_is_a_noop_when_the_alias_already_points_at_the_new_index() -> None:
    client = FakeClient(counts={"paper_chunks_v1": 3, "paper_chunks_v2": 3})
    client.state["aliases"]["paper_chunks_current"] = {"paper_chunks_v2"}

    code = create_index.migrate(
        client,
        old_index="paper_chunks_v1",
        new_index="paper_chunks_v2",
        alias="paper_chunks_current",
        poll_interval=0.0,
    )

    assert code == 0
    assert client.state["alias_calls"] == []
    assert client.state["reindex_calls"] == []
    assert client.counts["paper_chunks_v2"] == 3


def test_migrate_rejects_a_missing_source_index() -> None:
    client = FakeClient(counts={"paper_chunks_v2": 0})

    code = create_index.migrate(
        client,
        old_index="paper_chunks_v1",
        new_index="paper_chunks_v2",
        alias="paper_chunks_current",
        poll_interval=0.0,
    )

    assert code == 2
    assert client.state["reindex_calls"] == []


def test_migrate_rejects_migrating_an_index_onto_itself() -> None:
    client = FakeClient(counts={"paper_chunks_v1": 3})

    code = create_index.migrate(
        client,
        old_index="paper_chunks_v1",
        new_index="paper_chunks_v1",
        alias="paper_chunks_current",
        poll_interval=0.0,
    )

    assert code == 2
    assert client.state["reindex_calls"] == []


# --------------------------------------------------------------------------- #
# CLI surface: defaults keep today's behaviour
# --------------------------------------------------------------------------- #
def test_defaults_still_target_the_configured_index_and_alias() -> None:
    args = create_index.parse_args([])

    assert args.index is None and args.alias is None
    assert (args.index or opensearch.INDEX) == settings.opensearch_index
    assert (args.alias or opensearch.ALIAS) == settings.opensearch_alias
    assert args.migrate_from is None


def test_cli_overrides_index_alias_and_migration_source() -> None:
    args = create_index.parse_args(
        ["--index", "paper_chunks_v2", "--alias", "other_alias", "--migrate-from", "paper_chunks_v1"]
    )

    assert args.index == "paper_chunks_v2"
    assert args.alias == "other_alias"
    assert args.migrate_from == "paper_chunks_v1"
