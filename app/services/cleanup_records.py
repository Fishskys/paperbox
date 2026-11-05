"""Retention of operational rows; provenance and source evidence are excluded."""

from datetime import timedelta

from sqlalchemy import delete, func, or_, select

from app.db.models import (Author, CleanupCandidate, FigureExtractionRun, IngestionJob, Paper,
    PaperAuthor, PaperDegradation, PaperFigure, PaperFigureAsset, PapersTag, PaperTag, SearchQuery, Venue, VenueEdition)
from app.services import cleanup_policy as policy, operation_receipts

MODELS = {"search_logs": SearchQuery, "jobs": IngestionJob, "figure_history": FigureExtractionRun,
    "authors": Author, "tags": PaperTag, "venues": Venue}


def revision(row):
    return policy.digest({column.name: getattr(row, column.name) for column in row.__table__.columns})


def item(category, row, *, key=None, reason=None):
    return {"category": category, "resource_key": key or row.id, "revision": revision(row),
        "detail": {"created_at": str(getattr(row, "created_at", None)), "paper_id": getattr(row, "paper_id", None)},
        "state": "protected" if reason else "ready", "error": reason, "size_bytes": None}


def active_references(session):
    result = set()
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("job_id") and isinstance(child, str):
                    result.add(child)
                elif key.endswith("job_ids") and isinstance(child, list):
                    result.update(item for item in child if isinstance(item, str))
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    # A still-valid preview and an unfinished parent report are durable references.
    query = select(IngestionJob).where(or_(IngestionJob.stage.not_in(("COMPLETED", "FAILED")), IngestionJob.kind == "cleanup"))
    jobs = session.scalars(query.limit(policy.policy()["scan_limit"] + 1)).all()
    if len(jobs) > policy.policy()["scan_limit"]:
        raise ValueError("active-reference scan exceeds configured limit")
    for job in jobs:
        payload = job.payload or {}
        expires = payload.get("expires_at")
        if job.stage in ("COMPLETED", "FAILED") and not (expires and expires > policy.now().isoformat()):
            continue
        payload = {**payload, "preview_job_id": job.id} if expires else payload
        visit(payload)
    result.update(session.scalars(select(FigureExtractionRun.job_id).where(FigureExtractionRun.status.in_(("queued", "running")))))
    return result


def referenced(session, model, identifier):
    relations = {Author: [(PaperAuthor, PaperAuthor.author_id)], PaperTag: [(PapersTag, PapersTag.tag_id)],
        Venue: [(Paper, Paper.venue_id)]}
    found = any(session.scalar(select(func.count()).select_from(table).where(column == identifier)) for table, column in relations[model])
    if model is Venue:
        found = found or session.scalar(select(func.count()).select_from(Paper).where(Paper.venue_edition_id.in_(select(VenueEdition.id).where(VenueEdition.venue_id == identifier))))
    return bool(found)


def editions_revision(session, venue_id, *, lock=False):
    query = select(VenueEdition).where(VenueEdition.venue_id == venue_id).order_by(VenueEdition.id)
    rows = session.scalars(query.with_for_update() if lock else query)
    return policy.digest([revision(row) for row in rows])


def scan(session, category, moment):
    if category == "orphan_records":
        for key in ("authors", "tags", "venues"):
            model = MODELS[key]
            query = select(model).where(model.created_at < moment - timedelta(days=policy.policy()["days"][category])).order_by(model.created_at)
            for row in session.scalars(query.limit(policy.policy()["scan_limit"])):
                if not referenced(session, model, row.id):
                    candidate = item(category, row, key=f"{key}:{row.id}")
                    if model is Venue:
                        candidate["detail"]["editions_revision"] = editions_revision(session, row.id)
                    yield candidate
        return
    model = MODELS[category]
    if category == "jobs":
        refs = active_references(session)
        age = func.coalesce(model.finished_at, model.updated_at)
        query = select(model).where(model.stage.in_(("COMPLETED", "FAILED"))).order_by(age)
        for row in session.scalars(query.limit(policy.policy()["scan_limit"])):
            days = policy.policy()["days"]["failed_jobs" if row.stage == "FAILED" else "jobs"]
            if policy.aware(row.finished_at or row.updated_at) < moment - timedelta(days=days):
                yield item(category, row, reason="referenced by an active task" if row.id in refs else None)
        return
    age = model.finished_at if category == "figure_history" else model.created_at
    query = select(model).where(age < moment - timedelta(days=policy.policy()["days"][category])).order_by(age)
    if category == "figure_history":
        query = query.where(model.is_current.is_(False), model.cleanup_pending.is_(False), model.status.not_in(("queued", "running")))
    for row in session.scalars(query.limit(policy.policy()["scan_limit"])):
        yield item(category, row)


def load(session, candidate):
    category, key = candidate.category, candidate.resource_key
    if category == "orphan_records":
        kind, key = key.split(":", 1)
        model = MODELS[kind]
    else:
        model = MODELS[category]
    row = session.execute(select(model).where(model.id == key).with_for_update()).scalar_one_or_none()
    if row is None or revision(row) != candidate.revision:
        raise ValueError("record changed or was removed after preview")
    if category == "orphan_records" and referenced(session, model, key):
        raise ValueError("dictionary record is now referenced")
    if category == "orphan_records" and model is Venue and editions_revision(session, key, lock=True) != candidate.detail.get("editions_revision"):
        raise ValueError("venue editions changed after preview")
    if category == "jobs" and (row.stage not in ("COMPLETED", "FAILED") or row.id in active_references(session)):
        raise ValueError("task is active or referenced")
    if category == "figure_history" and (row.is_current or row.cleanup_pending or row.status in ("queued", "running")):
        raise ValueError("figure generation is current or not cleaned")
    return row


def remove(session, candidate, store):
    row = load(session, candidate)
    if candidate.category == "jobs":
        operation_receipts.remember(session, row)
        session.execute(delete(CleanupCandidate).where(CleanupCandidate.job_id == row.id))
        for run in session.scalars(select(FigureExtractionRun).where(FigureExtractionRun.job_id == row.id)):
            run.job_id = None
        for record in session.scalars(select(PaperDegradation).where(PaperDegradation.job_id == row.id)):
            record.job_id = None
    if candidate.category == "figure_history":
        from app.services.figure_service import prefix
        if next(iter(store.list_objects(prefix=prefix(row), bucket=row.bucket)), None) is not None:
            raise ValueError("figure generation still has stored objects")
        ids = list(session.scalars(select(PaperFigure.id).where(PaperFigure.run_id == row.id)))
        session.execute(delete(PaperFigureAsset).where(PaperFigureAsset.figure_id.in_(ids)))
        session.execute(delete(PaperFigure).where(PaperFigure.run_id == row.id))
    if candidate.category == "orphan_records" and isinstance(row, Venue):
        session.execute(delete(VenueEdition).where(VenueEdition.venue_id == row.id))
    session.delete(row)
