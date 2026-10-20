"""``GET /api/consistency`` -- three-way drift report (PostgreSQL / MinIO / OpenSearch).

Read-only: see ``app/services/consistency_service.py`` for what is compared and
why the endpoint never raises when a store is unreachable (that store is reported
in ``errors`` and the other two are still answered). ``scripts/check_consistency.py``
serves the same report on the command line.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.core.security import require_read

from app.schemas.consistency import ConsistencyOut, IdentifierMirrorOut
from app.services import consistency_service, identifier_mirror_audit

# Read door (plan D2): the credential is checked per AUTH_READ_ACCESS; an open
# door answers a caller without one as the anonymous reader.
router = APIRouter(
    prefix="/api/consistency",
    tags=["consistency"],
    dependencies=[Depends(require_read)],
)


def get_checker() -> Callable[..., Any]:
    """The check itself, as a dependency so tests can swap it out.

    Unit tests run against in-memory SQLite and fake stores, so they must be able
    to replace the checker (the default one opens the real PostgreSQL, MinIO and
    OpenSearch connections).
    """
    return consistency_service.check_consistency


@router.get("", response_model=ConsistencyOut)
def get_consistency(
    limit: int = Query(
        consistency_service.DEFAULT_PROBLEM_LIMIT,
        ge=1,
        le=1000,
        description="how many problem papers to list (totals are always exact)",
    ),
    parser_papers: bool = Query(
        False,
        description=(
            "include the live paper ids behind each parser stamp -- the worklist "
            "for re-parsing what a backend switch did not reach"
        ),
    ),
    checker: Callable[..., Any] = Depends(get_checker),
) -> ConsistencyOut:
    """Report every paper whose copies disagree across the three stores."""
    report = checker(limit=limit, with_parser_papers=parser_papers)
    return ConsistencyOut.model_validate(report.as_dict())

def get_mirror_auditor() -> Callable[..., Any]:
    """The mirror audit, as a dependency tests can swap out."""
    return identifier_mirror_audit.audit_identifier_mirrors


@router.get("/doi", response_model=IdentifierMirrorOut)
def get_identifier_mirrors(
    limit: int = Query(
        50,
        ge=1,
        le=1000,
        description="how many drift rows to list (totals are always exact)",
    ),
    auditor: Callable[..., Any] = Depends(get_mirror_auditor),
) -> IdentifierMirrorOut:
    """Read-only audit: are ``papers.doi``/``arxiv_id`` in step with ``paper_identifiers``?

    The mirrors are maintained by the application, not the database, so this is a
    watchdog, not a constraint: it reports any drift (mirror set without an
    identifier, identifier without a mirror, a value that disagrees, duplicates,
    or identifiers pointing at a deleted paper) and never writes or repairs.
    """
    report = auditor(limit=limit)
    return IdentifierMirrorOut.model_validate(report.as_dict())
