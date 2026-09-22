"""``scripts/refresh_index_metadata.py`` + the bulk-update plumbing behind it.

Refreshing the snapshot must not touch the embedding or the text, and it must
happen *after* the mapping declares the new fields (otherwise ``dynamic: true``
maps ``pages`` / ``paper_type`` / ``identifiers`` as ``text`` and the explicit
keyword type can no longer be applied). The fake client below records what the
real ``opensearchpy`` bulk helper sends, so both properties are pinned.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.db.models import (
    Paper,
    PaperChunk,
    PaperIdentifier,
    PaperTag,
    PapersTag,
    Venue,
    new_uuid,
)
from app.search import opensearch
from opensearchpy.serializer import JSONSerializer

from scripts import refresh_index_metadata as refresh
from tests.test_job_progress import factory  # noqa: F401 - fixture


# --------------------------------------------------------------------------- #
# fake OpenSearch client (satisfies opensearchpy.helpers.bulk)
# --------------------------------------------------------------------------- #
class FakeTransport:
    """The bulk helper serializes actions through ``client.transport.serializer``."""

    def __init__(self) -> None:
        self.serializer = JSONSerializer()


class RecordingClient:
    """Records bulk actions and mapping updates; ``indices`` is itself."""

    def __init__(self, *, missing_ids: set[str] | None = None) -> None:
        self.actions: list[dict] = []
        self.mapping_calls: list[dict] = []
        self.refreshed: list[str] = []
        self.missing_ids = set(missing_ids or ())
        self.indices = self
        self.transport = FakeTransport()

    def bulk(self, body, **kwargs):
        items = []
        for action in self._iter_actions(body):
            self.actions.append(action)
            doc_id = action.get("_id")
            if doc_id in self.missing_ids:
                items.append({"update": {"_id": doc_id, "status": 404}})
            else:
                items.append({"update": {"_id": doc_id, "status": 200}})
        return {"items": items, "errors": bool(self.missing_ids), "took": 1}

    @staticmethod
    def _iter_actions(body):
        """The helper hands ``bulk`` the serialized NDJSON body, not the actions."""
        if not isinstance(body, str):
            yield from body
            return
        lines = [line for line in body.split("\n") if line.strip()]
        for index in range(0, len(lines), 2):
            meta = json.loads(lines[index])["update"]
            doc = json.loads(lines[index + 1]).get("doc", {})
            yield {
                "_op_type": "update",
                "_index": meta.get("_index"),
                "_id": meta.get("_id"),
                "doc": doc,
            }

    def put_mapping(self, index, body):
        self.mapping_calls.append({"index": index, "body": body})
        return {"acknowledged": True}

    def refresh(self, index):
        self.refreshed.append(index)
        return {"_shards": {"total": 1, "successful": 1}}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def add_paper(
    session_factory,
    *,
    chunks: int = 2,
    deleted: bool = False,
    with_metadata: bool = True,
) -> str:
    """One paper with ``chunks`` chunk rows and an optional metadata snapshot."""
    session = session_factory()
    try:
        venue = None
        if with_metadata:
            venue = (
                session.query(Venue).filter(Venue.normalized_name == "isscc").one_or_none()
            )
            if venue is None:
                venue = Venue(
                    id=new_uuid(), name="ISSCC", normalized_name="isscc", kind="conference"
                )
                session.add(venue)
        paper = Paper(
            id=new_uuid(),
            title="A Low Power SRAM",
            fingerprint=f"sha256:{new_uuid()}",
            status="INDEXED",
            year=2021,
            venue=venue,
            venue_year=2021,
            paper_type="conference",
            volume="64",
            pages="412-419",
            publication_date=date(2021, 3, 1),
        )
        if deleted:
            from datetime import datetime, timezone

            paper.deleted_at = datetime.now(timezone.utc)
        session.add(paper)
        session.flush()
        if with_metadata:
            session.add(
                PaperIdentifier(
                    id=new_uuid(),
                    paper_id=paper.id,
                    scheme="ieee_article_number",
                    value="7065247",
                    normalized_value="7065247",
                )
            )
            tag = PaperTag(
                id=new_uuid(), name="Low Power SRAM", normalized_name=f"low power sram {new_uuid()[:6]}"
            )
            session.add(tag)
            session.flush()
            session.add(
                PapersTag(
                    id=new_uuid(), paper_id=paper.id, tag_id=tag.id, kind="ieee_terms"
                )
            )
        for index in range(chunks):
            session.add(
                PaperChunk(
                    id=new_uuid(),
                    paper_id=paper.id,
                    chunk_index=index,
                    page_start=1,
                    page_end=1,
                    section="body",
                    text=f"chunk {index}",
                    token_count=2,
                    char_count=7,
                )
            )
        session.commit()
        return paper.id
    finally:
        session.close()


def run_script(monkeypatch, *args: str) -> int:
    import sys

    monkeypatch.setattr(sys, "argv", ["refresh_index_metadata.py", *args])
    return refresh.main()


# --------------------------------------------------------------------------- #
# bulk_update_documents / update_mapping
# --------------------------------------------------------------------------- #
def test_bulk_update_documents_sends_a_partial_update_per_chunk() -> None:
    client = RecordingClient()
    report = opensearch.bulk_update_documents(
        [
            {"chunk_id": "c1", "doc": {"venue_year": 2021}},
            {"chunk_id": "c2", "doc": {"venue_year": 2021}},
        ],
        client=client,
        index="an-index",
    )
    assert report == {"updated": 2, "failed": 0}
    assert [action["_id"] for action in client.actions] == ["c1", "c2"]
    assert all(action["_op_type"] == "update" for action in client.actions)
    # A partial update: the embedding and the text are not part of the payload.
    assert client.actions[0]["doc"] == {"venue_year": 2021}
    assert "embedding" not in client.actions[0]["doc"]
    assert client.refreshed == ["an-index"]


def test_bulk_update_documents_counts_failures() -> None:
    client = RecordingClient(missing_ids={"c2"})
    report = opensearch.bulk_update_documents(
        [{"chunk_id": "c1", "doc": {}}, {"chunk_id": "c2", "doc": {}}], client=client
    )
    assert report["updated"] == 1
    assert report["failed"] == 1


def test_bulk_update_documents_without_items_does_not_touch_the_client() -> None:
    client = RecordingClient()
    assert opensearch.bulk_update_documents([], client=client) == {
        "updated": 0,
        "failed": 0,
    }
    assert client.actions == []


def test_update_mapping_sends_the_declared_properties() -> None:
    client = RecordingClient()
    result = opensearch.update_mapping(client, index="an-index")
    assert result["updated"] is True
    assert client.mapping_calls[0]["index"] == "an-index"
    properties = client.mapping_calls[0]["body"]["properties"]
    assert properties["venue_year"] == {"type": "integer"}
    assert properties["publication_date"] == {"type": "date"}
    assert properties["source_tags"] == {"type": "keyword"}


def test_update_mapping_reports_a_missing_index() -> None:
    class Missing(RecordingClient):
        def put_mapping(self, index, body):
            from opensearchpy.exceptions import NotFoundError

            raise NotFoundError(404, "no such index")

    result = opensearch.update_mapping(Missing(), index="nope")
    assert result == {"index": "nope", "updated": False, "exists": False}


# --------------------------------------------------------------------------- #
# the script
# --------------------------------------------------------------------------- #
def test_the_paper_id_query_selects_what_it_orders_by() -> None:
    """PostgreSQL rejects ``SELECT DISTINCT`` + ``ORDER BY`` on a non-selected
    column (SQLite accepts it), so the ordering columns must be selected too.
    """
    statement = refresh.paper_ids_statement()
    selected = {column.name for column in statement.selected_columns}
    ordered = {
        clause.name for clause in statement._order_by_clauses if hasattr(clause, "name")
    }
    assert ordered == {"created_at", "id"}
    assert ordered <= selected


def test_the_script_refreshes_every_chunk_of_every_live_paper(monkeypatch, factory) -> None:  # noqa: F811
    first = add_paper(factory, chunks=2)
    second = add_paper(factory, chunks=1)
    client = RecordingClient()
    monkeypatch.setattr(refresh, "SessionLocal", factory)
    monkeypatch.setattr(opensearch, "get_client", lambda: client)

    assert run_script(monkeypatch) == 0
    assert len(client.actions) == 3
    assert client.mapping_calls  # mapping first, documents after
    assert len({action["_index"] for action in client.actions}) == 1
    assert first and second


def test_the_script_sends_the_metadata_snapshot(monkeypatch, factory) -> None:  # noqa: F811
    add_paper(factory, chunks=1)
    client = RecordingClient()
    monkeypatch.setattr(refresh, "SessionLocal", factory)
    monkeypatch.setattr(opensearch, "get_client", lambda: client)

    assert run_script(monkeypatch) == 0
    doc = client.actions[0]["doc"]
    assert doc["venue"] == "ISSCC"
    assert doc["venue_year"] == 2021
    assert doc["paper_type"] == "conference"
    assert doc["volume"] == "64"
    assert doc["pages"] == "412-419"
    assert doc["publication_date"] == "2021-03-01"
    assert doc["identifiers"] == ["ieee_article_number:7065247"]
    assert doc["ieee_terms"] == ["Low Power SRAM"]
    assert doc["source_tags"] == []


def test_the_script_skips_deleted_papers(monkeypatch, factory) -> None:  # noqa: F811
    add_paper(factory, chunks=2, deleted=True)
    live = add_paper(factory, chunks=1)
    client = RecordingClient()
    monkeypatch.setattr(refresh, "SessionLocal", factory)
    monkeypatch.setattr(opensearch, "get_client", lambda: client)

    assert run_script(monkeypatch) == 0
    assert len(client.actions) == 1
    assert live


def test_the_script_skips_papers_without_chunks(monkeypatch, factory) -> None:  # noqa: F811
    add_paper(factory, chunks=0)
    client = RecordingClient()
    monkeypatch.setattr(refresh, "SessionLocal", factory)
    monkeypatch.setattr(opensearch, "get_client", lambda: client)

    assert run_script(monkeypatch) == 0
    assert client.actions == []


def test_a_dry_run_sends_nothing(monkeypatch, factory) -> None:  # noqa: F811
    add_paper(factory, chunks=2)
    client = RecordingClient()
    monkeypatch.setattr(refresh, "SessionLocal", factory)
    monkeypatch.setattr(opensearch, "get_client", lambda: client)

    assert run_script(monkeypatch, "--dry-run") == 0
    assert client.actions == []
    assert client.mapping_calls == []


def test_the_paper_id_option_limits_the_run(monkeypatch, factory) -> None:  # noqa: F811
    wanted = add_paper(factory, chunks=2)
    add_paper(factory, chunks=2)
    client = RecordingClient()
    monkeypatch.setattr(refresh, "SessionLocal", factory)
    monkeypatch.setattr(opensearch, "get_client", lambda: client)

    assert run_script(monkeypatch, "--paper-id", wanted) == 0
    assert len(client.actions) == 2


def test_the_no_mapping_option_skips_the_mapping_update(monkeypatch, factory) -> None:  # noqa: F811
    add_paper(factory, chunks=1)
    client = RecordingClient()
    monkeypatch.setattr(refresh, "SessionLocal", factory)
    monkeypatch.setattr(opensearch, "get_client", lambda: client)

    assert run_script(monkeypatch, "--no-mapping") == 0
    assert client.mapping_calls == []
    assert len(client.actions) == 1