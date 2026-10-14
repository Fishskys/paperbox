"""``GET /health`` -- dependency probes (MVP-SPEC section 2).

The probes themselves live in :mod:`app.services.health_service`: one spec per
dependency, each with its own success criteria, credentials, timeout and
``required`` flag. This module is only the protocol layer -- it runs them and
renders the report.

The overall ``status`` still answers for the liveness of the API process itself,
so a single failing dependency shows up inside ``services``/``dependencies``
without turning the endpoint into a 5xx -- use ``degraded`` (or the CLI) to gate
on the dependencies themselves.
"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.services import health_service

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Report API liveness plus the verdict of every dependency probe.

    ``status`` answers for the API process itself. ``degraded`` answers for the
    deployment: it is ``True`` as soon as one *required* dependency is not
    ``ok``, which is what a container healthcheck or an orchestrator should gate
    on. ``services`` is the coarse map (unchanged for existing consumers) and
    ``dependencies`` carries the detail behind each verdict.
    """
    results = await health_service.probe_all()
    return {
        "status": health_service.OK,
        "version": __version__,
        "degraded": health_service.is_degraded(results),
        "services": {result.name: result.status for result in results},
        "dependencies": {
            result.name: health_service.dependency_payload(result) for result in results
        },
    }


__all__ = ["router"]
