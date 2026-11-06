"""Administrator-only process memory and application logs."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.core.security import require_admin
from app.schemas.observability import LogFilters, LogsOut, RuntimeOut
from app.services import runtime_metrics, system_logs

router = APIRouter(prefix="/api/admin/observability", tags=["observability"], dependencies=[Depends(require_admin)])


@router.get("/memory", response_model=RuntimeOut)
def memory():
    return runtime_metrics.snapshot()


@router.get("/logs", response_model=LogsOut)
def logs(filters: Annotated[LogFilters, Query()]):
    return system_logs.query(filters)


@router.get("/logs/export", response_model=LogsOut)
def export_logs(filters: Annotated[LogFilters, Query()]):
    result = system_logs.query(filters, export=True)
    return JSONResponse(result.model_dump(mode="json"), headers={
        "Content-Disposition": 'attachment; filename="paperbox-logs.json"',
        "Cache-Control": "no-store",
    })
