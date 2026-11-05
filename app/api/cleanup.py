"""Admin-only policy, inventory, previews, execution and report access."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.security import require_admin
from app.db.session import get_db
from app.schemas.cleanup import CleanupIn, CleanupJobOut, CleanupListOut, CleanupReportOut, IndexClaimIn
from app.services import cleanup_service as service
from app.services.api_key_service import AuthIdentity

router = APIRouter(prefix="/api/admin/cleanup", tags=["cleanup"], dependencies=[Depends(require_admin)])


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except service.CleanupError as exc:
        raise HTTPException(exc.status, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


@router.get("/policy")
def policy(session: Session = Depends(get_db)):
    return call(service.effective_policy, session)


@router.get("/deleted-papers")
def deleted_papers(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), session: Session = Depends(get_db)):
    return service.deleted_papers(session, offset=offset, limit=limit)


@router.get("/indices")
def indices(session: Session = Depends(get_db)):
    return call(service.list_indices, session)


@router.post("/indices/claim")
def claim(body: IndexClaimIn, session: Session = Depends(get_db), identity: AuthIdentity = Depends(require_admin)):
    return call(service.claim_index, session, body, actor=identity.prefix)


@router.post("/jobs", response_model=CleanupJobOut, status_code=202)
def submit(body: CleanupIn, session: Session = Depends(get_db), identity: AuthIdentity = Depends(require_admin)):
    return service.serialize(call(service.submit, session, body, actor=identity.prefix))


@router.get("/jobs", response_model=CleanupListOut)
def jobs(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), session: Session = Depends(get_db)):
    return service.list_jobs(session, offset=offset, limit=limit)


@router.get("/jobs/requests/{request_id}", response_model=CleanupJobOut)
def by_request(request_id: UUID, session: Session = Depends(get_db), identity: AuthIdentity = Depends(require_admin)):
    return service.serialize(call(service.get, session, service.request_job_id(identity.prefix, request_id)))


@router.get("/jobs/{job_id}", response_model=CleanupJobOut)
def detail(job_id: UUID, session: Session = Depends(get_db)):
    return service.serialize(call(service.get, session, str(job_id)))


@router.get("/jobs/{job_id}/report", response_model=CleanupReportOut)
def report(job_id: UUID, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000), session: Session = Depends(get_db)):
    return call(service.report, session, str(job_id), offset=offset, limit=limit)


@router.get("/jobs/{job_id}/download")
def download(job_id: UUID, session: Session = Depends(get_db)):
    # The ledger was bounded at creation; later policy changes must not truncate it.
    result = call(service.report, session, str(job_id), limit=None)
    return JSONResponse(jsonable_encoder(result), headers={"Content-Disposition": f'attachment; filename="cleanup-{job_id}.json"'})
