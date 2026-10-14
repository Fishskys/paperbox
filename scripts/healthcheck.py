#!/usr/bin/env python3
"""Health check for paperbox and every dependency it talks to.

    uv run python scripts/healthcheck.py

The probes are the same ones ``GET /health`` runs (``app.services.health_service``):
one registry, one set of success criteria, no second implementation to drift. This
script adds what only an operator wants -- the API process itself, the indexed
document count and a search-pipeline drift check -- and turns the verdict into an
exit code: non-zero when a *required* dependency is not answering as expected.
Optional ones (rerank, and docling on a pypdf-only deployment) are reported and
warned about, but do not fail the run.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.services import health_service  # noqa: E402


def api_spec() -> health_service.ProbeSpec:
    """The API process itself -- the one dependency the registry cannot probe.

    Deliberately outside the registry: ``/health`` cannot report on its own
    process, while this script runs in a different one.
    """
    port = settings.paper_api_port
    return health_service.ProbeSpec(
        name="api",
        kind=health_service.KIND_HTTP,
        target=lambda: f"http://127.0.0.1:{port}",
        path="/health",
        ok_status=(200,),
        retries=0,
        detail=f"GET /health on port {port}",
    )


def report(results: Sequence[health_service.ProbeResult]) -> None:
    for result in results:
        latency = f"{result.latency_ms:.0f} ms" if result.latency_ms is not None else "-"
        tag = "" if result.required else " (optional)"
        print(
            f"  {result.status:<8} {result.name:<10} {latency:>9}  "
            f"{result.detail}{tag}"
        )
        if result.reason:
            print(f"           {result.reason}")
        if result.cached:
            print(f"           (cached verdict, measured {result.checked_at})")


def extras() -> None:
    """Deployment-state details a probe cannot express; warn-only."""
    import httpx

    index = settings.opensearch_alias
    try:
        response = httpx.get(
            f"{settings.opensearch_url.rstrip('/')}/{index}/_count", timeout=10
        )
        print(f"  info     {index} documents: {response.json().get('count')}")
    except Exception as exc:  # noqa: BLE001 - report and continue
        print(f"  warn     could not count {index}: {exc}")

    # The native hybrid backend needs its search pipelines in the cluster, and a
    # drifted body would silently change how a hybrid query is fused. Warn-only:
    # the default backend does not read them, so a missing pipeline must not fail
    # a healthcheck of a working deployment.
    try:
        from app.search.native import ensure_pipelines

        rows = ensure_pipelines(dry_run=True)
        drifted = [item["name"] for item in rows if item["changed"]]
        if drifted:
            print(
                f"  warn     search pipelines drifted: {', '.join(drifted)} "
                "(run scripts/ensure_search_pipelines.py)"
            )
        else:
            print(f"  info     search pipelines: {len(rows)} present, in sync")
    except Exception as exc:  # noqa: BLE001
        print(f"  warn     could not check search pipelines: {exc}")


def main(
    specs: Sequence[health_service.ProbeSpec] | None = None,
    *,
    with_extras: bool = True,
) -> int:
    chosen = (
        tuple(specs) if specs is not None else (*health_service.health_specs(), api_spec())
    )
    print("paperbox healthcheck")
    # Always a real probe: the CLI is the operator's deliberate "check it now", so
    # it bypasses the verdict cache the endpoint uses (a fresh process would have an
    # empty cache anyway -- being explicit keeps that true if this ever runs in-process).
    results = asyncio.run(health_service.probe_all(chosen, use_cache=False))
    report(results)

    if with_extras:
        extras()

    degraded = [item for item in results if item.required and not item.healthy]
    if degraded:
        names = ", ".join(f"{item.name}={item.status}" for item in degraded)
        print("RESULT: degraded -", names)
        return 1
    print("RESULT: all required dependencies ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
