"""Re-extract one paper's metadata from its stored PDF and merge the new values.

This is the same three-layer discovery the ingest pipeline runs
(``pdf_embedded`` → ``pdf_heuristic`` → ``filename``), lifted out of
``app.workers.tasks`` so that:

* the ingest pipeline (``tasks._backfill_metadata``) and
* the re-extraction batch (``scripts/reextract_metadata.py`` and
  ``POST /api/papers/metadata/reextract``)

all run **exactly one** implementation, never two that can drift.

The merge goes through ``metadata_merge.merge_values`` (rule R2: fill a blank,
never overwrite an existing or manual value), so re-running over the whole
library can only *add* absent identifiers/fields -- it never rewrites what is
already there. The chunk text, the embeddings and the stored PDF are not
touched: this is a metadata refresh, not a reindex.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper
from app.parsing.pdf import EmbeddedMetadata, extract_embedded_metadata
from app.services import metadata_identifiers, metadata_merge, metadata_sources
from app.services import metadata_service, paper_service, provenance_service

logger = get_logger(__name__)

#: A file name is a hint about identity, weaker than the first-page heuristics
#: (0.5) and far weaker than the PDF's own metadata (1.0).
FILENAME_MATCH_CONFIDENCE = 0.4

#: Fields whose *value* a run may change, in report order. Identifiers use the
#: ``identifier:<scheme>`` claim name so the merge engine decides them like the
#: pipeline does.
REPORT_FIELDS: tuple[str, ...] = (
    "title",
    "abstract",
    "year",
    "authors",
    "venue",
    "identifier:doi",
    "identifier:arxiv",
    "identifier:ieee_article_number",
)


@dataclass
class FieldChange:
    """One field a run added (or would add). ``action`` is the merge action."""

    field: str
    old: object
    new: object
    action: str


@dataclass
class ReextractResult:
    """What re-extracting one paper produced (or would produce)."""

    paper_id: str
    filename: str | None = None
    changes: list[FieldChange] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def _may_offer_arxiv_id(session: Session, paper: Paper, arxiv_id: str) -> bool:
    """Whether layer 3 may offer the arXiv id its file name carries.

    Two ways the answer is no: the paper already knows an arXiv id (as a claim or
    as the mirror column), or the id already belongs to another paper. This guard
    has always protected the ingest pipeline; re-extraction keeps it so a name can
    never second-guess a document or a human.
    """
    if provenance_service.current_claim(session, paper.id, "identifier:arxiv"):
        return False
    if (paper.arxiv_id or "").strip():
        return False
    return (
        metadata_identifiers.find_identifier(
            session, metadata_identifiers.SCHEME_ARXIV, arxiv_id
        )
        is None
    )


def reextract_paper_metadata(
    session: Session,
    paper: Paper,
    *,
    pages=None,
    pdf_bytes: bytes | None = None,
    filename: str | None = None,
    dry_run: bool = False,
    collect_changes: bool = True,
    importer: str = "reextract",
) -> ReextractResult:
    """Run the three discovery layers over ``paper`` and merge what they find.

    ``dry_run=True`` computes and reports the same decisions without writing
    anything. ``collect_changes=False`` skips building that report (the pipeline
    does not need it, so it avoids a redundant read-only merge pass). The caller
    owns the transaction; apply mode flushes but never commits.
    """
    result = ReextractResult(paper_id=paper.id, filename=filename)

    embedded = (
        extract_embedded_metadata(pdf_bytes) if pdf_bytes else EmbeddedMetadata(raw={})
    )
    _merge_layer(
        session,
        paper,
        result,
        metadata_service.embedded_claim_values(embedded),
        source_type=metadata_sources.SOURCE_TYPE_PDF_EMBEDDED,
        source_ref=metadata_sources.paper_embedded_ref(paper.id),
        raw=embedded.raw or {},
        match_method="embedded",
        confidence=1.0,
        dry_run=dry_run,
        collect_changes=collect_changes,
        importer=importer,
    )

    if pages is not None:
        heuristics = metadata_service.extract_metadata(
            pages, url=paper.url, pdf_bytes=pdf_bytes
        )
        _merge_layer(
            session,
            paper,
            result,
            metadata_service.heuristic_claim_values(heuristics),
            source_type=metadata_sources.SOURCE_TYPE_PDF_HEURISTIC,
            source_ref=metadata_sources.paper_heuristic_ref(paper.id),
            raw=dict(heuristics),
            match_method="heuristic",
            confidence=0.5,
            dry_run=dry_run,
            collect_changes=collect_changes,
            importer=importer,
        )

    if filename:
        filename_values = metadata_service.filename_claim_values(filename)
        guessed = str(filename_values.get("identifier:arxiv") or "")
        if guessed and _may_offer_arxiv_id(session, paper, guessed):
            _merge_layer(
                session,
                paper,
                result,
                filename_values,
                source_type=metadata_sources.SOURCE_TYPE_FILENAME,
                source_ref=metadata_sources.paper_filename_ref(paper.id),
                raw={"filename": filename, **filename_values},
                match_method="filename",
                confidence=FILENAME_MATCH_CONFIDENCE,
                dry_run=dry_run,
                collect_changes=collect_changes,
                importer=importer,
            )

    if not dry_run:
        metadata_identifiers.mirror_legacy_columns(session, paper)
        session.flush()
    return result


def _merge_layer(
    session: Session,
    paper: Paper,
    result: ReextractResult,
    values: dict,
    *,
    source_type: str,
    source_ref: str,
    raw: dict,
    match_method: str,
    confidence: float,
    dry_run: bool,
    collect_changes: bool,
    importer: str,
) -> None:
    """Merge one discovery layer, recording (and optionally applying) its changes."""
    if not values:
        return
    if dry_run or collect_changes:
        for decision in metadata_merge.merge_values(
            session, paper, values, source_type=source_type, dry_run=True
        ).decisions:
            # Only a *winning* action changes the paper. A recorded conflict keeps
            # the current value, so it is not a change and must not inflate the
            # report (nor be counted as "this paper changed").
            if decision.action not in (
                metadata_merge.ACTION_FILLED,
                metadata_merge.ACTION_OVERRIDDEN,
                metadata_merge.ACTION_SPECIAL,
            ):
                continue
            result.changes.append(
                FieldChange(
                    field=decision.field,
                    old=decision.rejected,
                    new=decision.kept,
                    action=decision.action,
                )
            )
    if dry_run:
        return
    source = metadata_sources.upsert_source(
        session,
        source_type=source_type,
        source_ref=source_ref,
        raw=raw,
        paper_id=paper.id,
        match_status=metadata_sources.MATCH_STATUS_MATCHED,
        match_method=match_method,
        match_confidence=confidence,
        importer=importer,
    )
    metadata_merge.merge_values(
        session,
        paper,
        values,
        source_type=source_type,
        source_id=source.id,
        confidence=confidence,
    )


__all__ = [
    "FILENAME_MATCH_CONFIDENCE",
    "REPORT_FIELDS",
    "FieldChange",
    "ReextractResult",
    "reextract_paper_metadata",
]


# --------------------------------------------------------------------------- #
# per-paper and whole-library drivers (shared by the CLI and the endpoints)
# --------------------------------------------------------------------------- #
@dataclass
class ReextractSummary:
    """Aggregate of a re-extraction run over one or many papers."""

    dry_run: bool
    papers: int = 0
    changed_papers: int = 0
    skipped_no_file: int = 0
    failed: int = 0
    #: ``field`` -> number of papers where it was added.
    added_fields: dict[str, int] = field(default_factory=dict)
    #: ``identifier:<scheme>`` -> number of papers that gained it.
    added_identifiers: dict[str, int] = field(default_factory=dict)
    #: Index documents whose metadata snapshot was rewritten (0 on a dry run).
    index_documents: int = 0
    #: Per-paper detail, only when ``detail=True``.
    details: list[ReextractResult] = field(default_factory=list)
    #: Paper ids that errored (with the message), for the operator to inspect.
    errors: list[dict[str, str]] = field(default_factory=list)

    def as_api_dict(self, *, note: str = "") -> dict:
        """Plain dict shaped like ``schemas.metadata.ReextractOut`` (changes flattened)."""
        changes = [
            {
                "paper_id": result.paper_id,
                "field": change.field,
                "old": change.old,
                "new": change.new,
                "action": change.action,
            }
            for result in self.details
            for change in result.changes
        ]
        return {
            "dry_run": self.dry_run,
            "papers": self.papers,
            "changed_papers": self.changed_papers,
            "skipped_no_file": self.skipped_no_file,
            "failed": self.failed,
            "added_fields": self.added_fields,
            "added_identifiers": self.added_identifiers,
            "index_documents": self.index_documents,
            "changes": changes,
            "errors": self.errors,
            "note": note,
        }


def select_live_papers(
    session: Session, *, paper_id: str | None = None, limit: int | None = None
):
    """Live papers to re-extract, oldest first (stable order for a dry run)."""
    from sqlalchemy import select

    statement = (
        select(Paper)
        .where(Paper.deleted_at.is_(None))
        .order_by(Paper.created_at, Paper.id)
    )
    if paper_id:
        statement = statement.where(Paper.id == paper_id)
    if limit is not None:
        statement = statement.limit(max(0, limit))
    return list(session.execute(statement).scalars().all())


def _chunk_ids(session: Session, paper_id: str) -> list[str]:
    from sqlalchemy import select

    from app.db.models import PaperChunk

    return [
        str(row[0])
        for row in session.execute(
            select(PaperChunk.id).where(PaperChunk.paper_id == paper_id)
        ).all()
    ]


def _refresh_index_for(session: Session, paper: Paper) -> int:
    """Rewrite the metadata snapshot of one paper's indexed chunks (seconds)."""
    from app.search import opensearch, snapshot

    chunk_ids = _chunk_ids(session, paper.id)
    if not chunk_ids:
        return 0
    doc = snapshot.paper_metadata_snapshot(paper)
    report = opensearch.bulk_update_documents(
        [{"chunk_id": cid, "doc": doc} for cid in chunk_ids]
    )
    return int(report.get("updated", 0))


def _accumulate(summary: ReextractSummary, result: ReextractResult) -> None:
    """Fold one paper's result into a run summary."""
    summary.papers += 1
    if not result.changed:
        return
    summary.changed_papers += 1
    for change in result.changes:
        summary.added_fields[change.field] = summary.added_fields.get(change.field, 0) + 1
        if change.field.startswith("identifier:"):
            summary.added_identifiers[change.field] = (
                summary.added_identifiers.get(change.field, 0) + 1
            )


def reextract_one(
    session: Session,
    paper: Paper,
    *,
    apply: bool = False,
    refresh_index: bool = True,
    detail: bool = True,
    download=None,
) -> ReextractSummary:
    """Re-extract exactly one paper (the single-paper entry point).

    Same behaviour as one iteration of :func:`reextract_library`, so the single and
    whole-library endpoints / CLI share one code path. Raises on a bad paper (the
    caller decides how to report it); a paper with no stored file is reported as
    ``skipped_no_file`` rather than raising.
    """
    from app.parsing.pdf import extract_pages
    from app.services import object_storage, paper_service

    download = download or object_storage.download_bytes
    summary = ReextractSummary(dry_run=not apply)

    record = paper_service.original_file(paper)
    if record is None:
        summary.papers = 1
        summary.skipped_no_file = 1
        return summary

    data = download(record.object_key)
    pages = extract_pages(data)
    result = reextract_paper_metadata(
        session,
        paper,
        pages=pages,
        pdf_bytes=data,
        filename=record.filename,
        dry_run=not apply,
        collect_changes=True,
        importer="reextract",
    )
    if detail:
        summary.details.append(result)
    _accumulate(summary, result)

    if apply:
        session.commit()
        if refresh_index and result.changed:
            summary.index_documents += _refresh_index_for(session, paper)
    else:
        session.rollback()  # discard the read-only probe's flushes
    return summary


def reextract_library(
    session: Session,
    *,
    paper_id: str | None = None,
    limit: int | None = None,
    apply: bool = False,
    refresh_index: bool = True,
    detail: bool = False,
    download=None,
) -> ReextractSummary:
    """Re-extract metadata for every live paper (or the one named), reporting deltas.

    ``apply=False`` is a dry run: it downloads and parses each PDF, computes the
    merge for every paper, and reports what would change -- nothing is written.
    ``apply=True`` merges the changes and (unless ``refresh_index=False``) rewrites
    the affected papers' index-metadata snapshot. It never touches chunk text,
    embeddings or the stored PDF.
    """
    from app.services import object_storage

    download = download or object_storage.download_bytes
    combined = ReextractSummary(dry_run=not apply)

    for paper in select_live_papers(session, paper_id=paper_id, limit=limit):
        try:
            one = reextract_one(
                session,
                paper,
                apply=apply,
                refresh_index=refresh_index,
                detail=detail,
                download=download,
            )
        except Exception as exc:  # noqa: BLE001 - one bad paper must not stop the batch
            combined.papers += 1
            combined.failed += 1
            combined.errors.append({"paper_id": paper.id, "error": str(exc)})
            session.rollback()
            logger.warning(
                "reextract failed for %s: %s",
                paper.id,
                exc,
                extra={"extra_fields": {"paper_id": paper.id}},
            )
            continue
        combined.papers += (1 if one.papers else 0)
        combined.changed_papers += one.changed_papers
        combined.skipped_no_file += one.skipped_no_file
        combined.index_documents += one.index_documents
        combined.details.extend(one.details)
        for source_map, dest in (
            (one.added_fields, combined.added_fields),
            (one.added_identifiers, combined.added_identifiers),
        ):
            for name, count in source_map.items():
                dest[name] = dest.get(name, 0) + count

    return combined


__all__ = [
    "FILENAME_MATCH_CONFIDENCE",
    "REPORT_FIELDS",
    "FieldChange",
    "ReextractResult",
    "ReextractSummary",
    "reextract_library",
    "reextract_one",
    "reextract_paper_metadata",
    "select_live_papers",
]
