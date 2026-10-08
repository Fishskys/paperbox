#!/usr/bin/env python3
"""Create (or verify) a ``paper_chunks`` index and point the alias at it.

Default (no arguments) behaviour is unchanged in spirit: it idempotently
creates the index named by ``OPENSEARCH_INDEX`` in ``.env`` (currently
``paper_chunks_v2``, the CJK-analyzer index) and makes ``paper_chunks_current``
resolve to it::

    uv run python scripts/create_index.py

Migration to a new analyzer version (SPEC-P1 H1) copies the existing documents
server-side, so the stored embeddings are never recomputed::

    uv run python scripts/create_index.py --index paper_chunks_v2 --migrate-from paper_chunks_v1

The migration is idempotent and safe:

1. refuse up front when the old index's ``embedding.dimension`` differs from
   the mapping that would be created (``_reindex`` copies vectors verbatim, so
   a model change means a full re-embed, never a copy);
2. create ``--index`` (idempotent; ``knn_vector`` mappings cannot be changed in
   place, so a new analyzer always means a new index);
3. ``_reindex`` every document from the old index, started with
   ``wait_for_completion=false`` and polled through ``GET _tasks/<id>`` so a
   2883-document copy does not time out;
4. compare document counts - they must match exactly, otherwise the script
   exits non-zero **without** touching the alias;
5. move the alias atomically (remove from old, add to new with
   ``is_write_index``);
6. print the counts on both sides. The old index is kept for rollback; nothing
   is ever deleted here.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.search.mappings import build_mapping  # noqa: E402
from app.search.opensearch import (  # noqa: E402
    ALIAS,
    INDEX,
    alias_swap_is_safe,
    alias_targets,
    build_alias_swap_body,
    build_reindex_body,
    ensure_index,
    get_client,
    index_exists,
    index_stats,
    mapping_embedding_dimension,
)

DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TASK_TIMEOUT = 1800.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--index",
        default=None,
        help=f"physical index to create (default={INDEX} if set, else OPENSEARCH_INDEX from .env)",
    )
    parser.add_argument(
        "--alias", default=None, help=f"read/write alias (default {ALIAS})"
    )
    parser.add_argument(
        "--migrate-from",
        default=None,
        help="old index to copy documents from, then swap the alias onto --index",
    )
    parser.add_argument(
        "--poll-interval",
        default=DEFAULT_POLL_INTERVAL,
        type=float,
        help="task poll spacing in seconds",
    )
    parser.add_argument(
        "--task-timeout",
        default=DEFAULT_TASK_TIMEOUT,
        type=float,
        help="how long to wait for the _reindex task",
    )
    return parser.parse_args(argv)


def verify(client, *, index: str, alias: str) -> int:
    """Default behaviour: ensure the index exists and report on it."""
    report = ensure_index(client, index=index, alias=alias)
    exists = index_exists(client, index)
    targets = alias_targets(client, alias)
    mapping = client.indices.get_mapping(index=index)
    properties = next(iter(mapping.values()))["mappings"]["properties"]
    print(
        json.dumps(
            {
                "index": report["index"],
                "exists": exists,
                "created_now": report["created"],
                "alias": alias,
                "alias_targets": targets,
                "alias_updated": report["alias_updated"],
                "embedding_field": properties.get("embedding"),
                "text_analyzers": {
                    name: (properties.get(name) or {}).get("analyzer")
                    for name in ("title", "text", "section_title")
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if exists and index in targets else 1


def wait_for_task(
    client,
    task_id: str,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_TASK_TIMEOUT,
) -> dict:
    """Poll ``GET _tasks/<id>`` until the reindex task completes."""
    started = time.monotonic()
    last: dict = {}
    while True:
        last = client.tasks.get(task_id=task_id)
        if last.get("completed"):
            return last
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            raise TimeoutError(f"reindex task {task_id} still running after {timeout:.0f}s")
        status = last.get("task", {}).get("status", {})
        print(
            f"  reindex running: {status.get('created', 0)}/{status.get('total', 0)} docs "
            f"({elapsed:.0f}s)"
        )
        time.sleep(poll_interval)


def migrate(
    client,
    *,
    old_index: str,
    new_index: str,
    alias: str,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_TASK_TIMEOUT,
) -> int:
    """Copy ``old_index`` -> ``new_index`` and move the alias over."""
    if old_index == new_index:
        print("--migrate-from must differ from --index", file=sys.stderr)
        return 2
    if not index_exists(client, old_index):
        print(f"source index {old_index!r} does not exist", file=sys.stderr)
        return 2

    # Dimensions must agree before anything is created: _reindex copies vectors
    # verbatim, so a copy across dimensions either fails mid-flight or, if it
    # did not, would fill a fresh index with vectors from the wrong model. The
    # honest path for a new embedding model is a full re-embed, not a copy.
    old_dim = mapping_embedding_dimension(client.indices.get_mapping(index=old_index))
    new_dim = mapping_embedding_dimension(build_mapping())
    if old_dim is not None and new_dim is not None and old_dim != new_dim:
        print(
            f"ABORT: {old_index} carries {old_dim}-dim embeddings but {new_index} "
            f"would be built for {new_dim} (EMBEDDING_DIMENSION). Server-side "
            "_reindex copies vectors verbatim and cannot cross dimensions: "
            "vectors from two models must never share one index. Re-embed the "
            "whole library instead -- scripts/reindex.py (per paper: "
            "POST /api/papers/{id}/reindex) -- after pointing OPENSEARCH_INDEX "
            "at the new index.",
            file=sys.stderr,
        )
        return 2

    report = ensure_index(client, index=new_index, alias=None)
    targets = alias_targets(client, alias)

    old_count = index_stats(client=client, index=old_index)["count"]
    new_count = index_stats(client=client, index=new_index)["count"]
    print(f"{old_index}: {old_count} docs")
    print(f"{new_index}: {new_count} docs (created_now={report['created']})")

    if new_index in targets and not [name for name in targets if name != new_index]:
        print(f"alias {alias!r} already points at {new_index!r}; nothing to do")
        print(
            json.dumps(
                {"alias": alias, "alias_targets": targets, "count": new_count}, indent=2
            )
        )
        return 0

    if new_count == old_count and old_count > 0:
        print("document counts already match; skipping the _reindex copy")
    else:
        print(f"reindexing {old_index} -> {new_index} (embeddings are copied verbatim)")
        response = client.reindex(
            body=build_reindex_body(old_index, new_index),
            wait_for_completion=False,
            refresh=True,
        )
        task_id = response.get("task")
        if not task_id:
            print(f"unexpected _reindex response: {response}", file=sys.stderr)
            return 1
        print(f"  task: {task_id}")
        task = wait_for_task(
            client, task_id, poll_interval=poll_interval, timeout=timeout
        )
        status = task.get("task", {}).get("status", {})
        failures = task.get("response", {}).get("failures") or []
        print(
            f"  reindex finished: created={status.get('created', 0)} "
            f"total={status.get('total', 0)} failures={len(failures)}"
        )
        if failures:
            print(f"  first failure: {json.dumps(failures[0])[:300]}", file=sys.stderr)
            return 1

    # Refresh so the counts below are exact, then gate the swap on equality.
    for name in (old_index, new_index):
        try:
            client.indices.refresh(index=name)
        except Exception as exc:  # noqa: BLE001 - refresh is best effort
            print(f"  warn: refresh {name} failed ({type(exc).__name__}: {exc})")

    old_count = index_stats(client=client, index=old_index)["count"]
    new_count = index_stats(client=client, index=new_index)["count"]
    print(f"counts before swap -> {old_index}: {old_count}  {new_index}: {new_count}")

    if not alias_swap_is_safe(old_count, new_count):
        print(
            f"ABORT: document counts differ ({old_index}={old_count}, "
            f"{new_index}={new_count}); alias {alias!r} left untouched",
            file=sys.stderr,
        )
        return 1

    body = build_alias_swap_body(
        targets[0] if targets else "", new_index, alias
    )
    client.indices.update_aliases(body=body)
    after = alias_targets(client, alias)
    print(
        json.dumps(
            {
                "alias": alias,
                "alias_targets_before": targets,
                "alias_targets_after": after,
                "old_index": old_index,
                "old_count": old_count,
                "new_index": new_index,
                "new_count": new_count,
                "old_index_kept_for_rollback": True,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if new_index in after else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    index = args.index or settings.opensearch_index
    alias = args.alias or ALIAS
    client = get_client()
    print(f"OpenSearch: {settings.opensearch_url}")

    if args.migrate_from:
        return migrate(
            client,
            old_index=args.migrate_from,
            new_index=index,
            alias=alias,
            poll_interval=args.poll_interval,
            timeout=args.task_timeout,
        )
    return verify(client, index=index, alias=alias)


if __name__ == "__main__":
    raise SystemExit(main())
