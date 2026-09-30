#!/usr/bin/env python3
"""Set up the OpenSearch snapshot repository and the scheduled snapshot policy.

    uv run python scripts/setup_snapshots.py                  # 建/核对仓库 + 建/更新策略（幂等）
    uv run python scripts/setup_snapshots.py --list           # 只读：仓库、快照、策略状态
    uv run python scripts/setup_snapshots.py --baseline 20260930
    uv run python scripts/setup_snapshots.py --restore-check paperbox-daily-2026-10-01...
    uv run python scripts/setup_snapshots.py --dry-run

Before 2026-09-30 the cluster had **no backups at all**: no snapshot repository, no
policy, nothing. The only "backup" of the index was the 30 frozen PDFs plus a
re-import (about an hour). This script closes that gap:

1. ``PUT _snapshot/<repo>`` - a filesystem repository. It needs the container to be
   started with ``-Epath.repo=<location>`` **and** that path bind-mounted; both live
   in ``infra/docker-compose.yml`` (``OPENSEARCH_BACKUP_DIR``). ``path.repo`` is not
   editable at runtime: it only appears in ``GET _nodes/settings`` after the
   container is *recreated* (``docker compose up -d --force-recreate opensearch``).
2. an SM (snapshot management) policy - daily snapshots of the chunk index(es) and
   the search-relevance objects, keeping ``max_count`` copies / ``max_age`` days.

``--baseline`` takes a manual snapshot (never garbage-collected, since the policy
only deletes its own ``<policy>-*`` names) and ``--restore-check`` proves the
backup is actually restorable: it restores the live chunk index into a *temporary*
index under a new name, compares ``_count``, and deletes the temporary index again
- including when the comparison fails. Nothing here ever deletes a real index.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.search.opensearch import INDEX, get_client  # noqa: E402

#: Repository name; also what the SM policy points at.
REPO = "paperbox_backup"
#: In-container location. Must match ``-Epath.repo`` in infra/docker-compose.yml.
LOCATION = "/mnt/backups"
#: SM policy name. SM stores it as ``<name>-sm-policy`` and the name is immutable.
POLICY = "paperbox-daily"
#: What a scheduled snapshot covers. ``paper_chunks_*`` follows the physical index
#: across renames; the search-relevance objects carry the M4 (SRW) query sets.
SNAPSHOT_INDICES = "paper_chunks_*,search-relevance-*"
#: 03:30 local time - after the nightly import window, before a working day.
CREATION_CRON = "30 3 * * *"
TIMEZONE = "Asia/Shanghai"
TIME_LIMIT = "1h"
DELETE_MAX_AGE = "30d"
DELETE_MAX_COUNT = 14
DELETE_MIN_COUNT = 3
#: Manual snapshots use this prefix; they are deliberately outside the policy's
#: deletion scope so a known-good baseline survives.
BASELINE_PREFIX = "paperbox-baseline-"
#: Where ``--restore-check`` puts its throwaway copy.
RESTORE_TEST_INDEX = "paper_chunks_restore_test"
RESTORE_TIMEOUT = 120.0


# --------------------------------------------------------------------------- #
# pure builders (no client, unit-tested)
# --------------------------------------------------------------------------- #
def build_repository_body(
    location: str = LOCATION, *, compress: bool = True, chunk_size: str = "100mb"
) -> dict[str, Any]:
    """Body for ``PUT _snapshot/<repo>``; ``fs`` keeps the backup on a plain path."""
    return {
        "type": "fs",
        "settings": {
            "location": location,
            "compress": compress,
            "chunk_size": chunk_size,
        },
    }


def build_policy_body(
    *,
    repository: str = REPO,
    indices: str = SNAPSHOT_INDICES,
    cron: str = CREATION_CRON,
    timezone: str = TIMEZONE,
    time_limit: str = TIME_LIMIT,
    max_age: str = DELETE_MAX_AGE,
    max_count: int = DELETE_MAX_COUNT,
    min_count: int = DELETE_MIN_COUNT,
) -> dict[str, Any]:
    """Body for ``POST/PUT _plugins/_sm/policies/<policy>``.

    ``partial=false`` on purpose: a red/unassigned index must fail the snapshot
    loudly instead of producing a backup nobody can restore.
    """
    return {
        "description": "paperbox: daily chunk-index snapshot (chunk index + SRW objects)",
        "enabled": True,
        "creation": {"schedule": {"cron": {"expression": cron, "timezone": timezone}}, "time_limit": time_limit},
        "deletion": {
            "schedule": {"cron": {"expression": cron, "timezone": timezone}},
            "condition": {"max_age": max_age, "max_count": max_count, "min_count": min_count},
            "time_limit": time_limit,
        },
        "snapshot_config": {
            "repository": repository,
            "indices": indices,
            "ignore_unavailable": False,
            "include_global_state": True,
            "partial": False,
            "date_format": "yyyy-MM-dd-HHmmss",
            "date_format_timezone": "UTC",
        },
    }


def build_snapshot_body(*, indices: str = "*", include_global_state: bool = True) -> dict[str, Any]:
    """Body for a manual ``PUT _snapshot/<repo>/<name>`` (the ``--baseline`` path)."""
    return {
        "indices": indices,
        "ignore_unavailable": True,
        "include_global_state": include_global_state,
        "expand_wildcards": "all",
    }


def build_restore_body(*, source_index: str, target_index: str) -> dict[str, Any]:
    """Body for ``POST _snapshot/<repo>/<snap>/_restore``.

    The live index is open, so the copy needs a different name; aliases are left
    out so the restore cannot steal ``paper_chunks_current`` from the live index.
    """
    return {
        "indices": source_index,
        "rename_pattern": source_index,
        "rename_replacement": target_index,
        "include_global_state": False,
        "include_aliases": False,
        "ignore_unavailable": False,
    }


def baseline_name(tag: str) -> str:
    """``20260930`` -> ``paperbox-baseline-20260930`` (idempotent re-runs collide)."""
    tag = tag.strip()
    return tag if tag.startswith(BASELINE_PREFIX) else f"{BASELINE_PREFIX}{tag}"


def newest_snapshot(snapshots: list[dict[str, Any]], prefix: str = "") -> dict[str, Any] | None:
    """Most recent snapshot by ``start_time_in_millis``, optionally name-prefixed."""
    candidates = [s for s in snapshots if not prefix or str(s.get("snapshot", "")).startswith(prefix)]
    if not candidates:
        return None
    return max(candidates, key=lambda s: int(s.get("start_time_in_millis") or 0))


def count_mismatch_report(source: int, restored: int) -> str:
    return (
        f"restore check: source={source} restored={restored} "
        f"{'MATCH' if source == restored else 'MISMATCH'}"
    )


# --------------------------------------------------------------------------- #
# repository
# --------------------------------------------------------------------------- #
def get_repository(client: Any, repo: str = REPO) -> dict[str, Any] | None:
    try:
        body = client.transport.perform_request("GET", f"/_snapshot/{repo}")
    except Exception:  # noqa: BLE001 - repository_missing_exception when absent
        return None
    return (body or {}).get(repo)


def ensure_repository(
    client: Any, repo: str = REPO, location: str = LOCATION, *, dry_run: bool = False
) -> str:
    """Create the repository, or report ``unchanged`` when it already matches.

    Returns one of ``created`` / ``recreated`` / ``unchanged``; a location change
    is a different backup destination, so the repository is re-registered (the old
    files are left on disk - this never deletes snapshots).
    """
    existing = get_repository(client, repo)
    if existing is not None and existing.get("settings", {}).get("location") == location:
        return "unchanged"
    if dry_run:
        return "created" if existing is None else "recreated"
    client.transport.perform_request("PUT", f"/_snapshot/{repo}", body=build_repository_body(location))
    return "created" if existing is None else "recreated"


def verify_repository(client: Any, repo: str = REPO) -> dict[str, Any]:
    """``POST _verify``: the node list proves every node can write to the repo."""
    body = client.transport.perform_request("POST", f"/_snapshot/{repo}/_verify") or {}
    return {"nodes": body.get("nodes") or {}}


def list_snapshots(client: Any, repo: str = REPO) -> list[dict[str, Any]]:
    try:
        body = client.transport.perform_request("GET", f"/_snapshot/{repo}/_all") or {}
    except Exception:  # noqa: BLE001 - missing repository
        return []
    return list(body.get("snapshots") or [])


def take_snapshot(
    client: Any,
    repo: str = REPO,
    name: str = "",
    *,
    indices: str = "*",
    include_global_state: bool = True,
) -> dict[str, Any]:
    body = client.transport.perform_request(
        "PUT",
        f"/_snapshot/{repo}/{name}",
        params={"wait_for_completion": "true"},
        body=build_snapshot_body(indices=indices, include_global_state=include_global_state),
    )
    return (body or {}).get("snapshot", {})


def get_snapshot(client: Any, repo: str, name: str) -> dict[str, Any] | None:
    try:
        body = client.transport.perform_request("GET", f"/_snapshot/{repo}/{name}") or {}
    except Exception:  # noqa: BLE001
        return None
    snapshots = body.get("snapshots") or []
    return snapshots[0] if snapshots else None


def restore_check(
    client: Any,
    repo: str = REPO,
    snapshot: str = "",
    *,
    source_index: str = INDEX,
    target_index: str = RESTORE_TEST_INDEX,
) -> bool:
    """Restore ``source_index`` from ``snapshot`` under a temporary name and compare.

    The temporary index is deleted on every path (success, mismatch, exception):
    a drill that leaves junk behind is worse than no drill.
    """
    meta = get_snapshot(client, repo, snapshot)
    if meta is None:
        print(f"快照不存在: {repo}/{snapshot}")
        return False
    if meta.get("state") != "SUCCESS":
        print(f"快照状态不是 SUCCESS: {meta.get('state')}")
        return False

    try:
        client.transport.perform_request(
            "POST",
            f"/_snapshot/{repo}/{snapshot}/_restore",
            params={"wait_for_completion": "true"},
            body=build_restore_body(source_index=source_index, target_index=target_index),
        )
        deadline = time.monotonic() + RESTORE_TIMEOUT
        while time.monotonic() < deadline:
            try:
                health = client.transport.perform_request("GET", f"/_cluster/health/{target_index}") or {}
            except Exception:  # noqa: BLE001 - index not visible yet
                health = {}
            if health.get("status") in {"green", "yellow"}:
                break
            time.sleep(2)
        source = int(client.transport.perform_request("GET", f"/{source_index}/_count")["count"])
        restored = int(client.transport.perform_request("GET", f"/{target_index}/_count")["count"])
        print(count_mismatch_report(source, restored))
        return source == restored
    finally:
        try:
            client.transport.perform_request("DELETE", f"/{target_index}")
            print(f"已清理临时索引 {target_index}")
        except Exception:  # noqa: BLE001 - nothing to clean up
            pass


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #
def get_policy(client: Any, name: str = POLICY) -> dict[str, Any] | None:
    try:
        body = client.transport.perform_request("GET", f"/_plugins/_sm/policies/{name}") or {}
    except Exception:  # noqa: BLE001 - 404 when the policy does not exist yet
        return None
    return body.get("sm_policy") or None


def upsert_policy(client: Any, name: str = POLICY, body: dict[str, Any] | None = None, *, dry_run: bool = False) -> str:
    """Create the SM policy, or update it in place using its seq_no/primary_term.

    Returns ``created`` / ``updated`` / ``unchanged``. ``unchanged`` compares the
    parts we own (schedule, retention, snapshot config) so re-running the script
    on an unchanged config does not churn the policy.
    """
    body = body or build_policy_body()
    existing = get_policy(client, name)
    if existing is not None and _policy_matches(existing, body):
        return "unchanged"
    if dry_run:
        return "created" if existing is None else "updated"
    if existing is None:
        client.transport.perform_request("POST", f"/_plugins/_sm/policies/{name}", body=body)
        return "created"
    params = {}
    try:
        raw = client.transport.perform_request("GET", f"/_plugins/_sm/policies/{name}") or {}
        if raw.get("_seq_no") is not None:
            params = {"if_seq_no": raw["_seq_no"], "if_primary_term": raw["_primary_term"]}
    except Exception:  # noqa: BLE001 - fall back to an unconditional PUT
        params = {}
    client.transport.perform_request("PUT", f"/_plugins/_sm/policies/{name}", params=params, body=body)
    return "updated"


def _policy_matches(existing: dict[str, Any], wanted: dict[str, Any]) -> bool:
    for key in ("creation", "deletion", "snapshot_config"):
        if (existing.get(key) or {}) != (wanted.get(key) or {}):
            return False
    return True


def explain_policy(client: Any, name: str = POLICY) -> dict[str, Any]:
    try:
        return client.transport.perform_request("GET", f"/_plugins/_sm/policies/{name}/_explain") or {}
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=REPO, help=f"snapshot repository name (default: {REPO})")
    parser.add_argument("--policy", default=POLICY, help=f"SM policy name (default: {POLICY})")
    parser.add_argument("--indices", default=SNAPSHOT_INDICES, help="indices a scheduled snapshot covers")
    parser.add_argument("--location", default=LOCATION, help="in-container repository path (must equal -Epath.repo)")
    parser.add_argument("--cron", default=CREATION_CRON, help=f"creation cron, UTC-free local expression (default: {CREATION_CRON!r})")
    parser.add_argument("--timezone", default=TIMEZONE, help=f"cron time zone (default: {TIMEZONE})")
    parser.add_argument("--list", action="store_true", help="read-only: repository, snapshots and policy state")
    parser.add_argument("--baseline", metavar="TAG", help="take a manual baseline snapshot, e.g. --baseline 20260930")
    parser.add_argument("--restore-check", metavar="SNAPSHOT", help="restore SNAPSHOT into a temp index and compare counts")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, touch nothing")
    return parser.parse_args(argv)


def _print_snapshots(snapshots: list[dict[str, Any]]) -> None:
    if not snapshots:
        print("快照: （空）")
        return
    print(f"快照（{len(snapshots)}）:")
    for snap in sorted(snapshots, key=lambda s: s.get("start_time") or "", reverse=True):
        shards = snap.get("shards") or {}
        print(
            f"  - {snap.get('snapshot')}  state={snap.get('state')} "
            f"indices={len(snap.get('indices') or [])} "
            f"shards={shards.get('successful')}/{shards.get('total')} "
            f"started={snap.get('start_time')}"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = get_client()
    ok = True

    try:
        repository = get_repository(client, args.repo)
    except Exception as exc:  # noqa: BLE001 - cluster unreachable
        print(f"OpenSearch 不可达: {exc}")
        return 2

    if args.list:
        print(f"仓库 {args.repo}: " + (json.dumps(repository, ensure_ascii=False) if repository else "（未注册）"))
        _print_snapshots(list_snapshots(client, args.repo))
        policy = get_policy(client, args.policy)
        print("策略: " + (json.dumps(policy, ensure_ascii=False) if policy else "（未创建）"))
        return 0 if repository else 1

    state = ensure_repository(client, args.repo, args.location, dry_run=args.dry_run)
    print(f"仓库 {args.repo} @ {args.location}: {state}")
    if not args.dry_run:
        verified = verify_repository(client, args.repo)
        nodes = verified.get("nodes") if isinstance(verified.get("nodes"), dict) else verified
        print(f"仓库校验: {nodes}")
        ok = ok and bool(nodes)

    body = build_policy_body(
        repository=args.repo, indices=args.indices, cron=args.cron, timezone=args.timezone
    )
    policy_state = upsert_policy(client, args.policy, body, dry_run=args.dry_run)
    print(f"策略 {args.policy}: {policy_state}")
    if not args.dry_run:
        explain = explain_policy(client, args.policy)
        print("策略状态: " + json.dumps(explain.get("policies") or explain, ensure_ascii=False)[:400])

    if args.baseline:
        name = baseline_name(args.baseline)
        snap = take_snapshot(client, args.repo, name, indices="*", include_global_state=True)
        shards = snap.get("shards") or {}
        print(
            f"基线快照 {name}: state={snap.get('state')} "
            f"shards={shards.get('successful')}/{shards.get('total')} "
            f"indices={len(snap.get('indices') or [])}"
        )
        ok = ok and snap.get("state") == "SUCCESS" and not shards.get("failed")

    if args.restore_check:
        ok = restore_check(client, args.repo, args.restore_check) and ok

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
