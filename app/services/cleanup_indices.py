"""Explicit ownership and alias checks, independent of physical index names."""

from contextlib import contextmanager

from sqlalchemy import select
from app.core.config import settings

from app.db.models import CleanupLock, ManagedSearchIndex
from app.search import opensearch
from app.services.cleanup_policy import digest


@contextmanager
def exclusive(session):
    if session.get(CleanupLock, "system") is None:
        # Migrations seed this row; SQLite test stores build the schema directly.
        session.add(CleanupLock(name="system"))
        session.flush()
    session.execute(select(CleanupLock).where(CleanupLock.name == "system").with_for_update()).scalar_one()
    yield


class IndexRegistry:
    def __init__(self, session, client=None):
        self.session = session
        self.client = client or opensearch.get_client()

    def snapshot(self, name):
        body = self.client.indices.get(index=name)
        if set(body) != {name}:
            raise ValueError("an explicit physical index is required")
        info = body[name]
        cluster = self.client.info().get("cluster_uuid")
        uuid = info.get("settings", {}).get("index", {}).get("uuid")
        if not cluster or not uuid:
            raise ValueError("cannot verify cluster and index identity")
        fields = info.get("mappings", {}).get("properties", {})
        compatible = fields.get("paper_id", {}).get("type") == "keyword" and fields.get("embedding", {}).get("type") == "knn_vector"
        row = self.session.get(ManagedSearchIndex, name)
        owned = bool(row and row.index_uuid == uuid and row.cluster_uuid == cluster)
        aliases = sorted(info.get("aliases", {}))
        return {"name": name, "cluster_uuid": cluster, "index_uuid": uuid, "aliases": aliases,
            "owned": owned, "is_rollback": bool(row and row.is_rollback), "compatible": compatible,
            "revision": digest({"uuid": uuid, "cluster": cluster, "aliases": aliases, "rollback": bool(row and row.is_rollback), "owned": owned})}

    def list(self):
        rows = self.client.cat.indices(format="json")
        if len(rows) > 200:
            raise ValueError("index inventory exceeds the supported 200-index bound")
        result = []
        for item in rows:
            name = item.get("index", "")
            if name.startswith("."):
                continue
            info = self.snapshot(name)
            if not info["compatible"] and not info["owned"]:
                continue
            info.update(documents=int(item.get("docs.count") or 0), display_size=item.get("store.size"))
            result.append(info)
        return result

    def claim(self, name, *, actor, rollback=False):
        info = self.snapshot(name)
        if not info["compatible"]:
            raise ValueError("index mapping does not identify a paper index")
        row = self.session.get(ManagedSearchIndex, name)
        if row and (row.index_uuid != info["index_uuid"] or row.cluster_uuid != info["cluster_uuid"]):
            raise ValueError("registered index identity changed; inspect ownership first")
        if row is None:
            row = ManagedSearchIndex(name=name, cluster_uuid=info["cluster_uuid"], index_uuid=info["index_uuid"], claimed_by=actor)
            self.session.add(row)
        row.is_rollback = rollback
        self.session.flush()
        return self.snapshot(name)

    def deletion_guard(self, name):
        info = self.snapshot(name)
        if name in (settings.opensearch_index, settings.opensearch_alias):
            return info, "configured current index is protected"
        if not info["owned"]:
            return info, "index is not claimed by this installation"
        if info["aliases"]:
            return info, "index still has aliases"
        if info["is_rollback"]:
            return info, "index is protected for rollback"
        rollbacks = self.session.scalars(select(ManagedSearchIndex).where(ManagedSearchIndex.is_rollback.is_(True))).all()
        if not any(row.name != name and self.snapshot(row.name)["owned"] for row in rollbacks):
            return info, "claim and protect a separate rollback index first"
        return info, None
