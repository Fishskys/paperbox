"""Administrative durable tasks and authenticated report downloads."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from uuid import UUID
from sqlalchemy.orm import Session

from app.core.security import require_admin
from app.db.session import get_db
from app.schemas.maintenance import MaintenanceIn, MaintenanceOut
from app.services import maintenance_service as service
from app.services.api_key_service import AuthIdentity

router = APIRouter(prefix="/api/maintenance/jobs", tags=["maintenance"], dependencies=[Depends(require_admin)])


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except service.MaintenanceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.post("", response_model=MaintenanceOut, status_code=202)
def submit(body: MaintenanceIn, session: Session = Depends(get_db), identity: AuthIdentity = Depends(require_admin)):
    return service.serialize(_call(service.submit, session, body, actor=identity.prefix))


@router.get("/{job_id}", response_model=MaintenanceOut)
def detail(job_id: str, session: Session = Depends(get_db)):
    return service.serialize(_call(service.get, session, job_id))


@router.get("/requests/{request_id}", response_model=MaintenanceOut)
def by_request(request_id: UUID, session: Session = Depends(get_db), identity: AuthIdentity = Depends(require_admin)):
    return service.serialize(_call(service.get, session, service.request_job_id(identity.prefix, request_id)))


@router.get("/{job_id}/report")
def report(job_id: str, session: Session = Depends(get_db)):
    job = _call(service.get, session, job_id)
    if "report" not in (job.payload or {}):
        raise HTTPException(status_code=409, detail="report is not available yet")
    return JSONResponse(job.payload["report"], headers={"Content-Disposition": f'attachment; filename="maintenance-{job.id}.json"'})
