"""Reconcile metadata-only shells against papers that already have PDFs."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    IngestionJob,
    Paper,
    PaperChunk,
    PaperFile,
    PaperIdentifier,
    PaperSource,
)
from app.services import metadata_identifiers as identifiers
from app.services import metadata_import
from app.services import metadata_matcher as matcher
from app.services import metadata_merge as merge
from app.services import metadata_sources as sources
from app.services import paper_service


@dataclass
class ReconcileReport:
    total: int = 0
    merged: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run: bool = True
    items: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "merged": self.merged,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "failed": self.failed,
            "dry_run": self.dry_run,
            "items": self.items,
        }


def reconcile_shells(
    session: Session, *, apply: bool = False, limit: int | None = None
) -> ReconcileReport:
    """Merge uniquely identified shells and delete safe unmatched shells.

    Dry-run executes the same mutations inside a savepoint and rolls them back,
    so its counts are the exact plan for an unchanged database.
    """
    statement = (
        select(Paper)
        .where(
            Paper.status == paper_service.STATUS_AWAITING_FILE,
            Paper.deleted_at.is_(None),
        )
        .order_by(Paper.created_at.asc(), Paper.id.asc())
    )
    if limit is not None:
        statement = statement.limit(max(0, limit))
    shells = list(session.execute(statement).scalars())

    if apply:
        return _run(session, shells)

    savepoint = session.begin_nested()
    try:
        report = _run(session, shells)
        report.dry_run = True
        return report
    finally:
        if savepoint.is_active:
            savepoint.rollback()
        session.expire_all()


def _run(session: Session, shells: list[Paper]) -> ReconcileReport:
    report = ReconcileReport(total=len(shells), dry_run=False)
    for shell in shells:
        try:
            with session.begin_nested():
                _reconcile_one(session, shell, report)
        except Exception as exc:  # noqa: BLE001 - isolate one corrupt shell
            report.failed += 1
            report.items.append(
                {
                    "shell_id": shell.id,
                    "action": "failed",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
    session.flush()
    return report


def _dependent_counts(session: Session, paper_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, model in (
        ("files", PaperFile),
        ("chunks", PaperChunk),
        ("jobs", IngestionJob),
    ):
        count = int(
            session.scalar(
                select(func.count()).select_from(model).where(model.paper_id == paper_id)
            )
            or 0
        )
        if count:
            counts[label] = count
    return counts


def _reconcile_one(
    session: Session, shell: Paper, report: ReconcileReport
) -> None:
    dependents = _dependent_counts(session, shell.id)
    if dependents:
        report.skipped += 1
        report.items.append(
            {
                "shell_id": shell.id,
                "action": "skipped",
                "reason": "AWAITING_FILE row has non-shell dependents",
                "dependents": dependents,
            }
        )
        return

    source_rows = sources.sources_for_paper(session, shell.id)
    parsed_rows: list[tuple[PaperSource, metadata_import.ParsedRecord]] = []
    matches: list[matcher.MatchResult] = []
    parse_errors: list[str] = []
    for source in source_rows:
        try:
            parsed = metadata_import.parse_records([source.raw])[0]
        except Exception as exc:  # noqa: BLE001 - recorded and deleted with shell
            parse_errors.append(f"{source.id}: {type(exc).__name__}: {exc}")
            continue
        parsed_rows.append((source, parsed))
        result = matcher.match_record(
            session, parsed.match_input(), exclude_paper_id=shell.id
        )
        if (
            result.matched
            and result.paper is not None
            and not paper_service.is_shell(result.paper)
        ):
            matches.append(result)

    targets = {result.paper_id: result for result in matches if result.paper_id}
    if len(targets) == 1:
        result = next(iter(targets.values()))
        target = result.paper
        assert target is not None
        _merge_shell(session, shell, target, parsed_rows, result)
        report.merged += 1
        report.items.append(
            {
                "shell_id": shell.id,
                "action": "merged",
                "paper_id": target.id,
                "match_method": result.method,
                "source_refs": [source.source_ref for source, _parsed in parsed_rows],
            }
        )
        return

    session.delete(shell)
    session.flush()
    report.deleted += 1
    reason = "no unique non-shell match"
    if len(targets) > 1:
        reason = "sources point to multiple non-shell papers"
    elif parse_errors and not parsed_rows:
        reason = "stored sources could not be parsed"
    report.items.append(
        {
            "shell_id": shell.id,
            "action": "deleted",
            "reason": reason,
            "parse_errors": parse_errors,
            "source_refs": [source.source_ref for source in source_rows],
        }
    )


def _merge_shell(
    session: Session,
    shell: Paper,
    target: Paper,
    parsed_rows: list[tuple[PaperSource, metadata_import.ParsedRecord]],
    result: matcher.MatchResult,
) -> None:
    # Release globally unique identifiers before replaying them onto the target.
    session.query(PaperIdentifier).filter(PaperIdentifier.paper_id == shell.id).delete(
        synchronize_session=False
    )
    for source, _parsed in parsed_rows:
        sources.attach_source(
            session,
            source,
            target,
            method=result.method or matcher.METHOD_MANUAL,
            confidence=result.confidence or 1.0,
        )
    session.expire(shell, ["sources", "identifiers"])
    session.delete(shell)
    session.flush()

    for source, parsed in parsed_rows:
        merge.merge_values(
            session,
            target,
            parsed.values,
            source_type=source.source_type,
            source_id=source.id,
            confidence=source.match_confidence or result.confidence or 1.0,
        )
    identifiers.refresh_primary(session, target.id)
    identifiers.mirror_legacy_columns(session, target)
    sha256 = next((parsed.sha256 for _source, parsed in parsed_rows if parsed.sha256), None)
    identifiers.upgrade_fingerprint(session, target, sha256=sha256)


__all__ = ["ReconcileReport", "reconcile_shells"]
