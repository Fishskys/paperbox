"""Fingerprint priority and the post-parse upgrade (SPEC-P1 section C).

``build_fingerprint`` is a pure function; ``_upgrade_fingerprint`` and
``_discard_duplicate_paper`` are driven against fakes so the duplicate branch can
be pinned without PostgreSQL, OpenSearch or MinIO.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.db.models import Paper, PaperChunk, PaperFile
from app.services import paper_service
from app.workers import tasks
from app.workers.tasks import (
    _discard_duplicate_paper,
    _original_sha256,
    _upgrade_fingerprint,
)

SHA = "a" * 64


def make_paper(**overrides) -> Paper:
    values = {
        "id": "11111111-1111-1111-1111-111111111111",
        "title": "Low Power SRAM Leakage Reduction",
        "fingerprint": f"sha256:{SHA}",
        "doi": None,
        "arxiv_id": None,
        "year": None,
    }
    values.update(overrides)
    paper = Paper(**values)
    paper.created_at = datetime.now(timezone.utc)
    paper.updated_at = paper.created_at
    return paper


# --------------------------------------------------------------------------- #
# build_fingerprint priority (pure)
# --------------------------------------------------------------------------- #
def test_doi_wins_over_everything_else() -> None:
    fingerprint = paper_service.build_fingerprint(
        doi="10.1109/JSSC.2020.1234567",
        arxiv_id="1710.07153",
        title="Low Power SRAM",
        first_author="Alice",
        year=2021,
        sha256=SHA,
    )

    assert fingerprint == "doi:10.1109/jssc.2020.1234567"


def test_arxiv_wins_when_there_is_no_doi() -> None:
    fingerprint = paper_service.build_fingerprint(
        arxiv_id="1710.07153v2",
        title="Low Power SRAM",
        first_author="Alice",
        year=2021,
        sha256=SHA,
    )

    assert fingerprint == "arxiv:1710.07153"


def test_title_author_year_when_neither_id_is_known() -> None:
    fingerprint = paper_service.build_fingerprint(
        title="Low Power SRAM",
        first_author="Alice",
        year=2021,
        sha256=SHA,
    )

    assert fingerprint == "title:low power sram|alice|2021"


def test_sha256_is_the_last_resort() -> None:
    assert paper_service.build_fingerprint(sha256=SHA) == f"sha256:{SHA}"
    assert paper_service.build_fingerprint(title="T", sha256=SHA) == f"sha256:{SHA}"
    # title without an author or year cannot form the title fingerprint
    assert paper_service.build_fingerprint(title="T", first_author="A") == (
        paper_service.build_fingerprint(title="T")
    )


def test_doi_normalization_ignores_case_space_and_url_prefix() -> None:
    expected = "doi:10.1109/jssc.2020.1234567"

    for raw in (
        "10.1109/JSSC.2020.1234567",
        "  10.1109/jssc.2020.1234567  ",
        "https://doi.org/10.1109/JSSC.2020.1234567",
        "doi:10.1109/JSSC.2020.1234567",
    ):
        assert paper_service.build_fingerprint(doi=raw) == expected


def test_arxiv_normalization_strips_prefix_and_version() -> None:
    expected = "arxiv:1710.07153"

    for raw in ("1710.07153", "1710.07153v3", "arXiv:1710.07153", "https://arxiv.org/abs/1710.07153"):
        assert paper_service.build_fingerprint(arxiv_id=raw) == expected


def test_title_author_year_fingerprint_is_case_insensitive() -> None:
    upper = paper_service.build_fingerprint(
        title="LOW POWER SRAM", first_author="ALICE", year=2021
    )
    lower = paper_service.build_fingerprint(
        title="low power sram", first_author="alice", year=2021
    )

    assert upper == lower


def test_empty_strings_do_not_produce_a_fingerprint() -> None:
    assert paper_service.build_fingerprint(doi=" ", arxiv_id="", sha256=SHA) == f"sha256:{SHA}"


def test_fingerprint_priority_ladder_is_ordered() -> None:
    """DOI > arXiv > title|author|year > sha256, checked pairwise."""
    full = dict(
        doi="10.1/x",
        arxiv_id="1710.07153",
        title="T",
        first_author="A",
        year=2020,
        sha256=SHA,
    )
    assert paper_service.build_fingerprint(**full).startswith("doi:")
    without_doi = {k: v for k, v in full.items() if k != "doi"}
    assert paper_service.build_fingerprint(**without_doi).startswith("arxiv:")
    without_arxiv = {k: v for k, v in without_doi.items() if k != "arxiv_id"}
    assert paper_service.build_fingerprint(**without_arxiv).startswith("title:")
    minimal = {"sha256": SHA}
    assert paper_service.build_fingerprint(**minimal) == f"sha256:{SHA}"


# --------------------------------------------------------------------------- #
# _original_sha256
# --------------------------------------------------------------------------- #
def attach_file(paper: Paper, sha256, kind: str = "original") -> PaperFile:
    """Attach a real ``PaperFile`` row to an in-memory paper."""
    record = PaperFile(
        id="44444444-4444-4444-4444-444444444444",
        paper_id=paper.id,
        kind=kind,
        object_key=f"papers/{paper.id}/original.pdf",
        bucket="paperbox",
        filename="original.pdf",
        content_type="application/pdf",
        size_bytes=10,
        sha256=sha256,
    )
    paper.files = [record]
    return record


def test_original_sha256_reads_the_original_file() -> None:
    paper = make_paper()
    attach_file(paper, SHA)

    assert _original_sha256(paper) == SHA


def test_original_sha256_reads_a_non_original_file_too() -> None:
    """``original_file`` only matches kind=original, so the fallback matters."""
    paper = make_paper()
    attach_file(paper, SHA, kind="supplementary")

    assert _original_sha256(paper) == SHA


def test_original_sha256_is_none_without_files() -> None:
    paper = make_paper()
    paper.files = []

    assert _original_sha256(paper) is None


def test_original_sha256_is_none_when_the_digest_is_blank() -> None:
    paper = make_paper()
    attach_file(paper, "   ")

    assert _original_sha256(paper) is None


def test_original_sha256_is_none_when_the_digest_is_missing() -> None:
    paper = make_paper()
    attach_file(paper, None)

    assert _original_sha256(paper) is None


# --------------------------------------------------------------------------- #
# _upgrade_fingerprint
# --------------------------------------------------------------------------- #
class UpgradeSession:
    """Minimal session double: records flushes and commits, never touches a DB."""

    def __init__(self, conflict: Paper | None = None, fail_on_flush: bool = False) -> None:
        self.conflict = conflict
        self.fail_on_flush = fail_on_flush
        self.flushes = 0
        self.rollbacks = 0
        self.queries = 0

    def flush(self) -> None:
        self.flushes += 1
        if self.fail_on_flush:
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("UPDATE papers", {}, Exception("duplicate key"))

    def begin_nested(self):
        """Savepoint double: an exception inside rolls the savepoint back.

        The production code no longer calls ``session.rollback()`` on a lost
        fingerprint race — it wraps the flip in a SAVEPOINT (review 2026-10-05,
        P1-1). The double maps the savepoint's exception path onto the same
        ``rollbacks`` counter the old assertions use.
        """
        session = self

        class _Savepoint:
            def __enter__(self):
                return session

            def __exit__(self, exc_type, exc, tb):
                if exc is not None:
                    session.rollback()
                return False

        return _Savepoint()

    def rollback(self) -> None:
        self.rollbacks += 1

    #: Lookup answers, consumed in call order. The first element is the
    #: pre-flush conflict check; the rest are the IntegrityError fallback.
    results: tuple[Paper | None, ...] = ()

    def execute(self, statement):
        """Answer the ``_find_other_live_paper_by_fingerprint`` lookup.

        ``results`` is consumed in order so a test can say "no conflict on the
        first check, then a conflict once the flush lost the race".
        """
        index = min(self.queries, len(self.results) - 1) if self.results else 0
        value = self.results[index] if self.results else self.conflict
        self.queries += 1

        class _Result:
            def scalars(self):
                return self

            def first(self):
                return value

        return _Result()


def test_upgrade_returns_none_when_the_fingerprint_is_unchanged() -> None:
    paper = make_paper(arxiv_id=None, doi=None)
    session = UpgradeSession()
    before = paper.fingerprint

    assert _upgrade_fingerprint(session, paper, sha256=SHA) is None

    assert paper.fingerprint == before
    assert session.flushes == 0


def test_upgrade_writes_the_new_fingerprint_when_free(monkeypatch) -> None:
    paper = make_paper(arxiv_id="arXiv:1710.07153", doi=None)
    session = UpgradeSession(conflict=None)

    assert _upgrade_fingerprint(session, paper, sha256=SHA) is None

    assert paper.fingerprint == "arxiv:1710.07153"
    assert session.flushes == 1


def test_upgrade_returns_the_conflicting_live_paper() -> None:
    other = make_paper(id="22222222-2222-2222-2222-222222222222", fingerprint="arxiv:1710.07153")
    paper = make_paper(arxiv_id="1710.07153", doi=None)
    session = UpgradeSession(conflict=other)

    conflict = _upgrade_fingerprint(session, paper, sha256=SHA)

    assert conflict is other
    assert paper.fingerprint == f"sha256:{SHA}", "the row must stay untouched"
    assert session.flushes == 0


def test_upgrade_falls_back_to_the_duplicate_path_on_integrity_error() -> None:
    """A lost flush race must end in the duplicate branch, not a crash."""
    other = make_paper(id="33333333-3333-3333-3333-333333333333", fingerprint="doi:10.1/x")
    paper = make_paper(doi="10.1/x")
    # First lookup finds nothing, the flush raises, the fallback lookup finds it.
    session = UpgradeSession(fail_on_flush=True)
    session.results = (None, other)

    conflict = _upgrade_fingerprint(session, paper, sha256=SHA)

    assert conflict is other
    assert session.rollbacks == 1
    assert session.queries == 2


def test_upgrade_reraises_when_the_race_leaves_no_visible_conflict() -> None:
    """Nothing to fall back to -> the IntegrityError must surface."""
    from sqlalchemy.exc import IntegrityError

    paper = make_paper(doi="10.1/x")
    session = UpgradeSession(fail_on_flush=True)
    session.results = (None, None)

    with pytest.raises(IntegrityError):
        _upgrade_fingerprint(session, paper, sha256=SHA)

    assert session.rollbacks == 1


def test_upgrade_prefers_doi_over_the_sha256_fingerprint() -> None:
    paper = make_paper(doi="https://doi.org/10.1109/JSSC.2020.1234567")
    session = UpgradeSession(conflict=None)

    assert _upgrade_fingerprint(session, paper, sha256=SHA) is None

    assert paper.fingerprint == "doi:10.1109/jssc.2020.1234567"


def test_upgrade_keeps_the_old_fingerprint_when_a_reindex_hits_a_conflict() -> None:
    """Reindex must never discard a live paper (``discard_on_conflict=False``).

    Regression: the legacy paper that already holds ``arxiv_id`` keeps its
    ``sha256:`` fingerprint, while a paper ingested later claims
    ``arxiv:<id>``. Reindexing the legacy paper then hits that conflict; it has
    to be a no-op (fingerprint untouched, nothing flagged for discard) instead of
    purging the paper that is being reindexed.
    """
    other = make_paper(
        id="22222222-2222-2222-2222-222222222222",
        arxiv_id="1706.03762",
        fingerprint="arxiv:1706.03762",
    )
    paper = make_paper(arxiv_id="1706.03762")
    session = UpgradeSession(conflict=other)

    conflict = _upgrade_fingerprint(
        session, paper, sha256=SHA, discard_on_conflict=False
    )

    assert conflict is None
    assert paper.fingerprint == f"sha256:{SHA}"


def test_upgrade_still_reports_the_conflict_for_a_fresh_ingest() -> None:
    other = make_paper(
        id="22222222-2222-2222-2222-222222222222",
        arxiv_id="1706.03762",
        fingerprint="arxiv:1706.03762",
    )
    paper = make_paper(arxiv_id="1706.03762")
    session = UpgradeSession(conflict=other)

    assert _upgrade_fingerprint(session, paper, sha256=SHA) is other


# --------------------------------------------------------------------------- #
# _discard_duplicate_paper
# --------------------------------------------------------------------------- #
class DiscardSession:
    def __init__(self, paper: Paper) -> None:
        self.paper = paper
        self.deleted_queries: list[str] = []
        self.commits = 0
        self.flushes = 0

    def query(self, model):
        assert model is PaperChunk
        outer = self

        class _Query:
            def filter(self, *args, **kwargs):
                return self

            def delete(self, **kwargs):
                outer.deleted_queries.append(str(model.__tablename__))
                return 4

        return _Query()

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1


class FakeJob:
    def __init__(self) -> None:
        self.id = "job-1"
        self.paper_id = None
        self.stage = "CHUNKING"
        self.progress = 60.0
        self.payload = {}
        self.error_message = None
        self.finished_at = None


def test_discard_duplicate_paper_cleans_up_and_marks_duplicate(monkeypatch) -> None:
    paper = make_paper()
    existing = make_paper(id="99999999-9999-9999-9999-999999999999", fingerprint="arxiv:1710.07153")
    job = FakeJob()
    session = DiscardSession(paper)

    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        tasks.opensearch,
        "delete_by_paper_id",
        lambda paper_id, **kwargs: calls.append(("opensearch", paper_id)) or 4,
    )
    monkeypatch.setattr(
        tasks.object_storage,
        "delete_prefix",
        lambda paper_id, **kwargs: calls.append(("minio", paper_id)) or 2,
    )
    monkeypatch.setattr(
        tasks.paper_service,
        "soft_delete_paper",
        lambda session_, paper_: calls.append(("soft_delete", paper_.id)) or paper_,
    )
    monkeypatch.setattr(
        tasks.ingest,
        "resolve_duplicate",
        lambda session_, existing_, job_, **kwargs: calls.append(("duplicate", existing_.id)) or job_,
    )

    _discard_duplicate_paper(session, paper, job, existing)

    assert session.deleted_queries == ["paper_chunks"]
    assert calls == [
        ("opensearch", paper.id),
        ("minio", paper.id),
        ("soft_delete", paper.id),
        ("duplicate", existing.id),
    ]
    assert session.commits == 1


def test_discard_duplicate_paper_survives_cleanup_failures(monkeypatch) -> None:
    paper = make_paper()
    existing = make_paper(id="88888888-8888-8888-8888-888888888888")
    job = FakeJob()
    session = DiscardSession(paper)

    def boom(*args, **kwargs):
        raise RuntimeError("service down")

    monkeypatch.setattr(tasks.opensearch, "delete_by_paper_id", boom)
    monkeypatch.setattr(tasks.object_storage, "delete_prefix", boom)
    soft_deleted: list[str] = []
    monkeypatch.setattr(
        tasks.paper_service,
        "soft_delete_paper",
        lambda session_, paper_: soft_deleted.append(paper_.id) or paper_,
    )
    monkeypatch.setattr(
        tasks.ingest, "resolve_duplicate", lambda session_, existing_, job_, **kwargs: job_
    )

    _discard_duplicate_paper(session, paper, job, existing)  # must not raise

    assert soft_deleted == [paper.id]
    assert session.commits == 1
