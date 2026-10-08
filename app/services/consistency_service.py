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
* The embedding model that produced the vectors, on both sides (the
  ``embedding_model`` column and the document field): one paper must not
  straddle two models, and a model switch that only reached part of the
  library shows up in the census.

A store that cannot be reached is reported in ``errors`` and that side is marked
missing rather than raising: an unreachable MinIO must not make the endpoint
useless for the other two. This mirrors the rule ``/health`` follows.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
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
#: The parser stamp on ``papers`` and the ``parser_backend`` carried by that
#: paper's documents disagree, or the documents are split across backends: the
#: library mixes parsers in a state that was never committed as a whole (plan
#: §6.1 step 2). A missing stamp on either side counts as ``unknown``, so papers
#: indexed before the stamp existed are *not* reported.
ISSUE_PARSER_STAMP_MISMATCH = "parser_stamp_mismatch"
#: One paper's vectors were embedded by more than one model (its PostgreSQL
#: chunk rows, its index documents, or the two sides disagree). Vectors from
#: different models are not comparable, so one paper -- and one index -- must
#: never straddle two. Rows/documents with no model recorded count as
#: ``unknown`` and are never a mismatch on their own: the library indexed
#: before the column existed looks like that, and a model switch that has not
#: started yet is a *census* fact, not per-paper drift.
ISSUE_EMBEDDING_MODEL_MISMATCH = "embedding_model_mismatch"

#: Prefixes owned by a paper / by the upload staging area.
OBJECT_PREFIX = f"{object_storage.ORIGINAL_PREFIX}/"
STAGING_PREFIX = f"{object_storage.UPLOAD_PREFIX}/"

#: The parse-artifact folder under a paper's prefix (``papers/<id>/extracted/``).
#: Those objects are the parse cache (``PARSER_CACHE``, T7.1): legitimate, never
#: registered in ``paper_files``, and therefore **not** orphans. They used to be
#: counted as orphans, which turned the whole report red as soon as docling +
#: cache became the default (2026-09-30: 30 papers, one ``orphan_object`` each).
#: They are counted separately (``cache_objects``) so nothing is hidden.
CACHE_SEGMENT = "extracted/"

#: The status a paper reaches once its chunks are indexed (chunks expected).
INDEXED_STATUS = "INDEXED"

#: Papers whose issues are listed in full; the totals always count everything.
DEFAULT_PROBLEM_LIMIT = 200
#: ``terms`` aggregation size for ``paper_id`` -- above this the report is marked
#: truncated instead of silently dropping papers.
AGG_SIZE = 10000
#: How many orphan keys/ids to spell out in the report (totals stay exact).
ORPHAN_SAMPLE = 100

#: Census key for rows/documents that carry no parser stamp at all.
UNKNOWN_BACKEND = "unknown"

#: Cap on the opt-in per-backend paper id lists (``with_parser_papers``): the
#: worklist for a backend switch is a few hundred ids, not an export dump.
PARSER_PAPER_ID_LIMIT = 2000


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
    #: Parse artifacts under ``papers/<id>/extracted/`` (the parse cache). Not
    #: drift, not orphans -- reported so the count stays visible.
    cache_objects: int = 0
    #: What PostgreSQL stamps for the paper (``None`` = indexed before the stamp).
    parser_backend: str | None = None
    #: What the paper's index documents carry, sorted. Empty = no documents, or
    #: documents written before the stamp existed.
    index_backends: tuple[str, ...] = ()
    #: Every *known* embedding model seen on the paper's chunk rows and index
    #: documents, sorted. One entry = agree; two or more =
    #: :data:`ISSUE_EMBEDDING_MODEL_MISMATCH`.
    embedding_models: tuple[str, ...] = ()

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
            "cache_objects": self.cache_objects,
            "parser_backend": self.parser_backend,
            "index_backends": list(self.index_backends),
            "embedding_models": list(self.embedding_models),
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
    #: Parse-cache objects across the corpus (``papers/<id>/extracted/...``):
    #: legitimate, counted, never a problem.
    cache_objects_total: int
    #: Backend census, keyed by backend name (or ``unknown``): what PostgreSQL
    #: stamps for its live papers, and what the index documents carry.
    parser_backends_papers: dict[str, int]
    parser_backends_documents: dict[str, int]
    #: Embedding-model census: chunk rows in PostgreSQL vs index documents,
    #: keyed by model (or ``unknown``). A split here means a model switch that
    #: only reached part of the library -- the vectors are not comparable.
    embedding_models_chunks: dict[str, int] = field(default_factory=dict)
    embedding_models_documents: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    #: True when the ``paper_id`` terms aggregation hit :data:`AGG_SIZE`.
    truncated: bool = False
    took_ms: float = 0.0
    #: Opt-in (``with_parser_papers``): the live paper ids behind each stamp, so
    #: "docling is back, re-parse the fallbacks" has a worklist, not just a count.
    parser_backends_paper_ids: dict[str, list[str]] = field(default_factory=dict)
    #: True when the id lists above hit :data:`PARSER_PAPER_ID_LIMIT`.
    parser_backends_paper_ids_truncated: bool = False
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
                "cache_objects": self.cache_objects_total,
            },
            "parser_backends": {
                "papers": dict(sorted(self.parser_backends_papers.items())),
                "documents": dict(sorted(self.parser_backends_documents.items())),
                "paper_ids": {
                    key: list(value)
                    for key, value in sorted(self.parser_backends_paper_ids.items())
                },
                "paper_ids_truncated": self.parser_backends_paper_ids_truncated,
            },
            "embedding_models": {
                "chunks": dict(sorted(self.embedding_models_chunks.items())),
                "documents": dict(sorted(self.embedding_models_documents.items())),
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
    parser_backend: str | None = None
    parser_version: str | None = None


def _load_papers(
    session_factory: Callable[[], Any],
) -> tuple[
    dict[str, _PaperRow],
    dict[str, list[str]],
    dict[str, int],
    dict[str, dict[str | None, int]],
]:
    """PostgreSQL side: papers, their live file object keys, chunk counts and
    the embedding model each paper's chunk rows carry."""
    session = session_factory()
    try:
        papers: dict[str, _PaperRow] = {}
        for paper_id, title, status, deleted_at, backend, version in session.execute(
            select(
                Paper.id,
                Paper.title,
                Paper.status,
                Paper.deleted_at,
                Paper.parser_backend,
                Paper.parser_version,
            )
        ).all():
            papers[str(paper_id)] = _PaperRow(
                paper_id=str(paper_id),
                title=title,
                status=str(status or ""),
                deleted=deleted_at is not None,
                parser_backend=backend,
                parser_version=version,
            )
        files: dict[str, list[str]] = {}
        for paper_id, object_key in session.execute(
            select(PaperFile.paper_id, PaperFile.object_key).where(
                PaperFile.deleted_at.is_(None)
            )
        ).all():
            files.setdefault(str(paper_id), []).append(str(object_key))
        # One group-by feeds both the chunk counts and the embedding-model
        # census: count per (paper, model), then sum per paper.
        chunk_models: dict[str, dict[str | None, int]] = {}
        model_rows = session.execute(
            select(
                PaperChunk.paper_id,
                PaperChunk.embedding_model,
                func.count(),
            ).group_by(PaperChunk.paper_id, PaperChunk.embedding_model)
        ).all()
        for paper_id, model, count in model_rows:
            per_paper = chunk_models.setdefault(str(paper_id), {})
            per_paper[model] = per_paper.get(model, 0) + int(count)
        chunks: dict[str, int] = {
            paper_id: sum(models.values()) for paper_id, models in chunk_models.items()
        }
    finally:
        session.close()
    return papers, files, chunks, chunk_models


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


def _load_documents(
    client: Any, alias: str
) -> tuple[dict[str, int], dict[str, dict[str, int]], dict[str, dict[str, int]], bool, bool]:
    """OpenSearch side: documents per ``paper_id``, split by parser backend and
    by the embedding model the documents carry.

    Returns ``(counts, backends, models, index_exists, truncated)``. One
    ``terms`` aggregation with two sub-aggregations is enough for the whole
    corpus, so this stays a single round trip. A paper whose documents predate
    a stamp comes back with an empty mapping -- "no backend / no model", not
    "unknown of a known kind".
    """
    exists = opensearch.index_exists(client, alias)
    if not exists:
        return {}, {}, {}, False, False
    client = client or opensearch.get_client()
    response = client.search(
        index=alias,
        body={
            "size": 0,
            "aggs": {
                "by_paper": {
                    "terms": {"field": "paper_id", "size": AGG_SIZE},
                    "aggs": {
                        "by_backend": {"terms": {"field": "parser_backend"}},
                        "by_model": {"terms": {"field": "embedding_model"}},
                    },
                }
            },
        },
    )
    buckets = response.get("aggregations", {}).get("by_paper", {}).get("buckets", [])
    counts: dict[str, int] = {}
    backends: dict[str, dict[str, int]] = {}
    models: dict[str, dict[str, int]] = {}
    for bucket in buckets:
        paper_id = str(bucket["key"])
        counts[paper_id] = int(bucket["doc_count"])
        for target, key in ((backends, "by_backend"), (models, "by_model")):
            sub = bucket.get(key, {}).get("buckets", [])
            target[paper_id] = {
                str(entry["key"]): int(entry["doc_count"])
                for entry in sub
                if str(entry["key"])
            }
    return counts, backends, models, True, len(buckets) >= AGG_SIZE


# --------------------------------------------------------------------------- #
# the check
# --------------------------------------------------------------------------- #


def _census(values: Iterable[str | None]) -> dict[str, int]:
    """Count live papers per parser backend; ``unknown`` covers "no stamp"."""
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or UNKNOWN_BACKEND)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _census_ids(
    rows: Iterable[tuple[str, str | None]],
) -> tuple[dict[str, list[str]], bool]:
    """Live paper ids grouped by stamp -- the opt-in detail behind ``_census``.

    Sorted per backend so the output is stable, and capped at
    :data:`PARSER_PAPER_ID_LIMIT` (the second element says the cap was hit).
    """
    grouped: dict[str, list[str]] = {}
    truncated = False
    for paper_id, value in rows:
        key = str(value or UNKNOWN_BACKEND)
        bucket = grouped.setdefault(key, [])
        if len(bucket) >= PARSER_PAPER_ID_LIMIT:
            truncated = True
            continue
        bucket.append(str(paper_id))
    return ({key: sorted(grouped[key]) for key in sorted(grouped)}, truncated)


def _document_census(
    documents: Mapping[str, int], backends: Mapping[str, Mapping[str, int]]
) -> dict[str, int]:
    """Count *documents* per backend, not papers: one paper can straddle two."""
    counts: dict[str, int] = {}
    for paper_id, total in documents.items():
        by_backend = dict(backends.get(paper_id) or {})
        accounted = sum(by_backend.values())
        if accounted < int(total):
            # Documents written before the stamp existed, or a sub-aggregation
            # that came back short: either way the remainder is "unknown".
            by_backend[UNKNOWN_BACKEND] = by_backend.get(UNKNOWN_BACKEND, 0) + (
                int(total) - accounted
            )
        for backend, doc_count in by_backend.items():
            counts[backend] = counts.get(backend, 0) + int(doc_count)
    return counts


def _chunk_census(
    chunk_models: Mapping[str, Mapping[str | None, int]],
) -> dict[str, int]:
    """Count *chunk rows* per embedding model (``unknown`` covers NULL)."""
    counts: dict[str, int] = {}
    for per_paper in chunk_models.values():
        for model, count in per_paper.items():
            key = str(model or UNKNOWN_BACKEND)
            counts[key] = counts.get(key, 0) + int(count)
    return counts


def _compare_paper(
    row: _PaperRow,
    *,
    file_keys: Sequence[str],
    objects: Sequence[str],
    chunks_pg: int,
    chunks_os: int,
    index_backends: Mapping[str, int] | None = None,
    chunk_models: Mapping[str | None, int] | None = None,
    index_models: Mapping[str, int] | None = None,
) -> PaperConsistency | None:
    """Compare one paper across the three stores; ``None`` when it agrees."""
    expected = set(file_keys)
    all_objects = set(objects)
    # Parse artifacts (``papers/<id>/extracted/...``) are the parse cache, not
    # drift: they never get a ``paper_files`` row. Split them out before the
    # comparison and report their count instead of calling them orphans.
    cache_objects = {key for key in all_objects if CACHE_SEGMENT in key}
    actual = all_objects - cache_objects

    # The embedding model must be one per paper across both sides: vectors from
    # different models are not comparable. Rows/documents written before the
    # model was recorded have none -- "unknown", never a mismatch on its own.
    known_models = sorted(
        {
            str(value)
            for value in [*(chunk_models or {}), *(index_models or {})]
            if value
        }
    )

    if row.deleted:
        # Deletion purges documents and objects inline (``DELETE /api/papers/{id}``)
        # while the ``paper_files`` rows stay behind on purpose (soft delete), so a
        # file without its object is the *expected* state here. The only thing worth
        # reporting is what the purge left behind.
        issues: list[str] = []
        # Residue means *any* leftover object, cache artifacts included: the
        # delete path purges the whole ``papers/<id>/`` prefix.
        if all_objects or chunks_os > 0:
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
            orphan_objects=sorted(all_objects),
            cache_objects=len(cache_objects),
            parser_backend=row.parser_backend,
            index_backends=tuple(sorted(index_backends or {})),
            embedding_models=tuple(known_models),
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

    # The parser stamp: PostgreSQL is the truth about what produced the chunks,
    # the documents carry what actually did. Both unknown (a paper indexed before
    # the stamp existed) is agreement, not drift.
    index_stamped = sorted(index_backends or {})
    expected_backends = [row.parser_backend] if row.parser_backend else []
    if chunks_os and index_stamped != expected_backends:
        issues.append(ISSUE_PARSER_STAMP_MISMATCH)

    if len(known_models) >= 2:
        issues.append(ISSUE_EMBEDDING_MODEL_MISMATCH)

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
        cache_objects=len(cache_objects),
        parser_backend=row.parser_backend,
        index_backends=tuple(index_stamped),
        embedding_models=tuple(known_models),
    )


def check_consistency(
    session_factory: Callable[[], Any] = SessionLocal,
    *,
    storage: Any = object_storage,
    client: Any = None,
    alias: str = opensearch.ALIAS,
    limit: int = DEFAULT_PROBLEM_LIMIT,
    with_parser_papers: bool = False,
) -> ConsistencyReport:
    """Run the three-way check and return the report (read-only, never raises).

    ``storage``/``client``/``session_factory`` are injectable so the check can be
    unit-tested without MinIO, OpenSearch or PostgreSQL.

    ``with_parser_papers`` adds the per-backend live paper id lists: the worklist
    for re-parsing what a backend switch did not reach. Opt-in, because the
    default report is a summary rather than an export.
    """
    started = time.perf_counter()
    errors: list[str] = []
    problems: list[PaperConsistency] = []

    papers: dict[str, _PaperRow] = {}
    files: dict[str, list[str]] = {}
    chunks: dict[str, int] = {}
    chunk_models: dict[str, dict[str | None, int]] = {}
    try:
        papers, files, chunks, chunk_models = _load_papers(session_factory)
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
    document_backends: dict[str, dict[str, int]] = {}
    document_models: dict[str, dict[str, int]] = {}
    index_exists = False
    truncated = False
    try:
        documents, document_backends, document_models, index_exists, truncated = (
            _load_documents(client, alias)
        )
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
            index_backends=document_backends.get(paper_id, {}),
            chunk_models=chunk_models.get(paper_id, {}),
            index_models=document_models.get(paper_id, {}),
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
    # Parse-cache artifacts across the corpus: legitimate, counted, not problems.
    cache_objects_total = sum(
        1
        for keys in objects_per_paper.values()
        for key in keys
        if CACHE_SEGMENT in key
    )

    paper_ids: dict[str, list[str]] = {}
    paper_ids_truncated = False
    if with_parser_papers:
        paper_ids, paper_ids_truncated = _census_ids(
            (paper_id, row.parser_backend)
            for paper_id, row in papers.items()
            if not row.deleted
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
        cache_objects_total=cache_objects_total,
        parser_backends_papers=_census(
            row.parser_backend for row in papers.values() if not row.deleted
        ),
        parser_backends_documents=_document_census(documents, document_backends),
        embedding_models_chunks=_chunk_census(chunk_models),
        embedding_models_documents=_document_census(documents, document_models),
        parser_backends_paper_ids=paper_ids,
        parser_backends_paper_ids_truncated=paper_ids_truncated,
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
                "parser_backends_papers": report.parser_backends_papers,
                "parser_backends_documents": report.parser_backends_documents,
                "embedding_models_chunks": report.embedding_models_chunks,
                "embedding_models_documents": report.embedding_models_documents,
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
    "ISSUE_EMBEDDING_MODEL_MISMATCH",
    "ISSUE_MISSING_CHUNKS",
    "ISSUE_MISSING_INDEX",
    "ISSUE_MISSING_OBJECT",
    "ISSUE_ORPHAN_INDEX",
    "ISSUE_ORPHAN_OBJECT",
    "ISSUE_PARSER_STAMP_MISMATCH",
    "PARSER_PAPER_ID_LIMIT",
    "UNKNOWN_BACKEND",
    "ConsistencyReport",
    "PaperConsistency",
    "check_consistency",
]
