"""Shell papers: metadata first, PDF later (import order B, section 10).

When an imported record cannot be matched to an existing paper, the paper is still
created -- with ``status='AWAITING_FILE'``, no file and no chunks -- because the
record is real and waiting for its PDF is better than dropping it. When the PDF
finally arrives, the pipeline looks for the shell and **reuses its ``paper_id``**:
the identifiers, provenance and review history of the import stay attached to the
same paper, and ``GET /api/papers?status=AWAITING_FILE`` shows what is still
missing a file.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper, PaperSource, new_uuid
from app.services import metadata_identifiers as identifiers
from app.services import metadata_merge as merge
from app.services import metadata_sources as sources
from app.services import object_storage, paper_service

logger = get_logger(__name__)


def create_shell(
    session: Session,
    values: Mapping[str, Any],
    *,
    source_type: str,
    source_ref: str,
    raw: dict | None = None,
    content_type: str | None = None,
    importer: str | None = None,
    title: str | None = None,
) -> tuple[Paper, PaperSource]:
    """Create a paper that only has metadata (``AWAITING_FILE``).

    The fingerprint comes from the primary identifier when there is one (so the
    paper is already findable by DOI), otherwise from the usual title/author/year
    ladder. No chunks and no ``paper_files`` row are created: the paper is not
    indexed until its PDF shows up.
    """
    identifier_values = {
        field.split(":", 1)[1]: value
        for field, value in values.items()
        if field.startswith("identifier:") and value
    }
    resolved_title = str(title or values.get("title") or "").strip()
    authors = values.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    fingerprint = identifiers.build_fingerprint_from_identifiers(
        [
            {"scheme": scheme, "normalized_value": identifiers.normalize_identifier(scheme, value)}
            for scheme, value in identifier_values.items()
        ],
        title=resolved_title,
        first_author=authors[0] if authors else None,
        year=values.get("year"),
    )
    # The row starts with a blank title so the merge below can *claim* the real
    # one: a value that is already on the paper is "fill blanks only" and would
    # never produce a provenance row, leaving the shell with a title nobody
    # claims to have stated.
    paper = Paper(
        id=new_uuid(),
        title="",
        fingerprint=fingerprint,
        status=paper_service.STATUS_AWAITING_FILE,
    )
    session.add(paper)
    session.flush()
    source = sources.upsert_source(
        session,
        source_type=source_type,
        source_ref=source_ref,
        raw=raw or {},
        paper_id=paper.id,
        content_type=content_type,
        match_status=sources.MATCH_STATUS_MATCHED,
        match_method="shell",
        match_confidence=1.0,
        importer=importer,
    )
    merge.merge_values(
        session,
        paper,
        values,
        source_type=source_type,
        source_id=source.id,
        confidence=1.0,
    )
    if not (paper.title or "").strip():
        # ``papers.title`` is NOT NULL: a record without a usable title still needs
        # something to show in a list. This is a placeholder, not a claim.
        paper.title = resolved_title or "untitled (awaiting file)"
        session.flush()
    identifiers.mirror_legacy_columns(session, paper)
    session.flush()
    logger.info(
        "shell paper created",
        extra={
            "extra_fields": {
                "paper_id": paper.id,
                "source_ref": source_ref,
                "fingerprint": fingerprint,
            }
        },
    )
    return paper, source


def shell_values_from_match(
    values: Mapping[str, Any], *, fallback_title: str | None = None
) -> dict[str, Any]:
    """Normalize claim values before a shell is created.

    A shell needs *something* to show in a list, so a title-less record falls back
    to the file name or a placeholder instead of an empty string (``papers.title``
    is ``NOT NULL``).
    """
    prepared = dict(values)
    if not str(prepared.get("title") or "").strip():
        prepared["title"] = (fallback_title or "untitled (awaiting file)").strip()
    return prepared


def adopt_paper(
    session: Session,
    paper: Paper,
    target: Paper,
    *,
    source_id: str | None = None,
) -> Paper:
    """Move the freshly ingested file onto ``target`` and drop the throwaway row.

    This is the "reuse the same ``paper_id``" step: the PDF is attached to the
    paper that already exists (a shell, or a paper that already has another
    version), the MinIO object is re-keyed under the surviving paper, and the
    paper row created by this ingest is deleted -- it never had a chunk, an index
    document or a provenance row of its own, so nothing is lost.
    """
    moved = 0
    for record in paper_service.live_files(paper):
        new_key = record.object_key
        prefix = f"{object_storage.ORIGINAL_PREFIX}/{paper.id}/"
        if record.object_key.startswith(prefix):
            candidate = f"{object_storage.ORIGINAL_PREFIX}/{target.id}/{record.object_key[len(prefix):]}"
            if object_storage.move_object(record.object_key, candidate):
                new_key = candidate
        record.paper_id = target.id
        record.object_key = new_key
        record.is_primary = False
        if source_id and not record.source_id:
            record.source_id = source_id
        moved += 1
    session.flush()

    # The files moved, so the ORM's cached collection is wrong: expire it before
    # deleting the row, or the cascade would delete the rows we just re-pointed.
    session.expire(paper, ["files"])
    session.delete(paper)
    session.flush()

    if paper_service.is_shell(target):
        target.status = paper_service.STATUS_PENDING
        session.flush()
    logger.info(
        "shell adopted by an arriving PDF",
        extra={
            "extra_fields": {
                "paper_id": target.id,
                "discarded_paper_id": paper.id,
                "files_moved": moved,
            }
        },
    )
    return target


def shell_ids(session: Session, limit: int = 200) -> list[Paper]:
    """Papers still waiting for their PDF (housekeeping/reporting helper)."""
    from sqlalchemy import select

    statement = (
        select(Paper)
        .where(
            Paper.status == paper_service.STATUS_AWAITING_FILE,
            Paper.deleted_at.is_(None),
        )
        .order_by(Paper.created_at.asc())
        .limit(max(1, min(limit, 500)))
    )
    return list(session.execute(statement).scalars().all())


def current_values(paper: Paper) -> dict[str, Any]:
    """Claim-shaped values of a paper (used when merging a shell into a record)."""
    return merge.field_values_from_paper(paper)


def attach_source_to_shell(
    session: Session, source: PaperSource, shell: Paper
) -> PaperSource:
    """Point an unmatched source record at the shell created for it."""
    return sources.attach_source(session, source, shell, method="shell")


__all__ = [
    "adopt_paper",
    "attach_source_to_shell",
    "create_shell",
    "current_values",
    "shell_ids",
    "shell_values_from_match",
]