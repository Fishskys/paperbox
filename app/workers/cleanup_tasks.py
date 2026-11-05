"""Cleanup uses the existing queue and database sessions."""

from app.db.session import SessionLocal
from app.services import cleanup_service, ingestion_service, operation_receipts


def run(job_id):
    with SessionLocal() as session:
        try:
            cleanup_service.execute(session, job_id)
        except Exception as exc:
            session.rollback()
            job = cleanup_service.get(session, job_id)
            job.payload = {**job.payload, "report_available": True}
            ingestion_service.mark_failed(session, job, str(exc), code="CLEANUP_FAILED")
            operation_receipts.remember(session, job)
            session.commit()
