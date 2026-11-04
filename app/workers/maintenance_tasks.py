"""Thread entry point using the existing single-process queue."""

from app.db.session import SessionLocal
from app.services import ingestion_service, maintenance_service


def run(job_id: str) -> None:
    with SessionLocal() as session:
        try:
            maintenance_service.execute(session, job_id)
        except Exception as exc:
            session.rollback()
            job = maintenance_service.get(session, job_id)
            ingestion_service.mark_failed(session, job, str(exc), code="MAINTENANCE_FAILED")
            session.commit()
