"""Acceptance: one PDF through the real pipeline, once per parser backend.

Run::

    PYTHONPATH=. uv run python scripts/acceptance_pipeline_backend.py \
        --pdf logs/eval/docling/corpus/2404.05260.pdf

What it proves (plan .hermes/plans/2026-09-28_160551 section 6.1):

* ``PARSER_BACKEND`` decides what the *pipeline* chunks and indexes -- not just
  what a parser script can do in isolation;
* the parser stamp on ``papers`` and the ``parser_backend`` on the indexed
  documents agree, and a degraded parse is visible in the ledger;
* a reindex replays the stored parse artifacts instead of calling docling again
  (``PARSER_CACHE``), which is what makes a backend switch affordable;
* both runs leave the library exactly as they found it -- papers, objects,
  documents, chunks and metadata entities.

Cost: one full ingestion per backend (docling spends 30-370 s per paper, pypdf
about a second), embeddings included. Everything it creates is deleted at the
end; ``--keep`` skips the cleanup for inspection.

The bytes get a trailing PDF comment before they are ingested, and each run's
paper is purged *before* the next backend starts. Both are needed because
ingestion dedupes twice against the whole live library: by SHA256, and -- after
the metadata backfill -- by the *identity* fingerprint (``doi:``/``arxiv:``), so a
corpus PDF (which is a library paper) or a second copy of the same paper would be
resolved as a duplicate no-op instead of being parsed. Purging between runs means
each backend ingests the same content into an empty slot, which is what the
comparison needs.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from app.core.config import settings
from app.db.models import (
    Author,
    Base,
    IngestionJob,
    Paper,
    PaperAuthor,
    PaperChunk,
    PaperDegradation,
    PaperFieldProvenance,
    PaperFile,
    PaperIdentifier,
    PaperSource,
    PaperTag,
    PapersTag,
    Venue,
    new_uuid,
)
from app.db.session import SessionLocal
from app.search import opensearch
from app.services import consistency_service, ingestion_service as ingest
from app.services import object_storage, paper_service
from app.workers import tasks

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_ROOT = Path("logs/eval/pipeline-backend")
DEFAULT_CORPUS = Path("logs/eval/docling/corpus")
BACKENDS = ("docling", "pypdf")
#: Where ``parser_service`` stores the cached parse of one paper.
PARSE_PREFIX = "papers/{paper_id}/extracted/parsed/"

#: Entity tables the metadata backfill can create rows in; snapshotted before the
#: first run and pruned afterwards, so a temporary paper leaves no new author or
#: venue behind.
ENTITY_TABLES = ("authors", "venues", "paper_tags")


# --------------------------------------------------------------------------- #
# observation
# --------------------------------------------------------------------------- #


@dataclass
class Run:
    """One backend's pass over the same bytes."""

    backend: str
    label: str
    paper_id: str = ""
    job_id: str = ""
    seconds: float = 0.0
    stage: str = ""
    error: str | None = None
    title: str | None = None
    stamp_backend: str | None = None
    stamp_version: str | None = None
    chunks: int = 0
    chunk_chars: int = 0
    text_sha256: str = ""
    sections: list[str] = field(default_factory=list)
    max_page: int = 0
    documents: int = 0
    document_backends: dict[str, int] = field(default_factory=dict)
    degradations: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    cache_probe_seconds: float | None = None
    cache_probe_documents: int = 0
    #: True when the reindex found that backend's own cached artifact.
    cache_probe_replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def pipeline_error(session, job_id: str) -> str | None:
    job = session.get(IngestionJob, job_id)
    if job is None:
        return "job vanished"
    return job.error


def document_backends(paper_id: str) -> tuple[int, dict[str, int]]:
    """Index side: how many documents this paper has, and what produced them."""
    client = opensearch.get_client()
    alias = opensearch.ALIAS
    if not opensearch.index_exists(client, alias):
        return 0, {}
    response = client.search(
        index=alias,
        body={
            "size": 0,
            "query": {"term": {"paper_id": paper_id}},
            "aggs": {"by_backend": {"terms": {"field": "parser_backend"}}},
        },
    )
    total = int(response.get("hits", {}).get("total", {}).get("value", 0))
    buckets = response.get("aggregations", {}).get("by_backend", {}).get("buckets", [])
    return total, {str(entry["key"]): int(entry["doc_count"]) for entry in buckets}


def artifacts_under(paper_id: str) -> list[str]:
    """Keys under one paper's parse-artifact prefix (what the run cached)."""
    prefix = PARSE_PREFIX.format(paper_id=paper_id)
    return sorted(str(obj.object_name) for obj in object_storage.list_objects(prefix=prefix))


def observe(session, run: Run, *, label: str) -> Run:
    """Read back everything the run left in PostgreSQL and in the index."""
    run.label = label
    job = session.get(IngestionJob, run.job_id)
    if job is not None:
        run.stage = str(job.stage)
        run.error = job.error_message
    paper = session.get(Paper, run.paper_id)
    if paper is not None:
        run.title = paper.title
        run.stamp_backend = paper.parser_backend
        run.stamp_version = paper.parser_version

    rows = session.scalars(
        select(PaperChunk)
        .where(PaperChunk.paper_id == run.paper_id)
        .order_by(PaperChunk.chunk_index)
    ).all()
    run.chunks = len(rows)
    texts = [str(row.text) for row in rows]
    run.chunk_chars = sum(len(text) for text in texts)
    run.text_sha256 = paper_service.compute_sha256("".join(texts).encode("utf-8"))
    seen: list[str] = []
    for row in rows:
        section = str(row.section)
        if section not in seen:
            seen.append(section)
    run.sections = seen[:12]
    run.max_page = max((int(row.page_end or 0) for row in rows), default=0)

    run.documents, run.document_backends = document_backends(run.paper_id)
    run.degradations = [
        {"stage": row.stage, "code": row.code, "detail": row.detail}
        for row in session.scalars(
            select(PaperDegradation).where(PaperDegradation.paper_id == run.paper_id)
        ).all()
    ]
    run.artifacts = artifacts_under(run.paper_id)
    return run


# --------------------------------------------------------------------------- #
# one run
# --------------------------------------------------------------------------- #


def stage_upload(data: bytes, filename: str) -> tuple[str, str]:
    """Stage the bytes and create the job the way ``POST /ingest/files`` does.

    No paper row is created here: ingestion creates it (and dedupes) itself, so
    the run goes through exactly the production path.
    """
    request_id = new_uuid()
    staging_key = object_storage.build_staging_key(request_id, 0, filename)
    object_storage.upload_bytes(
        staging_key, data, content_type="application/pdf", metadata={"filename": filename}
    )
    session = SessionLocal()
    try:
        job = ingest.create_job(
            session,
            source_type="file",
            filename=filename,
            content_type="application/pdf",
            size_bytes=len(data),
            payload={"object_key": staging_key},
        )
        session.commit()
        return job.id, staging_key
    finally:
        session.close()


def with_marker(data: bytes, tag: str) -> bytes:
    """A PDF comment after the trailer: new bytes, same document.

    Ingestion dedupes by SHA256 over the whole library, and the acceptance corpus
    is *in* the library, so an unmodified copy would be discarded as a duplicate
    before any parser ran. A trailing comment is legal PDF and invisible to both
    backends.
    """
    return data.rstrip(b"\r\n") + b"\n% paperbox-acceptance " + tag.encode() + b"\n"


def ingest_once(data: bytes, filename: str, backend: str) -> Run:
    """Run the production ingestion path with ``PARSER_BACKEND`` pinned."""
    run = Run(backend=backend, label=f"ingest/{backend}")
    settings.parser_backend = backend
    run.job_id, staging_key = stage_upload(data, filename)
    started = time.perf_counter()
    tasks.run_ingestion_job(run.job_id)  # the worker's own entry point
    run.seconds = round(time.perf_counter() - started, 2)

    session = SessionLocal()
    try:
        job = session.get(IngestionJob, run.job_id)
        run.paper_id = str(job.paper_id or "")
        if not run.paper_id:
            run.error = "ingestion produced no paper (duplicate or failed before storing)"
            return run
        session.expire_all()
        observe(session, run, label=f"ingest/{backend}")
    finally:
        session.close()
    if object_storage.object_exists(staging_key):
        object_storage.delete_object(staging_key)
    return run


def reindex_once(run: Run) -> None:
    """Second pass over the same paper: replay if the backend cached, else parse."""
    # ``observe`` read the artifacts before this call, so their presence tells us
    # which of the two happened -- the wording matters, they are very different
    # costs (61 s replay vs 316 s parse for the same paper).
    run.cache_probe_replayed = any(f"/{run.backend}/" in key for key in run.artifacts)
    session = SessionLocal()
    try:
        job = tasks.reindex_paper(session, run.paper_id)
        observe(session, run, label=run.label)
        started = time.perf_counter()
        tasks.run_reindex_job(job.id)
        run.cache_probe_seconds = round(time.perf_counter() - started, 2)
        run.cache_probe_documents = document_backends(run.paper_id)[0]
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# cleanup
# --------------------------------------------------------------------------- #


def _entity_ids(session) -> dict[str, set[str]]:
    """Id sets of the entity tables a metadata backfill can add rows to."""
    snapshot: dict[str, set[str]] = {}
    for table in ENTITY_TABLES:
        column = Base.metadata.tables[table].c.id
        snapshot[table] = {str(value) for value in session.scalars(select(column)).all()}
    return snapshot


def _referencing_columns(table_name: str) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    for table in Base.metadata.sorted_tables:
        for foreign_key in table.foreign_keys:
            if foreign_key.column.table.name == table_name and table.name != table_name:
                references.append((table.name, foreign_key.parent.name))
    return references


def prune_new_entities(session, before: dict[str, set[str]]) -> dict[str, int]:
    """Delete entity rows this run created and that nothing references any more."""
    removed: dict[str, int] = {}
    model_by_table = {"authors": Author, "venues": Venue, "paper_tags": PaperTag}
    for table, model in model_by_table.items():
        references = _referencing_columns(table)
        for row_id in list(session.scalars(select(model.id)).all()):
            if str(row_id) in before.get(table, set()):
                continue
            still_used = False
            for ref_table, ref_column in references:
                ref = Base.metadata.tables[ref_table]
                count = session.execute(
                    select(func.count())
                    .select_from(ref)
                    .where(ref.c[ref_column] == row_id)
                ).scalar_one()
                if count:
                    still_used = True
                    break
            if still_used:
                continue
            session.execute(delete(model).where(model.id == row_id))
            removed[table] = removed.get(table, 0) + 1
    session.commit()
    return removed


def purge_paper(session, paper_id: str) -> dict[str, int]:
    """Remove every trace of a temporary paper: index, objects, rows."""
    opensearch.delete_by_paper_id(paper_id)
    removed_objects = object_storage.delete_prefix(paper_id)
    counts: dict[str, int] = {"objects": removed_objects}
    for model in (
        PaperChunk,
        PaperDegradation,
        PaperFieldProvenance,
        PaperAuthor,
        PapersTag,
        PaperIdentifier,
        PaperSource,
        PaperFile,
        IngestionJob,
    ):
        result = session.execute(delete(model).where(model.paper_id == paper_id))
        counts[model.__tablename__] = int(result.rowcount or 0)
    result = session.execute(delete(Paper).where(Paper.id == paper_id))
    counts["papers"] = int(result.rowcount or 0)
    session.commit()
    return counts


def residue(session, paper_ids: list[str]) -> dict[str, int]:
    """Rows still pointing at a temporary paper, per table (must be empty).

    Every table with a ``paper_id`` column is checked by introspection, so a new
    table added later cannot silently start leaking temporary papers.
    """
    found: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        if "paper_id" not in table.c:
            continue
        count = session.execute(
            select(func.count())
            .select_from(table)
            .where(table.c.paper_id.in_(paper_ids))
        ).scalar_one()
        if count:
            found[table.name] = int(count)
    return found


# --------------------------------------------------------------------------- #
# store totals (before/after, via the consistency check)
# --------------------------------------------------------------------------- #


def store_totals() -> dict[str, Any] | None:
    try:
        report = consistency_service.check_consistency(limit=1)
    except Exception as exc:  # noqa: BLE001 - a broken store must not fail the run
        print(f"  (consistency totals unavailable: {type(exc).__name__}: {exc})")
        return None
    data = report.as_dict()
    return {
        "totals": data["totals"],
        "parser_backends": data["parser_backends"],
        "problems": data["totals"]["problems"],
        "orphan_objects": data["totals"]["orphan_objects"],
        "orphan_documents": data["totals"]["orphan_documents"],
    }


def totals_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    if not before or not after:
        return {}
    delta = {
        key: after["totals"][key] - before["totals"][key]
        for key in sorted(before["totals"])
    }
    return {
        "totals": delta,
        "parser_backends": {
            side: {
                key: (after["parser_backends"].get(side, {}).get(key, 0)
                      - before["parser_backends"].get(side, {}).get(key, 0))
                for key in set(before["parser_backends"].get(side, {}))
                | set(after["parser_backends"].get(side, {}))
                if (after["parser_backends"].get(side, {}).get(key, 0)
                    - before["parser_backends"].get(side, {}).get(key, 0))
            }
            for side in ("papers", "documents")
        },
    }


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def print_run(run: Run) -> None:
    print(
        f"  {run.backend:8} {run.stage:11} {run.seconds:8.2f}s  "
        f"chunks={run.chunks:4} docs={run.documents:4} pages<= {run.max_page:3}  "
        f"stamp={run.stamp_backend or '-'}"
        f"{'/' + (run.stamp_version or '-') if run.stamp_backend else ''}  "
        f"degraded={len(run.degradations)}  artifacts={len(run.artifacts)}"
    )
    if run.error:
        print(f"           error: {run.error}")
    if run.document_backends:
        print(f"           index backends: {run.document_backends}")
    if run.cache_probe_seconds is not None:
        how = "cache replay" if run.cache_probe_replayed else "real parse (no cached artifact)"
        print(
            f"           reindex/{run.backend}: {run.cache_probe_seconds:.2f}s, "
            f"{run.cache_probe_documents} document(s) -- {how}"
        )
    for entry in run.degradations:
        print(f"           degraded: {entry['stage']}/{entry['code']} {entry['detail']}")


def write_markdown(runs: list[Run], data: dict[str, Any], path: Path) -> None:
    lines = [
        "# Pipeline backend switch — acceptance",
        "",
        f"- ran at: {data['ran_at']}",
        f"- input: `{data['input']}` ({data['input_bytes']} bytes)",
        f"- `PARSER_BACKEND` default: `{data['default_backend']}`",
        f"- index: `{data['index']}`",
        "",
        "| backend | stage | seconds | chunks | doc chars | sections | max page | index docs | index backend | stamp | degraded | artifacts |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        lines.append(
            f"| {run.backend} | {run.stage} | {run.seconds:.2f} | {run.chunks} | "
            f"{run.chunk_chars} | {len(run.sections)} | {run.max_page} | {run.documents} | "
            f"{','.join(f'{k}:{v}' for k, v in run.document_backends.items()) or '-'} | "
            f"{run.stamp_backend or '-'} | {len(run.degradations)} | {len(run.artifacts)} |"
        )
    lines += ["", "## store totals", "", "```json", json.dumps(data["totals_delta"], indent=2), "```"]
    lines += ["", "## cleanup", "", "```json", json.dumps(data["cleanup"], indent=2), "```"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pdf", help=f"PDF to ingest twice (default: {DEFAULT_CORPUS}/2404.05260.pdf)")
    parser.add_argument(
        "--backends", nargs="+", default=list(BACKENDS), choices=list(BACKENDS)
    )
    parser.add_argument("--out", help=f"output directory (default: {DEFAULT_OUT_ROOT}/<stamp>)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary papers")
    parser.add_argument(
        "--no-cache-probe", dest="cache_probe", action="store_false", help="skip the reindex pass"
    )
    parser.add_argument(
        "--no-consistency", action="store_true", help="skip the store totals"
    )
    args = parser.parse_args()

    pdf = Path(args.pdf) if args.pdf else DEFAULT_CORPUS / "2404.05260.pdf"
    if not pdf.is_file():
        print(f"input not found: {pdf}")
        return 2
    data = with_marker(pdf.read_bytes(), time.strftime("%Y%m%d-%H%M%S"))
    filename = pdf.name

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else DEFAULT_OUT_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"input      : {pdf} ({len(data)} bytes, +marker so ingestion does not dedupe it)")
    print(f"default    : PARSER_BACKEND={settings.parser_backend}  docling={settings.docling_url}")
    print(f"output     : {out_dir}")

    session = SessionLocal()
    try:
        entities_before = _entity_ids(session)
    finally:
        session.close()

    totals_before = None if args.no_consistency else store_totals()
    runs: list[Run] = []
    cleanup: dict[str, Any] = {}
    paper_ids: list[str] = []
    print(f"\n{'backend':8} {'stage':11} {'seconds':>9}  chunks/docs")
    for backend in args.backends:
        run = ingest_once(data, filename, backend)
        print_run(run)
        if args.cache_probe and run.stage == "COMPLETED":
            reindex_once(run)
            print_run(run)
        runs.append(run)
        if run.paper_id:
            paper_ids.append(run.paper_id)
        if not args.keep and run.paper_id:
            # Purge before the next backend: identity dedupe would resolve the
            # next ingest as a duplicate of this paper and never parse it.
            session = SessionLocal()
            try:
                cleanup.setdefault("purged", {})[run.paper_id] = purge_paper(
                    session, run.paper_id
                )
            finally:
                session.close()

    if not args.keep:
        session = SessionLocal()
        try:
            cleanup["pruned_entities"] = prune_new_entities(session, entities_before)
            cleanup["residue"] = residue(session, paper_ids)
        finally:
            session.close()
        print(f"\nresidue    : {cleanup['residue'] or 'none'}")

    totals_after = None if args.no_consistency else store_totals()
    delta = totals_delta(totals_before, totals_after)
    if delta:
        print(f"store delta: {delta['totals']}")
        if any(delta["totals"].values()):
            print("  ^ the run did not leave the library exactly as it found it")

    report: dict[str, Any] = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "input": str(pdf),
        "input_bytes": len(data),
        "input_sha256": paper_service.compute_sha256(data),
        "default_backend": "docling",
        "docling_url": settings.docling_url,
        "index": opensearch.ALIAS,
        "runs": [run.as_dict() for run in runs],
        "cleanup": cleanup,
        "totals_before": totals_before,
        "totals_after": totals_after,
        "totals_delta": delta,
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_markdown(runs, report, out_dir / "summary.md")
    print(f"\nwrote {out_dir / 'report.json'} and {out_dir / 'summary.md'}")

    if any(run.error for run in runs) or any(run.stage != "COMPLETED" for run in runs):
        return 1
    if delta and any(delta["totals"].values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
