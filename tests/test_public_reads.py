"""The four-level route contract: what each endpoint is gated by (plan D2/D8).

Why a static table instead of only HTTP calls: the failure this guards against is
*silent*. A router-level dependency forgotten on a new endpoint opens a mutation
surface, and a read endpoint re-gated "for consistency" closes a read somebody
depends on — neither shows up in the behaviour of a single endpoint test. So the
test reads every router's routes, computes the tier each one actually declares, and
compares the whole table against the contract.

Tiers are monotonic, so "the tier a route needs" is the **highest** one it declares:
the reading routers carry ``require_read`` at router level and their writing
endpoints add ``require_write``, which implies it.

Behavioural coverage of the gates themselves lives in ``tests/test_auth_rest.py``.
"""

from __future__ import annotations

import pytest

from app.api import (
    consistency,
    health,
    ingestion,
    jobs,
    metadata,
    papers,
    search,
    search_logs,
)
from app.core import security

#: Framework/ops endpoints: no gate at all (the read door is not consulted).
PUBLIC: frozenset[tuple[str, str]] = frozenset({("GET", "/health")})

#: The read tier — 12 business reads plus ``/health`` are the 13 reads of the plan;
#: ``/health`` stays ungated, so these 12 are the ones carrying ``require_read``.
#: ``POST /api/search`` belongs here: it writes one search-log row, not corpus state.
READS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/consistency"),
        ("GET", "/api/jobs"),
        ("GET", "/api/jobs/queue"),
        ("GET", "/api/jobs/{job_id}"),
        ("GET", "/api/metadata/review"),
        ("GET", "/api/papers"),
        ("GET", "/api/papers/{paper_id}"),
        ("GET", "/api/papers/{paper_id}/chunks"),
        ("GET", "/api/papers/{paper_id}/degradations"),
        ("GET", "/api/papers/{paper_id}/file"),
        ("GET", "/api/papers/{paper_id}/metadata"),
        ("POST", "/api/search"),
        ("GET", "/api/search-logs"),
    }
)

#: Everything that changes state: 14 mutations and the deletion (both ``write``),
#: plus the one bulk/whole-library reindex (``admin``, plan D8).
WRITES: dict[tuple[str, str], str] = {
    ("DELETE", "/api/papers/{paper_id}"): "write",
    ("POST", "/api/jobs/{job_id}/retry"): "write",
    ("POST", "/api/metadata/apply"): "write",
    ("POST", "/api/metadata/import"): "write",
    ("POST", "/api/metadata/sources/{source_id}/attach"): "write",
    ("POST", "/api/papers/ingest"): "write",
    ("POST", "/api/papers/ingest/compressed"): "write",
    ("POST", "/api/papers/ingest/dir"): "write",
    ("POST", "/api/papers/ingest/file"): "write",
    ("POST", "/api/papers/ingest/files"): "write",
    ("POST", "/api/papers/{paper_id}/reindex"): "write",
    ("PATCH", "/api/papers/{paper_id}/metadata"): "write",
    ("POST", "/api/papers/{paper_id}/metadata/conflicts/dismiss"): "write",
    ("POST", "/api/papers/{paper_id}/metadata/rollback"): "write",
    ("POST", "/api/papers/reindex"): "admin",
}

MODULES = (
    health,
    consistency,
    ingestion,
    jobs,
    papers,
    metadata,
    search,
    search_logs,
)

_ORDER = {"read": 0, "write": 1, "admin": 2}


@pytest.fixture(scope="module")
def table() -> dict[tuple[str, str], frozenset[str]]:
    """``(method, path) -> tiers declared by that route`` for every API route."""
    names = {
        security.require_read: "read",
        security.require_write: "write",
        security.require_admin: "admin",
    }
    table: dict[tuple[str, str], frozenset[str]] = {}
    for module in MODULES:
        for route in module.router.routes:
            calls = {dep.call for dep in route.dependant.dependencies}
            tiers = frozenset(name for call, name in names.items() if call in calls)
            for method in route.methods - {"HEAD", "OPTIONS"}:
                table[(method, route.path)] = tiers
    return table


def _highest(tiers: frozenset[str]) -> str | None:
    """The tier a route actually needs (tiers are monotonic)."""
    return max(tiers, key=lambda tier: _ORDER[tier]) if tiers else None


def test_the_route_table_is_complete(table) -> None:
    """Every endpoint is classified; adding one forces a decision here."""
    assert set(table) == set(PUBLIC) | set(READS) | set(WRITES)


def test_reads_declare_the_read_gate(table) -> None:
    """Reads are gated again — by the tier the policy decides, not by absence."""
    offenders = {key: sorted(table[key]) for key in READS if table[key] != {"read"}}
    assert offenders == {}


def test_writes_reach_the_expected_tier(table) -> None:
    wrong = {
        key: sorted(table[key])
        for key, want in WRITES.items()
        if _highest(table[key]) != want
    }
    assert wrong == {}


def test_no_route_is_ungated(table) -> None:
    """The 2026-10-12 gap: an endpoint with no dependency at all."""
    ungated = sorted(key for key in table if not table[key])
    assert ungated == [("GET", "/health")]


def test_admin_gates_the_bulk_reindex_only(table) -> None:
    """Plan D8: deletion dropped to ``write``; the whole-library rebuild kept admin."""
    assert {key for key in table if _highest(table[key]) == "admin"} == {
        ("POST", "/api/papers/reindex")
    }


def test_ingestion_is_a_write_router_throughout() -> None:
    """The five ingest entries are the biggest blast radius; no read slipped in."""
    assert {path for _, path in WRITES if path.startswith("/api/papers/ingest")} == {
        "/api/papers/ingest",
        "/api/papers/ingest/compressed",
        "/api/papers/ingest/dir",
        "/api/papers/ingest/file",
        "/api/papers/ingest/files",
    }


def test_the_gated_routers_declare_read_at_router_level() -> None:
    """One declaration per reading router, not one per endpoint (plan task T4)."""
    for module in (consistency, jobs, metadata, papers, search, search_logs):
        calls = {dep.dependency for dep in module.router.dependencies}
        assert security.require_read in calls, module.__name__


def test_the_read_tier_singleton_exists_again() -> None:
    """``require_api_key`` (the old, misleading name) stays retired."""
    assert not hasattr(security, "require_api_key")
    assert security.require_read is not None
