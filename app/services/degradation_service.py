"""Degradation ledger: what each stage gave up on, per paper (plan T7.3).

A degraded result is not a failure -- docling being unreachable falls back to
pypdf, a refused embedding call falls back to length chunking, and the paper is
indexed anyway. That is the right call, but until now the only trace was a
``WARNING`` in the log, so nobody could answer "which papers should be re-run
once the missing service is back?".

Every stage reports through one sink signature::

    sink(stage, code, detail)        # e.g. ("chunking", "semantic_fallback", {...})

``Recorder`` is the sink bound to a ``(session, paper_id, job)``; the parsing and
chunking layers only ever see the plain callable, so they stay import-free of
this module and testable without a database. ``recorder.resolve(stage)`` marks
the rows of a stage that this run did *not* report as resolved -- the row stays
for the audit trail, the paper stops being selected by ``reindex --degraded``.

Register a new stage in :data:`STAGES` (and a code constant next to the code that
emits it): ``record()`` rejects unknown stages so a typo cannot quietly invent
one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import PaperDegradation

logger = get_logger(__name__)

# --------------------------------------------------------------------------- #
# stage vocabulary -- every stage that can produce a thinner-than-ideal result
# --------------------------------------------------------------------------- #
STAGE_PARSING = "parsing"
STAGE_CHUNKING = "chunking"
STAGE_EMBEDDING = "embedding"
STAGE_INDEXING = "indexing"

STAGES: frozenset[str] = frozenset(
    {STAGE_PARSING, STAGE_CHUNKING, STAGE_EMBEDDING, STAGE_INDEXING}
)

#: ``(stage, code, detail) -> None``; the only thing the parsing/chunking layers
#: know about this module (they accept it as an optional keyword argument).
DegradeSink = Callable[[str, str, dict[str, Any]], None]

CODE_MAX_LENGTH = 64


def _clean_stage(stage: str) -> str:
    value = (stage or "").strip().lower()
    if value not in STAGES:
        raise ValueError(f"unknown degradation stage {stage!r}; add it to STAGES")
    return value


def _clean_code(code: str) -> str:
    value = (code or "").strip()
    if not value:
        raise ValueError("degradation code must not be empty")
    if len(value) > CODE_MAX_LENGTH:
        raise ValueError(f"degradation code {value!r} exceeds {CODE_MAX_LENGTH} chars")
    return value


def _clean_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    if detail is None:
        return {}
    if not isinstance(detail, dict):  # pragma: no cover - defensive
        raise TypeError("degradation detail must be a dict")
    return dict(detail)


def record(
    session: Session,
    *,
    paper_id: str,
    stage: str,
    code: str,
    detail: dict[str, Any] | None = None,
    job_id: str | None = None,
    now: datetime | None = None,
) -> PaperDegradation:
    """Upsert one ``(paper, stage, code)`` row and return it.

    Repeated reports bump ``occurrences``/``last_seen_at`` and refresh the
    detail; a re-occurrence after a clean run clears ``resolved_at`` again. The
    caller commits (``flush`` only, so it participates in the job transaction).
    """
    stage = _clean_stage(stage)
    code = _clean_code(code)
    detail = _clean_detail(detail)
    moment = now or datetime.now(timezone.utc)

    row = session.execute(
        select(PaperDegradation).where(
            PaperDegradation.paper_id == paper_id,
            PaperDegradation.stage == stage,
            PaperDegradation.code == code,
        )
    ).scalar_one_or_none()

    if row is None:
        row = PaperDegradation(
            paper_id=paper_id,
            stage=stage,
            code=code,
            detail=detail,
            occurrences=1,
            first_seen_at=moment,
            last_seen_at=moment,
            job_id=job_id,
        )
        session.add(row)
    else:
        row.occurrences = (row.occurrences or 0) + 1
        row.last_seen_at = moment
        row.detail = detail
        row.resolved_at = None
        if job_id:
            row.job_id = job_id
    session.flush()
    return row


def resolve_stage(
    session: Session,
    *,
    paper_id: str,
    stage: str,
    keep: Iterable[str] = (),
    now: datetime | None = None,
) -> int:
    """Mark the stage's *open* rows that are not in ``keep`` as resolved.

    Returns how many rows were resolved. A stage that runs clean therefore
    clears its own history without touching other stages' rows.
    """
    stage = _clean_stage(stage)
    kept = {_clean_code(code) for code in keep}
    moment = now or datetime.now(timezone.utc)

    rows = list(
        session.execute(
            select(PaperDegradation).where(
                PaperDegradation.paper_id == paper_id,
                PaperDegradation.stage == stage,
                PaperDegradation.resolved_at.is_(None),
            )
        ).scalars()
    )
    resolved = 0
    for row in rows:
        if row.code in kept:
            continue
        row.resolved_at = moment
        resolved += 1
    if resolved:
        session.flush()
    return resolved


def list_for_paper(
    session: Session,
    paper_id: str,
    *,
    include_resolved: bool = False,
) -> list[PaperDegradation]:
    """Oldest first; unresolved only unless ``include_resolved``."""
    statement = select(PaperDegradation).where(PaperDegradation.paper_id == paper_id)
    if not include_resolved:
        statement = statement.where(PaperDegradation.resolved_at.is_(None))
    statement = statement.order_by(
        PaperDegradation.stage, PaperDegradation.first_seen_at
    )
    return list(session.execute(statement).scalars())


def open_degradations(
    session: Session,
    *,
    paper_id: str | None = None,
    stage: str | None = None,
    code: str | None = None,
) -> list[PaperDegradation]:
    """Every unresolved row, optionally narrowed to a paper/stage/code."""
    statement = select(PaperDegradation).where(PaperDegradation.resolved_at.is_(None))
    if paper_id:
        statement = statement.where(PaperDegradation.paper_id == paper_id)
    if stage:
        statement = statement.where(PaperDegradation.stage == _clean_stage(stage))
    if code:
        statement = statement.where(PaperDegradation.code == _clean_code(code))
    statement = statement.order_by(
        PaperDegradation.paper_id,
        PaperDegradation.stage,
        PaperDegradation.first_seen_at,
    )
    return list(session.execute(statement).scalars())


def paper_ids_with_open_degradations(
    session: Session,
    *,
    stage: str | None = None,
    code: str | None = None,
) -> set[str]:
    """Set of paper ids that currently have an unresolved degradation."""
    statement = select(PaperDegradation.paper_id).where(
        PaperDegradation.resolved_at.is_(None)
    )
    if stage:
        statement = statement.where(PaperDegradation.stage == _clean_stage(stage))
    if code:
        statement = statement.where(PaperDegradation.code == _clean_code(code))
    return {row[0] for row in session.execute(statement.distinct())}


class Recorder:
    """A :data:`DegradeSink` bound to a paper, remembering what it was told.

    ``pipeline`` code builds one per job, hands it to the stages that can
    degrade, then calls :meth:`resolve` per stage that ran to completion. The
    recorder never commits: it rides along in the caller's transaction, so a
    rolled-back job leaves no degradation row behind.
    """

    def __init__(
        self,
        session: Session,
        *,
        paper_id: str,
        job_id: str | None = None,
    ) -> None:
        self.session = session
        self.paper_id = paper_id
        self.job_id = job_id
        self._seen: dict[str, set[str]] = {}

    def __call__(
        self, stage: str, code: str, detail: dict[str, Any] | None = None
    ) -> None:
        # Bookkeeping about a degradation must never be able to fail the very
        # run it describes: a bad stage/code is logged and dropped here, while
        # ``record()`` itself stays strict so a typo is caught by the caller's
        # tests instead of on a customer paper.
        try:
            record(
                self.session,
                paper_id=self.paper_id,
                stage=stage,
                code=code,
                detail=detail,
                job_id=self.job_id,
            )
        except Exception as exc:  # noqa: BLE001 - never fail the pipeline
            logger.error(
                "could not record degradation",
                extra={
                    "extra_fields": {
                        "paper_id": self.paper_id,
                        "stage": stage,
                        "code": code,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                },
            )
            return
        self._seen.setdefault(_clean_stage(stage), set()).add(_clean_code(code))
        logger.warning(
            "stage degraded",
            extra={
                "extra_fields": {
                    "paper_id": self.paper_id,
                    "job_id": self.job_id,
                    "stage": stage,
                    "code": code,
                    "detail": detail or {},
                }
            },
        )

    def seen(self, stage: str) -> frozenset[str]:
        """Codes reported for ``stage`` during this run (empty when clean)."""
        return frozenset(self._seen.get(_clean_stage(stage), set()))

    def resolve(self, stage: str) -> int:
        """Resolve the stage's rows this run did not report (never raises)."""
        try:
            return resolve_stage(
                self.session,
                paper_id=self.paper_id,
                stage=stage,
                keep=self.seen(stage),
            )
        except Exception as exc:  # noqa: BLE001 - see __call__
            logger.error(
                "could not resolve degradations",
                extra={
                    "extra_fields": {
                        "paper_id": self.paper_id,
                        "stage": stage,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                },
            )
            return 0


def stages_for_report(rows: Sequence[PaperDegradation]) -> dict[str, list[str]]:
    """``{stage: [code, ...]}`` for printing/api payloads (stable order)."""
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row.stage, [])
        if row.code not in grouped[row.stage]:
            grouped[row.stage].append(row.code)
    return {stage: sorted(codes) for stage, codes in sorted(grouped.items())}


__all__ = [
    "CODE_MAX_LENGTH",
    "DegradeSink",
    "Recorder",
    "STAGES",
    "STAGE_CHUNKING",
    "STAGE_EMBEDDING",
    "STAGE_INDEXING",
    "STAGE_PARSING",
    "list_for_paper",
    "open_degradations",
    "paper_ids_with_open_degradations",
    "record",
    "resolve_stage",
    "stages_for_report",
]
