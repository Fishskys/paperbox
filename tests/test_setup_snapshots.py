"""``scripts/setup_snapshots.py`` - repository/policy bodies and their side effects.

The cluster had no backup at all until 2026-09-30 (no repository, no policy), so
two properties matter more than the happy path: (1) re-running the script must not
churn or duplicate anything, and (2) a restore drill must clean up after itself
even when the counts disagree. Both are pinned here with a fake transport; the
suite never touches a real OpenSearch.
"""

from __future__ import annotations

import json

import pytest

from scripts import setup_snapshots as ss


class FakeTransport:
    """Records every ``perform_request`` and replays canned responses."""

    def __init__(self, responses: dict[str, object] | None = None, count: int = 7) -> None:
        self.responses = responses or {}
        self.count = count
        self.calls: list[tuple[str, str, dict, dict | None]] = []

    def perform_request(self, method, url, params=None, body=None, **_: object):  # noqa: ANN001
        self.calls.append((method, url, dict(params or {}), body))
        key = f"{method} {url}"
        if key in self.responses:
            value = self.responses[key]
            if isinstance(value, Exception):
                raise value
            return value
        if url.endswith("/_count"):
            return {"count": self.count}
        if url.startswith("/_cluster/health/"):
            return {"status": "green"}
        if method in {"PUT", "POST"}:
            # Writes (repository / policy / snapshot / restore) are recorded above;
            # a bare acknowledgement is all the callers look at.
            return {"acknowledged": True}
        raise AssertionError(f"unexpected request: {key}")

    def methods(self) -> list[str]:
        return [method for method, *_ in self.calls]


class FakeClient:
    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport


def _client(responses: dict[str, object] | None = None, count: int = 7) -> tuple[FakeClient, FakeTransport]:
    transport = FakeTransport(responses, count=count)
    return FakeClient(transport), transport


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def test_repository_body_is_a_filesystem_repository_at_the_mounted_path() -> None:
    body = ss.build_repository_body()
    assert body["type"] == "fs"
    assert body["settings"]["location"] == ss.LOCATION
    assert body["settings"]["compress"] is True


def test_policy_body_covers_the_chunk_index_and_the_srw_objects() -> None:
    body = ss.build_policy_body()
    config = body["snapshot_config"]
    assert config["repository"] == ss.REPO
    assert "paper_chunks_*" in config["indices"]
    assert "search-relevance-*" in config["indices"]
    # A partial snapshot is a backup nobody can trust; fail loudly instead.
    assert config["partial"] is False
    # Cluster state carries the index templates the chunk documents depend on.
    assert config["include_global_state"] is True


def test_policy_body_carries_schedule_and_retention() -> None:
    body = ss.build_policy_body()
    assert body["creation"]["schedule"]["cron"]["expression"] == ss.CREATION_CRON
    assert body["creation"]["schedule"]["cron"]["timezone"] == ss.TIMEZONE
    condition = body["deletion"]["condition"]
    assert condition["max_age"] == ss.DELETE_MAX_AGE
    assert condition["max_count"] == ss.DELETE_MAX_COUNT
    assert condition["min_count"] <= condition["max_count"]
    assert json.dumps(body)  # the SM API takes JSON; no tuples/dates may sneak in


def test_restore_body_renames_the_index_and_drops_the_alias() -> None:
    body = ss.build_restore_body(source_index="paper_chunks_v3", target_index=ss.RESTORE_TEST_INDEX)
    assert body["rename_pattern"] == "paper_chunks_v3"
    assert body["rename_replacement"] == ss.RESTORE_TEST_INDEX
    assert body["include_aliases"] is False  # must not steal paper_chunks_current
    assert body["include_global_state"] is False


def test_baseline_name_is_prefixed_exactly_once() -> None:
    assert ss.baseline_name("20260930") == "paperbox-baseline-20260930"
    assert ss.baseline_name("paperbox-baseline-20260930") == "paperbox-baseline-20260930"


def test_newest_snapshot_ignores_other_policies_and_missing_timestamps() -> None:
    snapshots = [
        {"snapshot": "paperbox-daily-1", "start_time_in_millis": 100},
        {"snapshot": "paperbox-daily-2", "start_time_in_millis": 300},
        {"snapshot": "other-9", "start_time_in_millis": 900},
        {"snapshot": "paperbox-daily-3"},
    ]
    assert ss.newest_snapshot(snapshots)["snapshot"] == "other-9"
    assert ss.newest_snapshot(snapshots, prefix="paperbox-daily-")["snapshot"] == "paperbox-daily-2"
    assert ss.newest_snapshot([], prefix="paperbox-daily-") is None


def test_count_mismatch_report_says_match_or_mismatch() -> None:
    assert "MATCH" in ss.count_mismatch_report(2302, 2302)
    assert "MISMATCH" in ss.count_mismatch_report(2302, 2301)


# --------------------------------------------------------------------------- #
# repository: idempotency
# --------------------------------------------------------------------------- #
def test_ensure_repository_creates_when_missing() -> None:
    client, transport = _client({"GET /_snapshot/paperbox_backup": RuntimeError("missing")})
    assert ss.ensure_repository(client) == "created"
    assert ("PUT", "/_snapshot/paperbox_backup") in [(m, u) for m, u, *_ in transport.calls]


def test_ensure_repository_is_unchanged_for_the_same_location() -> None:
    client, transport = _client(
        {"GET /_snapshot/paperbox_backup": {ss.REPO: {"settings": {"location": ss.LOCATION}}}}
    )
    assert ss.ensure_repository(client) == "unchanged"
    assert transport.methods() == ["GET"]  # no write at all


def test_ensure_repository_reregisters_when_the_location_changes() -> None:
    client, transport = _client(
        {"GET /_snapshot/paperbox_backup": {ss.REPO: {"settings": {"location": "/mnt/old"}}}}
    )
    assert ss.ensure_repository(client, location="/mnt/new") == "recreated"
    put = [call for call in transport.calls if call[0] == "PUT"][0]
    assert put[3]["settings"]["location"] == "/mnt/new"


def test_ensure_repository_dry_run_writes_nothing() -> None:
    client, transport = _client({"GET /_snapshot/paperbox_backup": RuntimeError("missing")})
    assert ss.ensure_repository(client, dry_run=True) == "created"
    assert transport.methods() == ["GET"]


# --------------------------------------------------------------------------- #
# policy: create / update / leave alone
# --------------------------------------------------------------------------- #
def _raw_policy(**overrides):
    return {"_seq_no": 12, "_primary_term": 3, "sm_policy": {"name": ss.POLICY, **overrides}}


def test_upsert_policy_creates_when_absent() -> None:
    client, transport = _client({"GET /_plugins/_sm/policies/paperbox-daily": RuntimeError("404")})
    assert ss.upsert_policy(client) == "created"
    assert [m for m, *_ in transport.calls] == ["GET", "POST"]


def test_upsert_policy_updates_in_place_with_the_current_seq_no() -> None:
    wanted = ss.build_policy_body()
    client, transport = _client(
        {"GET /_plugins/_sm/policies/paperbox-daily": _raw_policy(creation={"schedule": {"cron": {"expression": "0 0 * * *"}}})}
    )
    assert ss.upsert_policy(client, body=wanted) == "updated"
    put = [call for call in transport.calls if call[0] == "PUT"][0]
    assert put[2] == {"if_seq_no": 12, "if_primary_term": 3}
    assert put[3]["snapshot_config"]["repository"] == ss.REPO


def test_upsert_policy_leaves_an_identical_policy_alone() -> None:
    wanted = ss.build_policy_body()
    client, transport = _client(
        {
            "GET /_plugins/_sm/policies/paperbox-daily": _raw_policy(
                creation=wanted["creation"],
                deletion=wanted["deletion"],
                snapshot_config=wanted["snapshot_config"],
            )
        }
    )
    assert ss.upsert_policy(client, body=wanted) == "unchanged"
    assert transport.methods() == ["GET"]


def test_upsert_policy_dry_run_reports_without_writing() -> None:
    client, transport = _client({"GET /_plugins/_sm/policies/paperbox-daily": RuntimeError("404")})
    assert ss.upsert_policy(client, dry_run=True) == "created"
    assert transport.methods() == ["GET"]


# --------------------------------------------------------------------------- #
# restore drill
# --------------------------------------------------------------------------- #
def test_restore_check_compares_counts_and_deletes_the_temp_index() -> None:
    client, transport = _client(
        {
            "GET /_snapshot/paperbox_backup/paperbox-baseline-20260930": {
                "snapshots": [{"snapshot": "paperbox-baseline-20260930", "state": "SUCCESS"}]
            },
            "POST /_snapshot/paperbox_backup/paperbox-baseline-20260930/_restore": {"accepted": True},
        },
        count=2302,
    )
    assert ss.restore_check(client, "paperbox_backup", "paperbox-baseline-20260930", source_index="paper_chunks_v3") is True
    assert ("DELETE", f"/{ss.RESTORE_TEST_INDEX}") in [(m, u) for m, u, *_ in transport.calls]


def test_restore_check_fails_but_still_cleans_up_on_a_mismatch() -> None:
    class Skewed(FakeTransport):
        def perform_request(self, method, url, params=None, body=None, **kwargs):  # noqa: ANN001
            if url == "/paper_chunks_restore_test/_count":
                self.calls.append((method, url, dict(params or {}), body))
                return {"count": 1}
            return super().perform_request(method, url, params=params, body=body, **kwargs)

    transport = Skewed(
        {
            "GET /_snapshot/paperbox_backup/baseline": {"snapshots": [{"snapshot": "baseline", "state": "SUCCESS"}]},
            "POST /_snapshot/paperbox_backup/baseline/_restore": {"accepted": True},
        },
        count=2302,
    )
    assert ss.restore_check(FakeClient(transport), "paperbox_backup", "baseline", source_index="paper_chunks_v3") is False
    assert ("DELETE", f"/{ss.RESTORE_TEST_INDEX}") in [(m, u) for m, u, *_ in transport.calls]


@pytest.mark.parametrize("state", ["PARTIAL", "FAILED", "IN_PROGRESS"])
def test_restore_check_refuses_a_snapshot_that_is_not_successful(state: str) -> None:
    client, transport = _client(
        {"GET /_snapshot/paperbox_backup/baseline": {"snapshots": [{"snapshot": "baseline", "state": state}]}}
    )
    assert ss.restore_check(client, "paperbox_backup", "baseline") is False
    assert transport.methods() == ["GET"]  # never restored, nothing to clean up


def test_restore_check_reports_a_missing_snapshot() -> None:
    client, transport = _client({"GET /_snapshot/paperbox_backup/nope": RuntimeError("missing")})
    assert ss.restore_check(client, "paperbox_backup", "nope") is False
    assert transport.methods() == ["GET"]


# --------------------------------------------------------------------------- #
# CLI defaults
# --------------------------------------------------------------------------- #
def test_cli_defaults_match_the_module_constants() -> None:
    args = ss.parse_args([])
    assert args.repo == ss.REPO
    assert args.policy == ss.POLICY
    assert args.location == ss.LOCATION
    assert args.indices == ss.SNAPSHOT_INDICES
    assert args.cron == ss.CREATION_CRON and args.timezone == ss.TIMEZONE
    assert not args.list and not args.baseline and not args.restore_check and not args.dry_run


def test_cli_lets_a_snapshot_be_triggered_soon_for_testing() -> None:
    args = ss.parse_args(["--cron", "* * * * *"])
    assert ss.build_policy_body(cron=args.cron)["creation"]["schedule"]["cron"]["expression"] == "* * * * *"
