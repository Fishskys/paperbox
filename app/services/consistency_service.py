"""Three-way consistency check: PostgreSQL vs MinIO vs OpenSearch.

The three stores hold different projections of the same paper and they drift for
ordinary operational reasons: a delete that purged one side but failed on the
other, a reindex that was interrupted, an object removed by hand, documents left
behind by an index migration. This module answers one question per paper -- *does
the database agree with the object store and with the search index?* -- and it is
strictly **read-only**: it never deletes, re-indexes or repairs anything, so it is
safe to call on a live system (``scripts/check_consistency.py`` and
``GET /api/consistency`` are the two callers).

What is compared, per paper:

* ``paper_files`` (live rows) against objects under ``papers/<paper_id>/`` -- by
  exact ``object_key``, not by count, so a leftover figure or a hand-uploaded file
  shows up as an orphan instead of skewing the totals.
* ``paper_chunks`` (rows) against documents grouped by ``paper_id`` in the index.

A store that cannot be reached is reported in ``errors`` and that side is marked
missing rather than raising: an unreachable MinIO must not make the endpoint
useless for the other two. This mirrors the rule ``/health`` follows.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.core.logging import get_logger
from app.db.models import Paper, PaperChunk, PaperFile
from app.db.session import SessionLocal
from app.search import opensearch
from app.services import object_storage

logger = get_logger(__name__)

# ---- issue codes (stable strings: callers switch on them) ------------------ #

#: A live ``paper_files`` row has no object in MinIO.
ISSUE_MISSING_OBJECT = "missing_object"
#: An object under the paper's prefix matches no live ``paper_files`` row.
ISSUE_ORPHAN_OBJECT = "orphan_object"
#: An ``INDEXED`` paper has no chunk rows at all.
ISSUE_MISSING_CHUNKS = "missing_chunks"
#: Chunks exist in PostgreSQL but the index holds no document for the paper.
ISSUE_MISSING_INDEX = "missing_index"
#: The paper has no chunk rows but the index still serves documents for it.
ISSUE_ORPHAN_INDEX = "orphan_index"
#: Both sides have documents, in different numbers (a partial index or delete).
ISSUE_CHUNK_MISMATCH = "chunk_count_mismatch"
#: A soft-deleted paper still owns documents and/or objects.
ISSUE_DELETED_RESIDUE = "deleted_paper_residue"

#: Prefixes owned by a paper / by the upload staging area.
OBJECT_PREFIX = f"{object_storage.ORIGINAL_PREFIX}/"
STAGING_PREFIX = f"{object_storage.UPLOAD_PREFIX}/"

#: The status a paper reaches once its chunks are indexed (chunks expected).
INDEXED_STATUS = "INDEXED"

#: Papers whose issues are listed in full; the totals always count everything.
DEFAULT_PROBLEM_LIMIT = 200
#: ``terms`` aggregation size for ``paper_id`` -- above this the report is marked
#: truncated instead of silently dropping papers.
AGG_SIZE = 10000
#: How many orphan keys/ids to spell out in the report (totals stay exact).
ORPHAN_SAMPLE = 100


@dataclass
class PaperConsistency:
    """One paper's three-way state (only built for papers that have issues)."""

    paper_id: str
    title: str | None
    status: str
    deleted: bool
    files_pg: int
    objects_minio: int
    chunks_pg: int
    chunks_os: int
    issues: list[str] = field(default_factory=list)
    missing_objects: list[str] = field(default_factory=list)
    orphan_objects: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "status": self.status,
            "deleted": self.deleted,
            "files_pg": self.files_pg,
            "objects_minio": self.objects_minio,
            "chunks_pg": self.chunks_pg,
            "chunks_os": self.chunks_os,
            "issues": list(self.issues),
            "missing_objects": list(self.missing_objects),
            "orphan_objects": list(self.orphan_objects),
        }


@dataclass
class ConsistencyReport:
    """The whole answer: store totals, per-paper problems, orphans, errors."""

    index: str
    index_exists: bool
    papers_total: int
    papers_live: int
    papers_deleted: int
    files_pg: int
    objects_minio: int
    chunks_pg: int
    documents_os: int
    staging_objects: int
    problems: list[PaperConsistency]
    problems_total: int
    orphan_objects: list[str]
    orphan_objects_total: int
    orphan_documents: list[str]
    orphan_documents_total: int
    errors: list[str]
    truncated: bool
    took_ms: float
    checked_at: str = ""

    @property
    def consistent(self) -> bool:
        """True when nothing drifted and every store answered."""
        return not (
            self.problems_total
            or self.orphan_objects_total
            or self.orphan_documents_total
            or self.errors
            or self.truncated
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "consistent": self.consistent,
            "index": self.index,
            "index_exists": self.index_exists,
            "totals": {
                "papers": self.papers_total,
                "papers_live": self.papers_live,
                "papers_deleted": self.papers_deleted,
                "files_pg": self.files_pg,
                "objects_minio": self.objects_minio,
                "chunks_pg": self.chunks_pg,
                "documents_os": self.documents_os,
                "staging_objects": self.staging_objects,
                "problems": self.problems_total,
                "orphan_objects": self.orphan_objects_total,
                "orphan_documents": self.orphan_documents_total,
            },
            "problems": [problem.as_dict() for problem in self.problems],
            "orphan_objects": list(self.orphan_objects),
            "orphan_documents": list(self.orphan_documents),
            "errors": list(self.errors),
            "truncated": self.truncated,
            "took_ms": self.took_ms,
        }


# --------------------------------------------------------------------------- #
# one loader per store
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _PaperRow:
    paper_id: str
    title: str | None
    status: str
    deleted: bool


def _load_papers(session_factory: Callable[[], Any]) -> tuple[dict[str, _PaperRow], dict[str, list[str]], dict[str, int]]:
    """PostgreSQL side: papers, their live file object keys and chunk counts."""
    session = session_factory()
    try:
        papers: dict[str, _PaperRow] = {}
        for paper_id, title, status, deleted_at in session.execute(
            select(Paper.id, Paper.title, Paper.status, Paper.deleted_at)
        ).all():
            papers[str(paper_id)] = _PaperRow(
                paper_id=str(paper_id),
                title=title,
                status=str(status or ""),
                deleted=deleted_at is not None,
            )
        files: dict[str, list[str]] = {}
        for paper_id, object_key in session.execute(
            select(PaperFile.paper_id, PaperFile.object_key).where(
                PaperFile.deleted_at.is_(None)
            )
        ).all():
            files.setdefault(str(paper_id), []).append(str(object_key))
        chunks: dict[str, int] = {
            str(paper_id): int(count)
            for paper_id, count in session.execute(
                select(PaperChunk.paper_id, func.count()).group_by(PaperChunk.paper_id)
            ).all()
        }
    finally:
        session.close()
    return papers, files, chunks


def _load_objects(storage: Any) -> tuple[dict[str, list[str]], int]:
    """MinIO side: object keys per paper id, plus the staging object count."""
    per_paper: dict[str, list[str]] = {}
    staging = 0
    for obj in storage.list_objects(prefix=OBJECT_PREFIX):
        key = str(getattr(obj, "object_name", obj))
        remainder = key[len(OBJECT_PREFIX) :]
        if not remainder or "/" not in remainder:
            # Not laid out as papers/<paper_id>/<file>: count it as an orphan.
            per_paper.setdefault("", []).append(key)
            continue
        per_paper.setdefault(remainder.split("/", 1)[0], []).append(key)
    for obj in storage.list_objects(prefix=STAGING_PREFIX):
        staging += 1
    return per_paper, staging


def _load_documents(client: Any, alias: str) -> tuple[dict[str, int], bool, bool]:
    """OpenSearch side: documents per ``paper_id``.

    Returns ``(counts, index_exists, truncated)``. One ``terms`` aggregation is
    enough for the whole corpus, so this stays a single round trip.
    """
    exists = opensearch.index_exists(client, alias)
    if not exists:
        return {}, False, False
    client = client or opensearch.get_client()
    response = client.search(
        index=alias,
        body={
            "size": 0,
            "aggs": {"by_paper": {"terms": {"field": "paper_id", "size": AGG_SIZE}}},
        },
    )
    buckets = response.get("aggregations", {}).get("by_paper", {}).get("buckets", [])
    counts = {str(bucket["key"]): int(bucket["doc_count"]) for bucket in buckets}
    return counts, True, len(buckets) >= AGG_SIZE


# --------------------------------------------------------------------------- #
# the check
# --------------------------------------------------------------------------- #


def _compare_paper(
    row: _PaperRow,
    *,
    file_keys: Sequence[str],
    objects: Sequence[str],
    chunks_pg: int,
    chunks_os: int,
) -> PaperConsistency | None:
    """Compare one paper across the three stores; ``None`` when it agrees."""
    expected = set(file_keys)
    actual = set(objects)

    if row.deleted:
        # Deletion purges documents and objects inline (``DELETE /api/papers/{id}``)
        # while the ``paper_files`` rows stay behind on purpose (soft delete), so a
        # file without its object is the *expected* state here. The only thing worth
        # reporting is what the purge left behind.
        issues: list[str] = []
        if actual or chunks_os > 0:
            issues.append(ISSUE_DELETED_RESIDUE)
        if not issues:
            return None
        return PaperConsistency(
            paper_id=row.paper_id,
            title=row.title,
            status=row.status,
            deleted=True,
            files_pg=len(expected),
            objects_minio=len(actual),
            chunks_pg=chunks_pg,
            chunks_os=chunks_os,
            issues=issues,
            orphan_objects=sorted(actual),
        )

    missing_objects = sorted(expected - actual)
    orphan_objects = sorted(actual - expected)

    issues = []
    if missing_objects:
        issues.append(ISSUE_MISSING_OBJECT)
    if orphan_objects:
        issues.append(ISSUE_ORPHAN_OBJECT)

    if chunks_pg and not chunks_os:
        issues.append(ISSUE_MISSING_INDEX)
    elif chunks_pg and chunks_os != chunks_pg:
        issues.append(ISSUE_CHUNK_MISMATCH)
    elif not chunks_pg and chunks_os:
        issues.append(ISSUE_ORPHAN_INDEX)
    elif not chunks_pg and row.status == INDEXED_STATUS:
        issues.append(ISSUE_MISSING_CHUNKS)

    if not issues:
        return None
    return PaperConsistency(
        paper_id=row.paper_id,
        title=row.title,
        status=row.status,
        deleted=row.deleted,
        files_pg=len(expected),
        objects_minio=len(actual),
        chunks_pg=chunks_pg,
        chunks_os=chunks_os,
        issues=issues,
        missing_objects=missing_objects,
        orphan_objects=orphan_objects,
    )


def check_consistency(
    session_factory: Callable[[], Any] = SessionLocal,
    *,
    storage: Any = object_storage,
    client: Any = None,
    alias: str = opensearch.ALIAS,
    limit: int = DEFAULT_PROBLEM_LIMIT,
) -> ConsistencyReport:
    """Run the three-way check and return the report (read-only, never raises).

    ``storage``/``client``/``session_factory`` are injectable so the check can be
    unit-tested without MinIO, OpenSearch or PostgreSQL.
    """
    started = time.perf_counter()
    errors: list[str] = []
    problems: list[PaperConsistency] = []

    papers: dict[str, _PaperRow] = {}
    files: dict[str, list[str]] = {}
    chunks: dict[str, int] = {}
    try:
        papers, files, chunks = _load_papers(session_factory)
    except Exception as exc:  # noqa: BLE001 - one broken store must not hide the rest
        logger.warning("consistency: could not read PostgreSQL: %s", exc)
        errors.append(f"postgres: {type(exc).__name__}: {exc}")

    objects_per_paper: dict[str, list[str]] = {}
    staging = 0
    try:
        objects_per_paper, staging = _load_objects(storage)
    except Exception as exc:  # noqa: BLE001
        logger.warning("consistency: could not list MinIO objects: %s", exc)
        errors.append(f"minio: {type(exc).__name__}: {exc}")

    documents: dict[str, int] = {}
    index_exists = False
    truncated = False
    try:
        documents, index_exists, truncated = _load_documents(client, alias)
    except Exception as exc:  # noqa: BLE001
        logger.warning("consistency: could not read OpenSearch: %s", exc)
        errors.append(f"opensearch: {type(exc).__name__}: {exc}")

    if truncated:
        errors.append(
            f"opensearch: paper_id aggregation hit its size limit ({AGG_SIZE}); "
            "document counts are incomplete"
        )

    for paper_id, row in sorted(papers.items()):
        problem = _compare_paper(
            row,
            file_keys=files.get(paper_id, []),
            objects=objects_per_paper.get(paper_id, []),
            chunks_pg=chunks.get(paper_id, 0),
            chunks_os=documents.get(paper_id, 0),
        )
        if problem is not None:
            problems.append(problem)

    # Objects under a paper prefix that has no row at all (or a malformed key).
    orphan_objects = sorted(
        key
        for paper_id, keys in objects_per_paper.items()
        if paper_id not in papers
        for key in keys
    )
    # Documents for a paper that PostgreSQL does not know about.
    orphan_documents = sorted(
        paper_id for paper_id in documents if paper_id not in papers
    )

    took_ms = round((time.perf_counter() - started) * 1000, 3)
    report = ConsistencyReport(
        index=alias,
        index_exists=index_exists,
        papers_total=len(papers),
        papers_live=sum(1 for row in papers.values() if not row.deleted),
        papers_deleted=sum(1 for row in papers.values() if row.deleted),
        files_pg=sum(len(keys) for keys in files.values()),
        objects_minio=sum(len(keys) for keys in objects_per_paper.values()),
        chunks_pg=sum(chunks.values()),
        documents_os=sum(documents.values()),
        staging_objects=staging,
        problems=problems[: max(1, int(limit))],
        problems_total=len(problems),
        orphan_objects=orphan_objects[:ORPHAN_SAMPLE],
        orphan_objects_total=len(orphan_objects),
        orphan_documents=orphan_documents[:ORPHAN_SAMPLE],
        orphan_documents_total=len(orphan_documents),
        errors=errors,
        truncated=truncated,
        took_ms=took_ms,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
    logger.info(
        "consistency check finished",
        extra={
            "extra_fields": {
                "papers": report.papers_total,
                "problems": report.problems_total,
                "orphan_objects": report.orphan_objects_total,
                "orphan_documents": report.orphan_documents_total,
                "errors": len(errors),
                "took_ms": took_ms,
            }
        },
    )
    return report


__all__ = [
    "DEFAULT_PROBLEM_LIMIT",
    "ISSUE_CHUNK_MISMATCH",
    "ISSUE_DELETED_RESIDUE",
    "ISSUE_MISSING_CHUNKS",
    "ISSUE_MISSING_INDEX",
    "ISSUE_MISSING_OBJECT",
    "ISSUE_ORPHAN_INDEX",
    "ISSUE_ORPHAN_OBJECT",
    "ConsistencyReport",
    "PaperConsistency",
    "check_consistency",
]
