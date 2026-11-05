"""Permanent deletion with frozen object/index identities and relational closure."""

from sqlalchemy import Text, cast, delete, or_, select

from app.core.config import settings
from app.db.models import Base, FigureExtractionRun, IngestionJob, Paper, PaperFile, SearchQuery
from app.services import cleanup_indices, cleanup_objects, cleanup_policy as policy, operation_receipts


def closure(session, paper_id):
    """Follow foreign keys, including tables added after the original purge CLI."""
    values = {"papers": {"id": [paper_id]}}
    found = []
    total = 0
    for table in Base.metadata.sorted_tables:
        if table.name == "papers":
            condition = table.c.id == paper_id
        else:
            predicates = [column.in_(values[fk.column.table.name][fk.column.name])
                for column in table.columns for fk in column.foreign_keys
                if fk.column.table.name in values and fk.column.name in values[fk.column.table.name]]
            if not predicates:
                continue
            condition = or_(*predicates)
        rows = session.execute(select(table).where(condition).limit(settings.cleanup_scan_limit + 1)).mappings().all()
        total += len(rows)
        if total > settings.cleanup_scan_limit:
            raise ValueError("paper relational scope exceeds configured scan limit")
        if any(row.get("paper_id") not in (None, paper_id) for row in rows):
            raise ValueError("cross-paper references require manual inspection")
        values[table.name] = {column.name: [row[column.name] for row in rows] for column in table.primary_key.columns}
        if rows:
            found.append((table, condition, rows))
    return found


def database_revision(session, paper_id):
    return policy.digest({table.name: sorted((dict(row) for row in rows), key=lambda row: str(row))
        for table, _, rows in closure(session, paper_id)})


def snapshot(session, store, client, paper):
    if paper.deleted_at is None:
        raise ValueError("paper is not soft-deleted")
    if session.scalar(select(IngestionJob.id).where(IngestionJob.paper_id == paper.id,
        IngestionJob.stage.not_in(("COMPLETED", "FAILED"))).limit(1)):
        raise ValueError("paper has an active task")
    for job in json_references(session, IngestionJob, IngestionJob.payload, paper.id):
        if job.kind != "cleanup" and job.stage not in ("COMPLETED", "FAILED"):
            raise ValueError("an active task references this paper")
    buckets = {settings.minio_bucket}
    buckets.update(session.scalars(select(PaperFile.bucket).where(PaperFile.paper_id == paper.id)))
    buckets.update(session.scalars(select(FigureExtractionRun.bucket).where(FigureExtractionRun.paper_id == paper.id)))
    objects = []
    for bucket in sorted(b for b in buckets if b):
        if cleanup_objects.versioning(store, bucket) in ("Enabled", "Suspended"):
            raise ValueError("versioned bucket needs separate permanent-version cleanup")
        for obj in store.list_objects(prefix=f"papers/{paper.id}/", bucket=bucket):
            objects.append({"bucket": bucket, **cleanup_objects.object_info(obj)})
            if len(objects) > settings.cleanup_scan_limit:
                raise ValueError("paper object scope exceeds configured scan limit")
    registry = cleanup_indices.IndexRegistry(session, client)
    indices = []
    for info in registry.list():
        count = int(registry.client.count(index=info["name"], body={"query": {"term": {"paper_id": paper.id}}})["count"])
        if count and not info["owned"]:
            raise ValueError("paper documents remain in an unclaimed index; confirm index ownership first")
        if info["owned"]:
            indices.append({"name": info["name"], "index_uuid": info["index_uuid"], "cluster_uuid": info["cluster_uuid"], "documents": count})
    return {"paper_id": paper.id, "deleted_at": paper.deleted_at.isoformat(), "database_revision": database_revision(session, paper.id),
        "buckets": sorted(b for b in buckets if b), "objects": sorted(objects, key=lambda value: (value["bucket"], value["key"])),
        "indices": sorted(indices, key=lambda value: value["name"])}


def scan(session, store, client, request):
    query = select(Paper).where(Paper.deleted_at.is_not(None)).order_by(Paper.deleted_at)
    if request["paper_ids"]:
        query = query.where(Paper.id.in_(request["paper_ids"]))
    for paper in session.scalars(query.limit(settings.cleanup_scan_limit)):
        try:
            detail = snapshot(session, store, client, paper)
            reason = None
        except ValueError as exc:
            detail, reason = {"paper_id": paper.id}, str(exc)
        yield {"category": "deleted_papers", "resource_key": paper.id, "revision": policy.digest(detail), "detail": detail,
            "size_bytes": sum(obj["size"] for obj in detail.get("objects", [])) if not reason else None,
            "state": "protected" if reason else "ready", "error": reason}


def scrub(value, paper_id):
    if isinstance(value, dict):
        if value.get("paper_id") == paper_id or value.get("id") == paper_id:
            return None
        return {key: scrub(child, paper_id) for key, child in value.items() if key != paper_id and child != paper_id}
    if isinstance(value, list):
        return [clean for child in value if child != paper_id and (clean := scrub(child, paper_id)) is not None]
    return value


def json_references(session, model, column, paper_id):
    rows = session.scalars(select(model).where(cast(column, Text).contains(paper_id)).limit(settings.cleanup_scan_limit + 1)).all()
    if len(rows) > settings.cleanup_scan_limit:
        raise ValueError("JSON reference scan exceeds configured limit")
    return rows


def remove(session, store, client, candidate):
    paper = session.get(Paper, candidate.resource_key)
    if paper is None:
        return  # A previous transaction already completed the relational step.
    expected = candidate.detail
    current = snapshot(session, store, client, paper)
    if current["database_revision"] != expected["database_revision"] or current["deleted_at"] != expected["deleted_at"]:
        raise ValueError("paper data changed since preview")
    old_objects = {(obj["bucket"], obj["key"]): obj for obj in expected["objects"]}
    if any(obj != old_objects.get((obj["bucket"], obj["key"])) for obj in current["objects"]):
        raise ValueError("paper object identity changed since preview")
    old_indices = {item["name"]: item for item in expected["indices"]}
    if {item["name"] for item in current["indices"]} != set(old_indices):
        raise ValueError("registered index scope changed since preview")
    registry = cleanup_indices.IndexRegistry(session, client)
    for info in current["indices"]:
        old = old_indices[info["name"]]
        if any(info[key] != old[key] for key in ("index_uuid", "cluster_uuid")) or info["documents"] > old["documents"]:
            raise ValueError("index identity or paper documents changed since preview")
        result = registry.client.delete_by_query(index=info["name"], body={"query": {"term": {"paper_id": paper.id}}}, refresh=True)
        if result.get("failures") or result.get("timed_out") or result.get("version_conflicts"):
            raise RuntimeError("index document deletion did not complete")
    for obj in current["objects"]:
        store.delete_object(obj["key"], bucket=obj["bucket"])
    # Remove stored JSON references while retaining unrelated parent/report data.
    for job in json_references(session, IngestionJob, IngestionJob.payload, paper.id):
        if job.kind == "cleanup":
            continue  # Cleanup reports retain identifiers, never copied metadata.
        changed = scrub(job.payload, paper.id)
        if changed != job.payload:
            if job.stage not in ("COMPLETED", "FAILED"):
                raise ValueError("an active task references this paper")
            job.payload = changed or {}
    for query in json_references(session, SearchQuery, SearchQuery.results, paper.id):
        query.results = scrub(query.results, paper.id)
    session.flush()
    for job in session.scalars(select(IngestionJob).where(IngestionJob.paper_id == paper.id)):
        operation_receipts.remember(session, job)
    session.flush()
    for table, condition, _ in reversed(closure(session, paper.id)):
        session.execute(delete(table).where(condition))
