"""Audit evidence describes actual decisions and cleanup failures."""

from app.services import ingestion_service as ingest
from app.workers import tasks
from tests.test_metadata_merge import make_paper


def test_fingerprint_conflict_retains_input_evidence_and_failed_cleanup(db_session, monkeypatch):
    incoming = make_paper(db_session, doi="10.1234/collision", title="Incoming paper")
    target = make_paper(db_session, doi="10.1234/collision", fingerprint="doi:10.1234/collision", title="Target paper")
    job = ingest.create_job(db_session, source_type="file", payload={"ingest_actor": "audit-test", "ingest_request_id": "audit-request"})
    db_session.commit()
    monkeypatch.setattr(tasks.opensearch, "delete_by_paper_id", lambda _: None)
    def broken(_): raise OSError("storage offline")
    monkeypatch.setattr(tasks.object_storage, "delete_prefix", broken)
    tasks._discard_duplicate_paper(db_session, incoming, job, target)
    db_session.expire_all()
    event = job.payload["deduplication_audit"][0]
    assert event["kind"] == "fingerprint_conflict"
    assert event["target_paper_id"] == target.id
    assert event["evidence"]["incoming"]["title"] == "Incoming paper"
    assert event["evidence"]["target"]["title"] == "Target paper"
    assert event["evidence"]["computed_fingerprint"] == "doi:10.1234/collision"
    assert event["file_action"]["objects"] == "cleanup_failed"
    assert event["actor"] == "audit-test" and event["request_id"] == "audit-request"
    assert incoming.deleted_at is not None and target.deleted_at is None