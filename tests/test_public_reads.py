"""The public-read contract (2026-10-12): reads carry no credential, writes do.

Why a static table instead of only HTTP calls: the failure this guards against is
*silent*. A router-level dependency re-added "for consistency" would gate reads
again, and a dropped dependency on a write endpoint would open a mutation surface —
neither shows up in the behaviour of a single endpoint test. So the test reads every
router's routes, computes the gate each one actually declares, and compares the
whole table against the contract.

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

#: Endpoints that must answer without consulting a credential — 12 reads plus
#: `/health` (the signed `/api/downloads/{paper_id}` route was retired on
#: 2026-10-12; every download goes through `GET /api/papers/{paper_id}/file`).
PUBLIC: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/health"),
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

#: Endpoints that must keep a gate: 14 mutations (`write`) and one deletion (`admin`).
GATED: dict[tuple[str, str], str] = {
    ("DELETE", "/api/papers/{paper_id}"): "admin",
    ("POST", "/api/jobs/{job_id}/retry"): "write",
    ("POST", "/api/metadata/apply"): "write",
    ("POST", "/api/metadata/import"): "write",
    ("POST", "/api/metadata/sources/{source_id}/attach"): "write",
    ("POST", "/api/papers/ingest"): "write",
    ("POST", "/api/papers/ingest/compressed"): "write",
    ("POST", "/api/papers/ingest/dir"): "write",
    ("POST", "/api/papers/ingest/file"): "write",
    ("POST", "/api/papers/ingest/files"): "write",
    ("POST", "/api/papers/reindex"): "write",
    ("POST", "/api/papers/{paper_id}/reindex"): "write",
    ("PATCH", "/api/papers/{paper_id}/metadata"): "write",
    ("POST", "/api/papers/{paper_id}/metadata/conflicts/dismiss"): "write",
    ("POST", "/api/papers/{paper_id}/metadata/rollback"): "write",
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


@pytest.fixture(scope="module")
def table() -> dict[tuple[str, str], frozenset[str]]:
    """``(method, path) -> tiers declared by that route`` for every API route."""
    names = {security.require_write: "write", security.require_admin: "admin"}
    table: dict[tuple[str, str], frozenset[str]] = {}
    for module in MODULES:
        for route in module.router.routes:
            calls = {dep.call for dep in route.dependant.dependencies}
            tiers = frozenset(name for call, name in names.items() if call in calls)
            for method in route.methods - {"HEAD", "OPTIONS"}:
                table[(method, route.path)] = tiers
    return table


def test_the_route_table_is_complete(table) -> None:
    """Every endpoint is classified; adding one forces a decision here."""
    assert set(table) == set(PUBLIC) | set(GATED)


def test_reads_declare_no_gate(table) -> None:
    offenders = {key: sorted(table[key]) for key in PUBLIC if table[key]}
    assert offenders == {}


def test_writes_declare_the_expected_gate(table) -> None:
    wrong = {key: sorted(table[key]) for key, want in GATED.items() if table[key] != {want}}
    assert wrong == {}


def test_ingestion_is_a_write_router_throughout() -> None:
    """The five ingest entries are the biggest blast radius; no read slipped in."""
    assert {path for _, path in GATED if path.startswith("/api/papers/ingest")} == {
        "/api/papers/ingest",
        "/api/papers/ingest/compressed",
        "/api/papers/ingest/dir",
        "/api/papers/ingest/file",
        "/api/papers/ingest/files",
    }


def test_the_read_tier_singleton_is_gone() -> None:
    """``require_api_key`` was the read gate; keeping the name invites re-adding it.

    If a caller needs a gate, the module offers ``require_write`` / ``require_admin``
    and ``require_role``; there is deliberately no read-tier singleton any more.
    """
    assert not hasattr(security, "require_api_key")
