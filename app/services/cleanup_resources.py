"""Resource adapters behind the durable cleanup use case."""

from sqlalchemy import select

from app.db.models import IngestionJob, Paper
from app.services import cleanup_indices, cleanup_objects, cleanup_policy as policy, cleanup_records, object_storage


class Resources:
    def __init__(self, session, *, store=object_storage, client=None):
        self.session, self.store, self.client = session, store, client

    def scan(self, category, request, moment):
        if category == "app_logs":
            yield from cleanup_objects.scan_logs(moment)
        elif category in ("parse_cache", "orphan_objects"):
            yield from cleanup_objects.scan_objects(self.session, self.store, category, moment)
        elif category in ("deleted_papers",):
            from app.services import cleanup_papers
            yield from cleanup_papers.scan(self.session, self.store, self.client, request)
        elif category == "indices":
            registry = cleanup_indices.IndexRegistry(self.session, self.client)
            names = request["indices"] or [item["name"] for item in registry.list()]
            for name in names:
                info, reason = registry.deletion_guard(name)
                yield {"category": category, "resource_key": name, "revision": info["revision"], "detail": info,
                    "size_bytes": None, "state": "protected" if reason else "ready", "error": reason}
        else:
            yield from cleanup_records.scan(self.session, category, moment)

    def remove(self, candidate):
        paper_id = (candidate.detail or {}).get("paper_id")
        if paper_id:
            self.session.execute(select(Paper).where(Paper.id == paper_id).with_for_update(of=Paper)).scalar_one_or_none()
            active = self.session.scalar(select(IngestionJob.id).where(IngestionJob.paper_id == paper_id,
                IngestionJob.stage.not_in(("COMPLETED", "FAILED"))).limit(1))
            if active:
                raise ValueError("paper has an active task")
        if candidate.category == "app_logs":
            try:
                cleanup_objects.remove_log(candidate)
            except FileNotFoundError:
                pass
        elif candidate.category in ("parse_cache", "orphan_objects"):
            cleanup_objects.remove_objects(self.session, self.store, candidate)
        elif candidate.category == "indices":
            registry = cleanup_indices.IndexRegistry(self.session, self.client)
            if not registry.client.indices.exists(index=candidate.resource_key):
                if registry.client.info().get("cluster_uuid") != candidate.detail["cluster_uuid"]:
                    raise ValueError("cluster identity changed after preview")
                return
            info, reason = registry.deletion_guard(candidate.resource_key)
            if reason or info["revision"] != candidate.revision:
                raise ValueError(reason or "index changed after preview")
            registry.client.indices.delete(index=candidate.resource_key)
        elif candidate.category == "deleted_papers":
            from app.services import cleanup_papers
            cleanup_papers.remove(self.session, self.store, self.client, candidate)
        else:
            cleanup_records.remove(self.session, candidate, self.store)
