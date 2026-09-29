"""Three-way consistency check: PostgreSQL vs MinIO vs OpenSearch.

Every test runs the check against in-memory SQLite plus fake stores (unit tests
never touch PostgreSQL/MinIO/OpenSearch). The cases pin one drift each, because a
report that stays silent is exactly what makes drift invisible: a paper that
agrees, a file whose object is gone, an object nobody claims, chunks the index
never got, documents whose chunks are gone, and residue of a deleted paper.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import consistency as consistency_api
from app.core.security import require_api_key
from app.db.models import Paper, PaperChunk, PaperFile, new_uuid
from app.db.session import get_db
from app.main import app
from app.services import consistency_service
from tests.test_job_progress import factory  # noqa: F401 - fixture


# --------------------------------------------------------------------------- #
# helpers and fake stores
# --------------------------------------------------------------------------- #


def add_paper(
    session_factory,
    *,
    status: str = "INDEXED",
    deleted: bool = False,
    file_names: tuple[str, ...] = ("original.pdf",),
    chunks: int = 0,
    title: str = "A Paper",
    parser_backend: str | None = None,
    parser_version: str | None = None,
) -> str:
    """Insert one paper with ``file_names`` rows and ``chunks`` chunk rows."""
    session = session_factory()
    try:
        paper = Paper(
            id=new_uuid(),
            title=title,
            fingerprint=f"sha256:{new_uuid()}",
            status=status,
            parser_backend=parser_backend,
            parser_version=parser_version,
        )
        if deleted:
            paper.deleted_at = datetime.now(timezone.utc)
        session.add(paper)
        session.flush()
        for index, name in enumerate(file_names):
            session.add(
                PaperFile(
                    id=new_uuid(),
                    paper_id=paper.id,
                    kind="original" if index == 0 else "arxiv_pdf",
                    object_key=f"papers/{paper.id}/{name}",
                    bucket="paperbox",
                    filename=name,
                    content_type="application/pdf",
                    size_bytes=10,
                )
            )
        for index in range(chunks):
            session.add(
                PaperChunk(
                    id=new_uuid(),
                    paper_id=paper.id,
                    chunk_index=index,
                    page_start=1,
                    page_end=1,
                    section="body",
                    text=f"chunk {index}",
                    token_count=3,
                    char_count=7,
                )
            )
        session.commit()
        return paper.id
    finally:
        session.close()


def object_key(paper_id: str, name: str = "original.pdf") -> str:
    return f"papers/{paper_id}/{name}"


class FakeObject:
    def __init__(self, name: str) -> None:
        self.object_name = name


class FakeStorage:
    """Just enough of the MinIO service module: ``list_objects(prefix=...)``."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = list(keys)

    def list_objects(self, prefix: str = "papers/", bucket=None, *, recursive: bool = True):
        return [FakeObject(key) for key in self.keys if key.startswith(prefix)]


class BrokenStorage:
    def list_objects(self, *args, **kwargs):
        raise RuntimeError("minio is down")


class FakeIndices:
    def __init__(self, exists: bool = True) -> None:
        self._exists = exists

    def exists(self, index: str) -> bool:
        return self._exists


class FakeClient:
    """Enough of the OpenSearch client for one ``terms`` aggregation.

    ``backends`` gives a paper's documents a parser provenance: it becomes the
    ``by_backend`` sub-aggregation, which is what the stamp check reads. A paper
    missing from it has documents written before the stamp existed.
    """

    def __init__(
        self,
        counts: dict[str, int] | None = None,
        exists: bool = True,
        backends: dict[str, dict[str, int]] | None = None,
    ) -> None:
        self.counts = dict(counts or {})
        self.backends = {key: dict(value) for key, value in (backends or {}).items()}
        self.indices = FakeIndices(exists)
        self.requests: list[dict] = []

    def search(self, index=None, body=None):
        self.requests.append({"index": index, "body": body})
        buckets: list[dict] = []
        for key, count in self.counts.items():
            if count <= 0:
                continue
            bucket: dict = {"key": key, "doc_count": count}
            by_backend = self.backends.get(key)
            if by_backend:
                bucket["by_backend"] = {
                    "buckets": [
                        {"key": name, "doc_count": docs}
                        for name, docs in by_backend.items()
                    ]
                }
            buckets.append(bucket)
        return {"aggregations": {"by_paper": {"buckets": buckets}}}


class BrokenClient:
    def __init__(self) -> None:
        self.indices = FakeIndices(True)

    def search(self, *args, **kwargs):
        raise RuntimeError("opensearch is down")


def run(session_factory, storage, client, **kwargs) -> consistency_service.ConsistencyReport:
    return consistency_service.check_consistency(
        session_factory, storage=storage, client=client, **kwargs
    )


def problems_by_paper(report) -> dict[str, list[str]]:
    return {problem.paper_id: problem.issues for problem in report.problems}


# --------------------------------------------------------------------------- #
# the happy path and the false-positive guards
# --------------------------------------------------------------------------- #


def test_a_paper_that_agrees_across_all_three_stores_is_not_reported(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=3)
    report = run(factory, FakeStorage([object_key(paper_id)]), FakeClient({paper_id: 3}))
    assert report.problems_total == 0
    assert report.orphan_objects_total == 0
    assert report.orphan_documents_total == 0
    assert report.errors == []
    assert report.consistent is True
    assert report.papers_total == 1
    assert report.papers_live == 1
    assert report.files_pg == 1
    assert report.chunks_pg == 3
    assert report.documents_os == 3


def test_a_paper_that_is_not_indexed_yet_is_not_a_problem(factory) -> None:  # noqa: F811
    """PENDING/FAILED/AWAITING_FILE papers legitimately have no chunks."""
    pending = add_paper(factory, status="PENDING", chunks=0)
    awaiting = add_paper(factory, status="AWAITING_FILE", chunks=0)
    failed = add_paper(factory, status="FAILED", chunks=0)
    report = run(
        factory,
        FakeStorage([object_key(pid) for pid in (pending, awaiting, failed)]),
        FakeClient({}),
    )
    assert report.problems_total == 0
    assert report.consistent is True


# --------------------------------------------------------------------------- #
# objects: files without bytes, bytes without files
# --------------------------------------------------------------------------- #


def test_a_file_row_without_an_object_is_reported_as_missing(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=1)
    report = run(factory, FakeStorage([]), FakeClient({paper_id: 1}))
    assert problems_by_paper(report)[paper_id] == [consistency_service.ISSUE_MISSING_OBJECT]
    problem = report.problems[0]
    assert problem.missing_objects == [object_key(paper_id)]
    assert problem.files_pg == 1
    assert problem.objects_minio == 0
    assert report.consistent is False


def test_an_object_without_a_file_row_is_reported_as_orphan(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=1)
    stale = object_key(paper_id, "supplement.pdf")
    report = run(factory, FakeStorage([object_key(paper_id), stale]), FakeClient({paper_id: 1}))
    assert problems_by_paper(report)[paper_id] == [consistency_service.ISSUE_ORPHAN_OBJECT]
    assert report.problems[0].orphan_objects == [stale]


def test_only_live_file_rows_are_expected(factory) -> None:  # noqa: F811
    """A soft-deleted file row must not make its (purged) object look missing."""
    paper_id = add_paper(factory, chunks=1)
    session = factory()
    try:
        row = session.query(PaperFile).filter(PaperFile.paper_id == paper_id).one()
        row.deleted_at = datetime.now(timezone.utc)
        session.commit()
    finally:
        session.close()
    report = run(factory, FakeStorage([]), FakeClient({paper_id: 1}))
    assert report.problems_total == 0
    assert report.files_pg == 0


# --------------------------------------------------------------------------- #
# chunks vs documents
# --------------------------------------------------------------------------- #


def test_an_indexed_paper_without_chunks_is_reported(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, status="INDEXED", chunks=0)
    report = run(factory, FakeStorage([object_key(paper_id)]), FakeClient({}))
    assert problems_by_paper(report)[paper_id] == [consistency_service.ISSUE_MISSING_CHUNKS]


def test_chunks_the_index_never_got_are_reported_as_a_missing_index(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=4)
    report = run(factory, FakeStorage([object_key(paper_id)]), FakeClient({}))
    issues = problems_by_paper(report)[paper_id]
    assert issues == [consistency_service.ISSUE_MISSING_INDEX]
    assert report.problems[0].chunks_pg == 4
    assert report.problems[0].chunks_os == 0


def test_a_partial_index_is_reported_as_a_count_mismatch(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=4)
    report = run(factory, FakeStorage([object_key(paper_id)]), FakeClient({paper_id: 2}))
    assert problems_by_paper(report)[paper_id] == [consistency_service.ISSUE_CHUNK_MISMATCH]


def test_documents_without_chunk_rows_are_reported_as_orphans(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, status="PENDING", chunks=0)
    report = run(factory, FakeStorage([object_key(paper_id)]), FakeClient({paper_id: 3}))
    assert problems_by_paper(report)[paper_id] == [consistency_service.ISSUE_ORPHAN_INDEX]


# --------------------------------------------------------------------------- #
# deleted papers and rows no paper claims
# --------------------------------------------------------------------------- #


def test_residue_of_a_deleted_paper_is_reported(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, deleted=True, chunks=0)
    report = run(factory, FakeStorage([object_key(paper_id)]), FakeClient({paper_id: 5}))
    assert problems_by_paper(report)[paper_id] == [consistency_service.ISSUE_DELETED_RESIDUE]
    assert report.papers_deleted == 1


def test_a_cleanly_deleted_paper_is_not_reported(factory) -> None:  # noqa: F811
    add_paper(factory, deleted=True, chunks=2)
    report = run(factory, FakeStorage([]), FakeClient({}))
    assert report.problems_total == 0


def test_objects_and_documents_of_unknown_papers_are_listed(factory) -> None:  # noqa: F811
    ghost_object = object_key(new_uuid(), "original.pdf")
    ghost_paper = new_uuid()
    report = run(factory, FakeStorage([ghost_object]), FakeClient({ghost_paper: 2}))
    assert report.orphan_objects_total == 1
    assert report.orphan_objects == [ghost_object]
    assert report.orphan_documents_total == 1
    assert report.orphan_documents == [ghost_paper]
    assert report.problems_total == 0
    assert report.consistent is False


def test_staging_objects_are_counted_but_never_problems(factory) -> None:  # noqa: F811
    report = run(
        factory,
        FakeStorage(["uploads/batch-1/0-a.pdf", "uploads/batch-1/1-b.pdf"]),
        FakeClient({}),
    )
    assert report.staging_objects == 2
    assert report.orphan_objects_total == 0
    assert report.consistent is True


# --------------------------------------------------------------------------- #
# a broken store degrades the report, it does not raise
# --------------------------------------------------------------------------- #


def test_the_check_resolves_the_module_client_when_none_is_passed(monkeypatch) -> None:
    """A live run passes ``client=None``; that must not become ``None.search``."""
    sentinel = FakeClient({"paper-1": 4})
    monkeypatch.setattr(consistency_service.opensearch, "get_client", lambda: sentinel)
    counts, backends, exists, truncated = consistency_service._load_documents(
        None, "an-alias"
    )
    assert counts == {"paper-1": 4}
    # No ``by_backend`` sub-aggregation in the fake answer: documents written
    # before the stamp existed.
    assert backends == {"paper-1": {}}
    assert exists is True
    assert truncated is False


def test_a_broken_store_is_reported_and_the_others_still_answer(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=2)
    report = run(factory, BrokenStorage(), FakeClient({paper_id: 2}))
    assert any(error.startswith("minio:") for error in report.errors)
    # The object side is unknown, so the missing objects show up as issues.
    assert consistency_service.ISSUE_MISSING_OBJECT in report.problems[0].issues
    assert report.documents_os == 2
    assert report.consistent is False


def test_a_broken_index_client_is_reported(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=2)
    report = run(factory, FakeStorage([object_key(paper_id)]), BrokenClient())
    assert any(error.startswith("opensearch:") for error in report.errors)
    assert report.chunks_pg == 2
    assert report.documents_os == 0


def test_a_missing_index_is_reported_rather_than_counted_as_zero_documents(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=2)
    report = run(factory, FakeStorage([object_key(paper_id)]), FakeClient({}, exists=False))
    assert report.index_exists is False
    assert report.documents_os == 0
    assert report.consistent is False


# --------------------------------------------------------------------------- #
# bounds
# --------------------------------------------------------------------------- #


def test_limit_caps_the_listed_problems_but_not_the_totals(factory) -> None:  # noqa: F811
    for _ in range(5):
        add_paper(factory, chunks=1)
    report = run(factory, FakeStorage([]), FakeClient({}), limit=2)
    assert report.problems_total == 5
    assert len(report.problems) == 2
    assert report.consistent is False


# --------------------------------------------------------------------------- #
# the endpoint
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(factory):  # noqa: F811 - fixture comes from the import above
    """A TestClient wired to the in-memory database and a stub API key."""

    def _db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_api_key] = lambda: "test-key"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_the_default_checker_is_the_service_function() -> None:
    assert consistency_api.get_checker() is consistency_service.check_consistency


def test_the_endpoint_serves_the_report(client, factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=2)
    storage = FakeStorage([object_key(paper_id)])
    fake_os = FakeClient({paper_id: 2})

    def checker():
        return lambda limit: consistency_service.check_consistency(
            factory, storage=storage, client=fake_os, limit=limit
        )

    app.dependency_overrides[consistency_api.get_checker] = checker
    response = client.get("/api/consistency")
    assert response.status_code == 200
    body = response.json()
    assert body["consistent"] is True
    assert body["index_exists"] is True
    assert body["totals"]["papers"] == 1
    assert body["totals"]["chunks_pg"] == 2
    assert body["totals"]["documents_os"] == 2
    assert body["problems"] == []


def test_the_endpoint_lists_a_drifted_paper(client, factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=3)
    storage = FakeStorage([])
    fake_os = FakeClient({})

    def checker():
        return lambda limit: consistency_service.check_consistency(
            factory, storage=storage, client=fake_os, limit=limit
        )

    app.dependency_overrides[consistency_api.get_checker] = checker
    response = client.get("/api/consistency?limit=10")
    assert response.status_code == 200
    body = response.json()
    assert body["consistent"] is False
    assert body["totals"]["problems"] == 1
    problem = body["problems"][0]
    assert problem["paper_id"] == paper_id
    assert consistency_service.ISSUE_MISSING_OBJECT in problem["issues"]
    assert consistency_service.ISSUE_MISSING_INDEX in problem["issues"]


def test_the_endpoint_validates_the_limit(client) -> None:
    assert client.get("/api/consistency?limit=0").status_code == 422
    assert client.get("/api/consistency?limit=100000").status_code == 422


# --------------------------------------------------------------------------- #
# the parser stamp (plan 2026-09-28_160551 section 6.1 step 2)
# --------------------------------------------------------------------------- #


def backend_census(report) -> dict:
    data = report.as_dict()
    return data["parser_backends"]


def test_a_paper_stamped_like_its_documents_is_not_reported(factory) -> None:  # noqa: F811
    """The ordinary case after the switch: stamp and documents agree."""
    paper_id = add_paper(factory, chunks=2, parser_backend="docling", parser_version="2.1")
    report = run(
        factory,
        FakeStorage([object_key(paper_id)]),
        FakeClient({paper_id: 2}, backends={paper_id: {"docling": 2}}),
    )
    assert report.problems_total == 0
    assert backend_census(report) == {
        "papers": {"docling": 1},
        "documents": {"docling": 2},
    }


def test_documents_from_another_backend_than_the_stamp_are_reported(factory) -> None:  # noqa: F811
    """A switch that only reached PostgreSQL: the old parse is still indexed."""
    paper_id = add_paper(factory, chunks=3, parser_backend="docling")
    report = run(
        factory,
        FakeStorage([object_key(paper_id)]),
        FakeClient({paper_id: 3}, backends={paper_id: {"pypdf": 3}}),
    )
    assert problems_by_paper(report)[paper_id] == [
        consistency_service.ISSUE_PARSER_STAMP_MISMATCH
    ]
    problem = report.problems[0]
    assert problem.parser_backend == "docling"
    assert problem.index_backends == ("pypdf",)
    assert backend_census(report)["papers"] == {"docling": 1}
    assert backend_census(report)["documents"] == {"pypdf": 3}


def test_a_paper_whose_documents_are_split_across_backends_is_reported(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=4, parser_backend="docling")
    report = run(
        factory,
        FakeStorage([object_key(paper_id)]),
        FakeClient(
            {paper_id: 4}, backends={paper_id: {"docling": 2, "pypdf": 2}}
        ),
    )
    assert problems_by_paper(report)[paper_id] == [
        consistency_service.ISSUE_PARSER_STAMP_MISMATCH
    ]
    assert report.problems[0].index_backends == ("docling", "pypdf")


def test_documents_without_a_stamp_under_a_stamped_paper_are_reported(factory) -> None:  # noqa: F811
    paper_id = add_paper(factory, chunks=2, parser_backend="docling")
    report = run(factory, FakeStorage([object_key(paper_id)]), FakeClient({paper_id: 2}))
    assert problems_by_paper(report)[paper_id] == [
        consistency_service.ISSUE_PARSER_STAMP_MISMATCH
    ]
    assert report.problems[0].index_backends == ()


def test_rows_indexed_before_the_stamp_existed_are_not_drift(factory) -> None:  # noqa: F811
    """Both sides unknown is agreement -- the whole legacy library looks like this."""
    paper_id = add_paper(factory, chunks=2)
    report = run(factory, FakeStorage([object_key(paper_id)]), FakeClient({paper_id: 2}))
    assert report.problems_total == 0
    assert backend_census(report) == {
        "papers": {consistency_service.UNKNOWN_BACKEND: 1},
        "documents": {consistency_service.UNKNOWN_BACKEND: 2},
    }


def test_a_stamped_paper_never_indexed_is_not_reported_as_a_stamp_problem(factory) -> None:  # noqa: F811
    """Nothing indexed yet: ``missing_index`` speaks, the stamp stays quiet."""
    paper_id = add_paper(factory, chunks=2, parser_backend="pypdf")
    report = run(factory, FakeStorage([object_key(paper_id)]), FakeClient({}))
    assert problems_by_paper(report)[paper_id] == [
        consistency_service.ISSUE_MISSING_INDEX
    ]


def test_document_census_counts_the_remainder_as_unknown(factory) -> None:  # noqa: F811
    """A sub-aggregation that comes back short must not lose documents."""
    paper_id = add_paper(factory, chunks=5, parser_backend="docling")
    report = run(
        factory,
        FakeStorage([object_key(paper_id)]),
        FakeClient({paper_id: 5}, backends={paper_id: {"docling": 3}}),
    )
    assert backend_census(report)["documents"] == {
        "docling": 3,
        consistency_service.UNKNOWN_BACKEND: 2,
    }


def test_deleted_papers_stay_out_of_the_backend_census(factory) -> None:  # noqa: F811
    """The census describes what is live; deletion is not a parse."""
    live_id = add_paper(factory, chunks=1, parser_backend="docling")
    add_paper(factory, chunks=0, parser_backend="pypdf", deleted=True)
    report = run(
        factory,
        FakeStorage([object_key(live_id)]),
        FakeClient({live_id: 1}, backends={live_id: {"docling": 1}}),
    )
    assert backend_census(report)["papers"] == {"docling": 1}
