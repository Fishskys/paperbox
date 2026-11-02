"""Re-extract one paper's metadata from its stored PDF and merge the new values.

This is the same three-layer discovery the ingest pipeline runs
(``pdf_embedded`` → ``pdf_heuristic`` → ``filename``), lifted out of
``app.workers.tasks`` so that:

* the ingest pipeline (``tasks._backfill_metadata``) and
* the re-extraction batch (``scripts/reextract_metadata.py`` and
  ``POST /api/papers/metadata/reextract``)

all run **exactly one** implementation, never two that can drift.

The shared merge policy protects human decisions and structured sources while
allowing corroborated PDF corrections. Chunk text, embeddings and stored PDFs
are untouched; changed metadata is synchronized through a transactional outbox.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper
from app.parsing.pdf import EmbeddedMetadata, embedded_metadata_from_sources, extract_embedded_metadata
from app.parsing.metadata_fields import abstract_problem, authors_problem, corroborates
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
    quality_counts: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def _may_offer_identifier(
    session: Session, paper: Paper, scheme: str, value: str
) -> bool:
    """Whether layer 3 may offer the identifier its file name carries.

    Two ways the answer is no: the paper already knows an arXiv id (as a claim or
    as the mirror column), or the id already belongs to another paper. This guard
    has always protected the ingest pipeline; re-extraction keeps it so a name can
    never second-guess a document or a human.
    """
    field = f"identifier:{scheme}"
    if provenance_service.current_claim(session, paper.id, field):
        return False
    mirror = paper.doi if scheme == metadata_identifiers.SCHEME_DOI else paper.arxiv_id
    if (mirror or "").strip():
        return False
    return (
        metadata_identifiers.find_identifier(
            session, scheme, value
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
    if dry_run:
        from app.services import metadata_quality
        token = metadata_quality.preview_mode.set(True)
        savepoint = session.begin_nested()
        try:
            return reextract_paper_metadata(session, paper, pages=pages, pdf_bytes=pdf_bytes, filename=filename,
                collect_changes=collect_changes, importer=importer)
        finally:
            savepoint.rollback()
            metadata_quality.preview_mode.reset(token)
    result = ReextractResult(paper_id=paper.id, filename=filename)

    embedded, heuristics, context = pdf_candidates(pages, pdf_bytes=pdf_bytes, title=paper.title, url=paper.url)
    if context.get("subject_status"):
        result.quality_counts[f"ignored_subject:{context['subject_status']['reason']}"] = 1
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

    if heuristics is not None:
        _merge_layer(
            session,
            paper,
            result,
            metadata_service.heuristic_claim_values(heuristics),
            source_type=metadata_sources.SOURCE_TYPE_PDF_HEURISTIC,
            source_ref=metadata_sources.paper_heuristic_ref(paper.id),
            raw={**dict(heuristics), "quality": context},
            match_method="heuristic",
            confidence=0.5,
            dry_run=dry_run,
            collect_changes=collect_changes,
            importer=importer,
        )

    if filename:
        filename_values = metadata_service.filename_claim_values(filename)
        accepted: dict[str, object] = {}
        for field, raw_value in filename_values.items():
            scheme = field.split(":", 1)[1]
            value = str(raw_value or "")
            if value and _may_offer_identifier(session, paper, scheme, value):
                accepted[field] = raw_value
        if accepted:
            _merge_layer(
                session,
                paper,
                result,
                accepted,
                source_type=metadata_sources.SOURCE_TYPE_FILENAME,
                source_ref=metadata_sources.paper_filename_ref(paper.id),
                raw={"filename": filename, **accepted},
                match_method="filename",
                confidence=FILENAME_MATCH_CONFIDENCE,
                dry_run=dry_run,
                collect_changes=collect_changes,
                importer=importer,
            )

    if not dry_run:
        metadata_identifiers.mirror_legacy_columns(session, paper)
        session.flush()
    from app.services import metadata_quality
    if not metadata_quality.preview_mode.get():
        logger.info("PDF metadata quality summary", extra={"extra_fields": {"paper_id": paper.id,
            "rule_version": metadata_merge.policy.QUALITY_VERSION, "quality_counts": result.quality_counts}})
    return result


def pdf_candidates(pages, *, pdf_bytes: bytes | None, title: str | None = None,
                   url: str | None = None, bundle: dict | None = None) -> tuple[EmbeddedMetadata, dict | None, dict]:
    """Extract candidates and evidence without database or index writes."""
    embedded = (embedded_metadata_from_sources(bundle["metadata"]["info"], bundle["metadata"]["xmp"])
                if bundle is not None else extract_embedded_metadata(pdf_bytes) if pdf_bytes else EmbeddedMetadata(raw={}))
    subject_reason = (abstract_problem(embedded.subject) or "uncorroborated_subject") if embedded.subject else None
    native = {"native_content": bundle["lines"]} if bundle is not None else {}
    heuristics = metadata_service.extract_metadata(pages, url=url, pdf_bytes=pdf_bytes, **native) if pages is not None else None
    if (not embedded.abstract and embedded.subject and not abstract_problem(embedded.subject)
            and heuristics and heuristics.get("quality", {}).get("text_state") == "ok"
            and corroborates(embedded.subject, str(heuristics.get("abstract") or ""))):
        embedded.abstract = embedded.subject
        embedded.field_origins = {**(embedded.field_origins or {}), "abstract": "info:Subject+body:abstract"}
        embedded.raw = {**(embedded.raw or {}), "field_origins": embedded.field_origins}
        subject_reason = None
    context = {**((heuristics or {}).get("quality") or {}), "abstract": (heuristics or {}).get("abstract"),
               "field_origins": embedded.field_origins or {}, "rule_version": metadata_merge.policy.QUALITY_VERSION}
    if (embedded.authors and not authors_problem(embedded.authors, title=title)
            and context.get("byline")
            and not (metadata_merge.policy.byline_spelling_variants(embedded.authors, context["byline"])
                     or metadata_merge.policy.byline_abbreviations(embedded.authors, context["byline"]))):
        context["byline_complete"] = False
        context["byline_conflict"] = True
    if subject_reason:
        context["subject_status"] = {"action": "ignored", "reason": subject_reason}
    embedded.raw = {**(embedded.raw or {}), "quality": context}
    return embedded, heuristics, context


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
    existing = metadata_sources.find_source(session, source_type, source_ref)
    if existing is not None and existing.raw != raw:
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:24]
        source_ref = f"{source_ref}:{digest}"
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
    report = metadata_merge.merge_values(
        session,
        paper,
        values,
        source_type=source_type,
        source_id=source.id,
        confidence=confidence,
        evidence=raw.get("quality") or {},
    )
    for decision in report.decisions:
        if decision.field in ("authors", "abstract"):
            key = f"{decision.action}:{decision.reason}"
            result.quality_counts[key] = result.quality_counts.get(key, 0) + 1
        if collect_changes and decision in report.applied:
            result.changes.append(FieldChange(decision.field, decision.rejected, decision.kept, decision.action))
    if source_type == metadata_sources.SOURCE_TYPE_PDF_HEURISTIC and raw.get("quality", {}).get("needs_review"):
        from app.services import metadata_quality
        metadata_quality.record(session, paper.id, "document", {"action": "conflict",
            "reason": raw["quality"]["text_state"], "before": None, "candidate": None,
            "selected": None, "source_id": source.id, "evidence": raw["quality"]})
        key = f"conflict:{raw['quality']['text_state']}"
        result.quality_counts[key] = result.quality_counts.get(key, 0) + 1


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
    index_pending: int = 0
    quality_counts: dict[str, int] = field(default_factory=dict)
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
            "index_pending": self.index_pending,
            "quality_counts": self.quality_counts,
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


def _refresh_index_for(session: Session, paper: Paper) -> int:
    """Rewrite the metadata snapshot of one paper's indexed chunks (seconds)."""
    from app.services import metadata_index_sync

    return metadata_index_sync.flush_pending(session, paper_id=paper.id)["updated"]


def _accumulate(summary: ReextractSummary, result: ReextractResult) -> None:
    """Fold one paper's result into a run summary."""
    summary.papers += 1
    for key, count in result.quality_counts.items():
        summary.quality_counts[key] = summary.quality_counts.get(key, 0) + count
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
        if refresh_index:
            summary.index_documents += _refresh_index_for(session, paper)
        from app.db.models import MetadataIndexUpdate
        summary.index_pending = int(session.get(MetadataIndexUpdate, paper.id) is not None)
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
        combined.index_pending += one.index_pending
        for key, count in one.quality_counts.items():
            combined.quality_counts[key] = combined.quality_counts.get(key, 0) + count
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
