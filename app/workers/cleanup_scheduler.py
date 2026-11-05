"""Periodic retries of invalid figures and opt-in operational retention."""

from dataclasses import dataclass
from uuid import uuid4

from app.core.config import settings
from app.db.session import SessionLocal
from app.schemas.cleanup import CleanupIn
from app.services import cleanup_policy, cleanup_service, figure_service
from app.workers.housekeeping import GcReport, Housekeeping


@dataclass
class CleanupPass(GcReport):
    figure_objects: int = 0
    automatic: bool = False

    def as_dict(self):
        return {**super().as_dict(), "figure_objects": self.figure_objects, "automatic": self.automatic}


def collect():
    with SessionLocal() as session:
        report = CleanupPass(figure_objects=figure_service.cleanup(session), automatic=settings.cleanup_auto_enabled)
        if not settings.cleanup_auto_enabled:
            return report
        categories = cleanup_policy.policy()["automatic_categories"]
        body = CleanupIn(request_id=uuid4(), categories=categories)
        preview = cleanup_service.submit(session, body, actor="scheduler", enqueue=False)
        cleanup_service.execute(session, preview.id)
        if preview.stage == "COMPLETED":
            apply = cleanup_service.submit(session, CleanupIn(request_id=uuid4(), categories=categories,
                dry_run=False, preview_job_id=preview.id), actor="scheduler", enqueue=False)
            cleanup_service.execute(session, apply.id)
        return report


_scheduler = None


def scheduler():
    global _scheduler
    if _scheduler is None:
        _scheduler = Housekeeping(interval=settings.cleanup_interval_s, runner=collect)
    return _scheduler


def start():
    scheduler().start()


async def stop():
    await scheduler().stop()


def stats():
    return scheduler().stats()
