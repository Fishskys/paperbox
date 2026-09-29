"""OpenSearch access layer (MVP-SPEC section 7).

The cluster runs single-node with the security plugin disabled, so the client
needs no credentials and no TLS. All reads and writes go through the alias
``paper_chunks_current`` so the physical index can be swapped later.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from opensearchpy import OpenSearch
from opensearchpy.exceptions import NotFoundError, OpenSearchException
from opensearchpy.helpers import bulk

from app.core.config import settings
from app.core.logging import get_logger
from app.search.mappings import TAG_KIND_FIELDS, build_mapping

logger = get_logger(__name__)

#: Alias used for every read and write (plan sections 9/15/24).
ALIAS = settings.opensearch_alias
#: Physical index name; a future reindex writes ``paper_chunks_v2``.
INDEX = settings.opensearch_index

#: Bulk request size (spec: 100-500 documents per batch).
BULK_BATCH_SIZE = 200

_client_holder: list[OpenSearch] = []


class SearchIndexError(RuntimeError):
    """Raised when OpenSearch cannot satisfy a request."""


def get_client() -> OpenSearch:
    """Process-wide OpenSearch client (no auth, no TLS)."""
    if not _client_holder:
        _client_holder.append(
            OpenSearch(
                hosts=[settings.opensearch_url],
                timeout=60,
                max_retries=3,
                retry_on_timeout=True,
            )
        )
    return _client_holder[0]


def index_exists(client: OpenSearch | None = None, index: str = INDEX) -> bool:
    """True when the physical index is already present."""
    return bool((client or get_client()).indices.exists(index=index))


def alias_targets(client: OpenSearch | None = None, alias: str = ALIAS) -> list[str]:
    """Indices the alias currently points at (empty when unknown)."""
    try:
        response = (client or get_client()).indices.get_alias(name=alias)
    except NotFoundError:
        return []
    if not isinstance(response, dict):
        return []
    return sorted(response.keys())


def ensure_index(
    client: OpenSearch | None = None,
    *,
    index: str = INDEX,
    alias: str = ALIAS,
) -> dict[str, Any]:
    """Idempotently create ``index`` and point ``alias`` at it.

    ``knn_vector`` fields cannot be added to an existing index by updating the
    mapping, so the index itself is only ever created once (never re-created
    here). The alias is retargeted when it points somewhere else.

    Returns a small report: ``{"index", "created", "alias", "alias_updated"}``.
    """
    client = client or get_client()
    created = False
    if not index_exists(client, index):
        client.indices.create(index=index, body=build_mapping())
        created = True
        logger.info("created OpenSearch index", extra={"extra_fields": {"index": index}})

    alias_updated = False
    if alias and alias != index:
        targets = alias_targets(client, alias)
        if index not in targets:
            actions: list[dict[str, Any]] = [
                {"remove": {"index": name, "alias": alias}} for name in targets
            ]
            actions.append({"add": {"index": index, "alias": alias}})
            client.indices.update_aliases(body={"actions": actions})
            alias_updated = True
            logger.info(
                "pointed alias at index",
                extra={"extra_fields": {"alias": alias, "index": index}},
            )
    return {
        "index": index,
        "created": created,
        "alias": alias,
        "alias_updated": alias_updated,
        "exists": True,
    }


def update_mapping(
    client: OpenSearch | None = None,
    *,
    index: str = ALIAS,
) -> dict[str, Any]:
    """Add the current mapping's properties to an existing index.

    Adding *new* fields is allowed on a live index - only changing or removing an
    existing field (the ``knn_vector``, the analyzers) needs a new index and a
    ``_reindex`` copy. This is how a metadata snapshot field reaches an index that
    is already deployed, and it must run **before** the first document carrying
    the field is written: ``dynamic: true`` would otherwise map it as ``text``
    (``pages``, ``paper_type``, ``identifiers``) and the explicit type could no
    longer be applied.

    Idempotent: re-sending the same properties is a no-op.
    """
    client = client or get_client()
    body = {"properties": build_mapping()["mappings"]["properties"]}
    try:
        response = client.indices.put_mapping(index=index, body=body)
    except NotFoundError:
        return {"index": index, "updated": False, "exists": False}
    except OpenSearchException as exc:  # pragma: no cover - live cluster only
        raise SearchIndexError(f"update_mapping failed: {exc}") from exc
    logger.info(
        "updated index mapping",
        extra={"extra_fields": {"index": index, "acknowledged": bool(response)}},
    )
    return {"index": index, "updated": True, "exists": True, "acknowledged": bool(response)}


def bulk_update_documents(
    updates: Iterable[dict[str, Any]],
    *,
    client: OpenSearch | None = None,
    index: str = ALIAS,
    batch_size: int = BULK_BATCH_SIZE,
    refresh: bool = True,
) -> dict[str, int]:
    """Partial-update documents in bulk: ``{"chunk_id": ..., "doc": {...}}``.

    Used to rewrite the metadata snapshot of documents that are already indexed -
    the embedding and the text are left alone, so refreshing 3000 chunks costs
    seconds instead of a full re-embedding pass. Unknown documents are skipped by
    OpenSearch rather than created (``doc_as_upsert`` is not set).
    """
    client = client or get_client()
    items = list(updates)
    if not items:
        return {"updated": 0, "failed": 0}
    updated = 0
    failed = 0
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        actions = [
            {
                "_op_type": "update",
                "_index": index,
                "_id": str(item["chunk_id"]),
                "doc": dict(item.get("doc") or {}),
            }
            for item in batch
        ]
        try:
            success, errors = bulk(
                client,
                actions,
                refresh="wait_for" if refresh else False,
                raise_on_error=False,
                stats_only=False,
            )
        except OpenSearchException as exc:  # pragma: no cover - live cluster only
            raise SearchIndexError(f"bulk update failed: {exc}") from exc
        updated += int(success)
        if errors:
            failed += len(errors)
            logger.warning(
                "bulk update reported errors",
                extra={
                    "extra_fields": {
                        "index": index,
                        "first_error": json.dumps(errors[0])[:300],
                    }
                },
            )
    if refresh:
        client.indices.refresh(index=index)
    logger.info(
        "updated documents",
        extra={"extra_fields": {"index": index, "updated": updated, "failed": failed}},
    )
    return {"updated": updated, "failed": failed}


def build_reindex_body(source: str, dest: str) -> dict[str, Any]:
    """Body for a server-side ``_reindex`` copy of every document.

    Documents are copied verbatim (``_source`` untouched), so the 1024-dim
    ``embedding`` vectors travel with them and nothing is re-embedded.
    """
    return {"source": {"index": source}, "dest": {"index": dest}}


def build_alias_swap_body(old_index: str, new_index: str, alias: str) -> dict[str, Any]:
    """Atomically move ``alias`` from ``old_index`` onto ``new_index``.

    One ``_aliases`` call removes the alias from the old index and adds it to
    the new one with ``is_write_index`` set, so writers switch over with the
    alias never pointing at nothing. The old index is left in place (rollback).
    """
    actions: list[dict[str, Any]] = []
    if old_index and old_index != new_index:
        actions.append({"remove": {"index": old_index, "alias": alias}})
    actions.append({"add": {"index": new_index, "alias": alias, "is_write_index": True}})
    return {"actions": actions}


def alias_swap_is_safe(old_count: int, new_count: int) -> bool:
    """True only when the copy completed: both sides must hold the same count."""
    return old_count == new_count


def _as_string_list(values: Any) -> list[str]:
    """Stringify a list-ish document field (``None`` becomes ``[]``)."""
    if not values:
        return []
    return [str(value) for value in values]


def build_chunk_document(row: dict[str, Any]) -> dict[str, Any]:
    """Shape one ``paper_chunks`` row (+ paper metadata) into an ES document."""
    authors = row.get("authors") or []
    tags = row.get("tags") or []
    document: dict[str, Any] = {
        "chunk_id": str(row["chunk_id"]),
        "paper_id": str(row["paper_id"]),
        "title": row.get("title"),
        "authors": [str(name) for name in authors],
        "year": row.get("year"),
        "venue": row.get("venue"),
        "doi": row.get("doi"),
        "arxiv_id": row.get("arxiv_id"),
        "tags": [str(tag) for tag in tags],
        "section": row.get("section"),
        "section_title": row.get("section_title"),
        "page_start": row.get("page_start"),
        "page_end": row.get("page_end"),
        "chunk_index": row.get("chunk_index"),
        "text": row.get("text") or "",
        "embedding_model": row.get("embedding_model") or settings.embedding_model,
        "embedding_dimension": row.get("embedding_dimension")
        or settings.embedding_dimension,
    }
    # Provenance of the *text*: which parser produced it (plan §6.1 step 2).
    # Not a filter exposed by ``POST /api/search`` (hence not in KEYWORD_FIELDS),
    # but a keyword field in the index, so ``parser_backend: pypdf`` finds what a
    # backend switch did not reach. NULL means the paper predates the stamp --
    # not the same as "no backend". A metadata refresh deliberately leaves these
    # alone: it must not make a stale parse look fresh.
    document["parser_backend"] = row.get("parser_backend")
    document["parser_version"] = row.get("parser_version")
    # Metadata snapshot: the filter fields of POST /api/search (see mappings.py).
    document["venue_year"] = row.get("venue_year")
    document["paper_type"] = row.get("paper_type")
    document["volume"] = row.get("volume")
    document["issue"] = row.get("issue")
    document["pages"] = row.get("pages")
    document["identifiers"] = _as_string_list(row.get("identifiers"))
    for field in TAG_KIND_FIELDS.values():
        document[field] = _as_string_list(row.get(field))
    publication_date = row.get("publication_date")
    if publication_date is not None:
        document["publication_date"] = (
            publication_date.isoformat()
            if hasattr(publication_date, "isoformat")
            else str(publication_date)
        )
    embedding = row.get("embedding")
    if embedding is not None:
        document["embedding"] = list(embedding)
    created_at = row.get("created_at")
    if created_at is not None:
        document["created_at"] = (
            created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
        )
    return document


def bulk_index_chunks(
    rows: Iterable[dict[str, Any]],
    *,
    client: OpenSearch | None = None,
    index: str = ALIAS,
    batch_size: int = BULK_BATCH_SIZE,
    refresh: bool = True,
) -> dict[str, int]:
    """Index chunks in batches of ``batch_size`` (default 200) with refresh.

    ``rows`` may be raw ``paper_chunks`` rows joined with paper metadata; each
    one carries its own ``embedding`` when vectors should be written.
    """
    client = client or get_client()
    items = list(rows)
    if not items:
        return {"indexed": 0, "failed": 0}

    indexed = 0
    failed = 0
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        actions = [
            {
                "_op_type": "index",
                "_index": index,
                "_id": str(row["chunk_id"]),
                "_source": build_chunk_document(row),
            }
            for row in batch
        ]
        try:
            success, errors = bulk(
                client,
                actions,
                refresh="wait_for" if refresh else False,
                raise_on_error=False,
                stats_only=False,
            )
        except OpenSearchException as exc:  # pragma: no cover - live cluster only
            raise SearchIndexError(f"bulk index failed: {exc}") from exc
        indexed += int(success)
        if errors:
            failed += len(errors)
            logger.warning(
                "bulk index reported errors",
                extra={
                    "extra_fields": {
                        "index": index,
                        "first_error": json.dumps(errors[0])[:300],
                    }
                },
            )
    if refresh:
        client.indices.refresh(index=index)
    logger.info(
        "indexed chunks",
        extra={"extra_fields": {"index": index, "indexed": indexed, "failed": failed}},
    )
    return {"indexed": indexed, "failed": failed}


def delete_by_paper_id(
    paper_id: str,
    *,
    client: OpenSearch | None = None,
    index: str = ALIAS,
    refresh: bool = True,
) -> int:
    """Delete every chunk document of one paper; returns deleted count."""
    client = client or get_client()
    try:
        response = client.delete_by_query(
            index=index,
            body={"query": {"term": {"paper_id": str(paper_id)}}},
            refresh=refresh,
            conflicts="proceed",
        )
    except NotFoundError:
        return 0
    except OpenSearchException as exc:  # pragma: no cover - live cluster only
        raise SearchIndexError(f"delete_by_paper_id failed: {exc}") from exc
    deleted = int(response.get("deleted", 0))
    logger.info(
        "deleted chunks for paper",
        extra={"extra_fields": {"paper_id": str(paper_id), "deleted": deleted}},
    )
    return deleted


def index_stats(
    *, client: OpenSearch | None = None, index: str = ALIAS
) -> dict[str, Any]:
    """Document count for one index/alias (used by scripts and healthchecks)."""
    try:
        response = (client or get_client()).count(index=index)
    except NotFoundError:
        return {"index": index, "count": 0, "exists": False}
    return {"index": index, "count": int(response.get("count", 0)), "exists": True}


def chunk_sequences(rows: Sequence[dict[str, Any]]) -> list[str]:
    """Debug helper: the document ids a bulk call would write."""
    return [str(row["chunk_id"]) for row in rows]


__all__ = [
    "ALIAS",
    "BULK_BATCH_SIZE",
    "INDEX",
    "SearchIndexError",
    "alias_swap_is_safe",
    "alias_targets",
    "build_alias_swap_body",
    "build_reindex_body",
    "build_chunk_document",
    "bulk_index_chunks",
    "bulk_update_documents",
    "delete_by_paper_id",
    "ensure_index",
    "get_client",
    "index_exists",
    "index_stats",
    "update_mapping",
]
