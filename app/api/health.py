"""``GET /health`` -- dependency probes (MVP-SPEC section 2).

The probes themselves live in :mod:`app.services.health_service`: one spec per
dependency, each with its own success criteria, credentials, timeout and
``required`` flag. This module is only the protocol layer -- it runs them and
renders the report.

The overall ``status`` still answers for the liveness of the API process itself,
so a single failing dependency shows up inside ``services``/``dependencies``
without turning the endpoint into a 5xx.
"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.services import health_service

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Report API liveness plus the verdict of every dependency probe."""
    results = await health_service.probe_all()
    return {
        "status": health_service.OK,
        "version": __version__,
        "services": {result.name: result.status for result in results},
    }


__all__ = ["router"]
