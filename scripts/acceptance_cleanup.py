#!/usr/bin/env python3
"""Real-store cleanup acceptance in a new database, bucket and physical indices.

Requires CREATE DATABASE permission and healthy PostgreSQL/MinIO/OpenSearch.
Only resources created by this invocation are removed, including on failure.
"""

from datetime import timedelta
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.orm import Session
    from app.core.config import REPO_ROOT, settings
    from app.db.models import Author, CleanupReceipt, IngestionJob, MetadataSourceReviewEvent, Paper, PaperAuthor, PaperSource, SearchQuery
    from app.schemas.cleanup import CleanupIn
    from app.search import opensearch
    from app.services import cleanup_indices, cleanup_policy as policy, cleanup_service as service, object_storage

    token = uuid4().hex
    database = f"paperbox_cleanup_accept_{token}"
    bucket = f"paperbox-cleanup-accept-{token}"
    names = [f"paperbox-cleanup-accept-{token}-{suffix}" for suffix in ("live", "old")]
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    engine = None
    created_db = created_bucket = False
    owned_indices = {}
    previous_bucket = settings.minio_bucket
    client, storage = opensearch.get_client(), object_storage.get_client()
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        created_db = True
        url = admin.url.set(database=database)
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
        command.upgrade(config, "head")
        engine = create_engine(url)
        assert not storage.bucket_exists(bucket), "generated bucket already exists"
        storage.make_bucket(bucket)
        created_bucket = True
        settings.minio_bucket = bucket
        for name in names:
            assert not client.indices.exists(index=name), "generated index already exists"
            client.indices.create(index=name, body={"mappings": {"properties": {
                "paper_id": {"type": "keyword"}, "embedding": {"type": "knn_vector", "dimension": 2}}}})
            owned_indices[name] = client.indices.get(index=name)[name]["settings"]["index"]["uuid"]
        client.indices.put_alias(index=names[0], name=f"paperbox-cleanup-accept-{token}-alias")
        with Session(engine, expire_on_commit=False) as session:
            paper = Paper(id=str(uuid4()), title="Cleanup synthetic acceptance", fingerprint=f"accept:{token}", status="DELETED", deleted_at=policy.now())
            session.add(paper)
            session.flush()
            source = PaperSource(id=str(uuid4()), paper_id=paper.id, source_type="manual", source_ref=f"accept:{token}", raw={}, match_status="matched")
            session.add(source)
            session.flush()
            session.add(MetadataSourceReviewEvent(source_id=source.id, actor="acceptance", reason="synthetic", action="accept", payload={}))
            job = IngestionJob(id=str(uuid4()), paper_id=paper.id, kind="ingest", stage="COMPLETED", payload={})
            session.add(job)
            for name in names:
                cleanup_indices.IndexRegistry(session, client).claim(name, actor="acceptance", rollback=name == names[0])
                client.index(index=name, id=paper.id, body={"paper_id": paper.id, "embedding": [0.0, 1.0]}, refresh=True)
            key = f"papers/{paper.id}/original.pdf"
            object_storage.upload_bytes(key, b"synthetic acceptance bytes", bucket=bucket)
            session.add(SearchQuery(query="synthetic acceptance", mode="keyword", top_k=10, rerank=False, returned=1,
                results=[{"paper_id": paper.id, "title": paper.title}], created_at=policy.now() - timedelta(days=100)))
            unused = Author(name="Synthetic unused", normalized_name=f"unused:{token}", created_at=policy.now() - timedelta(days=200))
            used = Author(name="Synthetic used", normalized_name=f"used:{token}", created_at=policy.now() - timedelta(days=200))
            stale = IngestionJob(kind="ingest", stage="COMPLETED", payload={}, finished_at=policy.now() - timedelta(days=200))
            session.add_all([unused, used, stale])
            session.flush()
            session.add(PaperAuthor(paper_id=paper.id, author_id=used.id, author_order=0))
            session.commit()

            def task(body):
                record = service.submit(session, body, actor="acceptance", enqueue=False)
                service.execute(session, record.id)
                report = service.report(session, record.id)
                assert record.stage == "COMPLETED", report
                return record, report

            categories = ["search_logs", "jobs", "orphan_records"]
            preview, report = task(CleanupIn(request_id=uuid4(), categories=categories))
            assert report["ready"] == 3, report
            _, report = task(CleanupIn(request_id=uuid4(), categories=categories, dry_run=False, preview_job_id=preview.id))
            assert report["completed"] == 3, report
            assert session.scalar(select(Author.id).where(Author.id == unused.id)) is None
            assert session.get(Author, used.id) and session.get(PaperSource, source.id)
            assert session.get(CleanupReceipt, stale.id)
            session.add(SearchQuery(query="synthetic current query", mode="keyword", top_k=10, rerank=False, returned=1,
                results=[{"paper_id": paper.id, "title": paper.title}]))
            session.commit()

            preview, report = task(CleanupIn(request_id=uuid4(), categories=["deleted_papers"], paper_ids=[paper.id]))
            assert report["ready"] == 1 and storage.stat_object(bucket, key)
            apply, report = task(CleanupIn(request_id=uuid4(), categories=["deleted_papers"], paper_ids=[paper.id],
                dry_run=False, preview_job_id=preview.id))
            assert report["completed"] == 1, report
            assert session.scalar(select(Paper.id).where(Paper.id == paper.id)) is None
            assert session.scalar(select(MetadataSourceReviewEvent.id)) is None
            assert session.get(CleanupReceipt, job.id)
            assert not list(storage.list_objects(bucket, prefix=f"papers/{paper.id}/", recursive=True))
            assert all(client.count(index=name)["count"] == 0 for name in names)

            preview, report = task(CleanupIn(request_id=uuid4(), categories=["indices"], indices=[names[1]]))
            assert report["ready"] == 1
            _, report = task(CleanupIn(request_id=uuid4(), categories=["indices"], indices=[names[1]], dry_run=False, preview_job_id=preview.id))
            assert report["completed"] == 1 and not client.indices.exists(index=names[1])
            protected, report = task(CleanupIn(request_id=uuid4(), categories=["indices"], indices=[names[0]]))
            assert report["protected"] == 1 and client.indices.exists(index=names[0])
        print("Cleanup acceptance passed: operational retention, protected references, frozen preview, PostgreSQL foreign keys, MinIO deletion, historical index documents, index retirement and alias protection.")
    finally:
        settings.minio_bucket = previous_bucket
        for name, identity in owned_indices.items():
            if client.indices.exists(index=name):
                current = client.indices.get(index=name)[name]["settings"]["index"]["uuid"]
                if current != identity:
                    raise RuntimeError("acceptance index identity changed; refusing cleanup")
                client.indices.delete(index=name)
        if created_bucket:
            for obj in storage.list_objects(bucket, recursive=True):
                storage.remove_object(bucket, obj.object_name)
            storage.remove_bucket(bucket)
        if engine:
            engine.dispose()
        if created_db:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database}"'))
        admin.dispose()
        print("Synthetic acceptance resources removed.")


if __name__ == "__main__":
    main()