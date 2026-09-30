"""Merge engine, rule R2 (decision 11): fill blanks, never rank sources.

The rules, in order:

1. **Fill blanks only.** When ``papers`` already holds a value, the incoming
   record does not replace it; its claim is still recorded (as history) and the
   disagreement is reported as a conflict.
2. **One exception.** A value that came from ``pdf_heuristic`` may be replaced by
   any *structured* source (``ieee_api`` / ``arxiv_api`` / ``crossref`` /
   ``import_file`` / ``pdf_embedded`` / ``manual``). That is the whole point of
   the layer: the 68 existing papers were described by heuristics, so a real
   record from IEEE has to be able to correct them.
3. **No authority ranking between structured sources.** IEEE vs. arXiv: first
   writer keeps the field, the other one is logged.
4. **Field specifics.** ``abstract`` keeps the longest text (sources truncate
   differently), ``authors`` keeps the longest list, ``year`` keeps the current
   value on a conflict.
5. Nothing is ever deleted: the loser is a row in ``paper_field_provenance`` with
   ``is_current=false``, so anything can be rolled back.

There is deliberately no "which source is more trustworthy" table -- deciding
that for every new platform is the maintenance burden this design avoids.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper, PaperSource
from app.services import metadata_sources as sources
from app.services import provenance_service as prov

logger = get_logger(__name__)

#: Source-type vocabulary lives in :mod:`app.services.metadata_sources`; the merge
#: engine only classifies it, so the names are imported rather than re-declared.
SOURCE_TYPES: tuple[str, ...] = sources.SOURCE_TYPES
SOURCE_TYPE_IEEE_API = sources.SOURCE_TYPE_IEEE_API
SOURCE_TYPE_ARXIV_API = sources.SOURCE_TYPE_ARXIV_API
SOURCE_TYPE_CROSSREF = sources.SOURCE_TYPE_CROSSREF
SOURCE_TYPE_PDF_EMBEDDED = sources.SOURCE_TYPE_PDF_EMBEDDED
SOURCE_TYPE_PDF_HEURISTIC = sources.SOURCE_TYPE_PDF_HEURISTIC
SOURCE_TYPE_FILENAME = sources.SOURCE_TYPE_FILENAME
SOURCE_TYPE_IMPORT_FILE = sources.SOURCE_TYPE_IMPORT_FILE
SOURCE_TYPE_MANUAL = sources.SOURCE_TYPE_MANUAL


#: Sources whose values may correct a heuristic value (rule 2).
STRUCTURED_SOURCE_TYPES: frozenset[str] = frozenset(
    {
        SOURCE_TYPE_IEEE_API,
        SOURCE_TYPE_ARXIV_API,
        SOURCE_TYPE_CROSSREF,
        SOURCE_TYPE_PDF_EMBEDDED,
        SOURCE_TYPE_IMPORT_FILE,
        SOURCE_TYPE_MANUAL,
    }
)

#: The sources whose values can be corrected by rule 2: the first-page
#: heuristics and the file name (both are guesses about the document, while
#: ``pdf_embedded`` / ``arxiv_api`` / ``manual`` actually know something).
WEAK_SOURCE_TYPES: frozenset[str] = frozenset(
    {SOURCE_TYPE_PDF_HEURISTIC, SOURCE_TYPE_FILENAME}
)

#: Backwards-compatible alias (older call sites and docs use this name).
HEURISTIC_SOURCE_TYPES: frozenset[str] = WEAK_SOURCE_TYPES

#: ``manual`` is the one source R2 does not constrain (decision 12).
UNRESTRICTED_SOURCE_TYPES: frozenset[str] = frozenset({SOURCE_TYPE_MANUAL})

ACTION_FILLED = "filled"
ACTION_OVERRIDDEN = "overridden"
ACTION_CONFLICT = "conflict"
ACTION_UNCHANGED = "unchanged"
ACTION_SPECIAL = "special_rule"

#: Fields with a field-specific tie-breaker (rule 4).
ABSTRACT_FIELD = prov.FIELD_ABSTRACT
AUTHORS_FIELD = prov.FIELD_AUTHORS
YEAR_FIELD = prov.FIELD_YEAR


@dataclass
class MergeDecision:
    """What the merge did (or would do) for one field."""

    field: str
    action: str
    kept: Any = None
    rejected: Any = None
    reason: str = ""
    source_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "action": self.action,
            "kept": self.kept,
            "rejected": self.rejected,
            "reason": self.reason,
            "source_type": self.source_type,
        }


@dataclass
class MergeReport:
    """Every decision of one record's merge, plus the per-field outcome."""

    decisions: list[MergeDecision] = dataclass_field(default_factory=list)

    @property
    def conflicts(self) -> list[MergeDecision]:
        return [item for item in self.decisions if item.action == ACTION_CONFLICT]

    @property
    def applied(self) -> list[MergeDecision]:
        return [
            item
            for item in self.decisions
            if item.action in (ACTION_FILLED, ACTION_OVERRIDDEN, ACTION_SPECIAL)
        ]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.decisions]


def is_structured(source_type: str | None) -> bool:
    """Whether a source type is "structured" for rule 2."""
    return (source_type or "").strip().lower() in STRUCTURED_SOURCE_TYPES


def is_heuristic(source_type: str | None) -> bool:
    """Whether a source type is a *weak* one a structured source may correct."""
    return (source_type or "").strip().lower() in HEURISTIC_SOURCE_TYPES


def current_source_type(session: Session, paper: Paper, field: str) -> str | None:
    """Source type behind the value that currently holds ``field``.

    A value with no claim row is treated as ``pdf_heuristic``: everything that
    existed before this layer was written by the first-page heuristics (the
    backfill labels those rows exactly the same way), and treating them as
    protected would make the whole layer pointless for the existing library.
    """
    claim = prov.current_claim(session, paper.id, field)
    if claim is None or claim.source_id is None:
        return SOURCE_TYPE_PDF_HEURISTIC if claim is None else None
    source = session.get(PaperSource, claim.source_id)
    if source is None:
        return None
    return (source.source_type or "").strip().lower()


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def _special_winner(field_name: str, current: Any, incoming: Any) -> bool:
    """Whether a field-specific rule prefers the incoming value on a conflict."""
    if field_name == ABSTRACT_FIELD:
        return len(str(incoming)) > len(str(current))
    if field_name == AUTHORS_FIELD:
        return len(list(incoming)) > len(list(current))
    return False


def decide(
    session: Session,
    paper: Paper,
    field_name: str,
    value: Any,
    *,
    source_type: str | None,
) -> MergeDecision:
    """Compute the merge outcome for one field without touching any row."""
    incoming_type = (source_type or "").strip().lower() or None
    if _is_blank(value):
        return MergeDecision(
            field=field_name,
            action=ACTION_UNCHANGED,
            kept=prov.read_field(paper, field_name),
            reason="empty value",
            source_type=incoming_type,
        )

    current_claim = prov.current_claim(session, paper.id, field_name)
    current_value = prov.read_field(paper, field_name)
    if _is_blank(current_value):
        return MergeDecision(
            field=field_name,
            action=ACTION_FILLED,
            kept=value,
            reason="blank",
            source_type=incoming_type,
        )

    same = prov._same_value(current_value, value) or _same_value_deep(current_value, value)
    if same:
        return MergeDecision(
            field=field_name,
            action=ACTION_UNCHANGED,
            kept=current_value,
            reason="same value",
            source_type=incoming_type,
        )

    existing_type = current_source_type(session, paper, field_name)
    if incoming_type in UNRESTRICTED_SOURCE_TYPES:
        return MergeDecision(
            field=field_name,
            action=ACTION_OVERRIDDEN,
            kept=value,
            rejected=current_value,
            reason="manual override",
            source_type=incoming_type,
        )
    if is_heuristic(existing_type) and is_structured(incoming_type):
        return MergeDecision(
            field=field_name,
            action=ACTION_OVERRIDDEN,
            kept=value,
            rejected=current_value,
            reason="structured source overrides a weak source",
            source_type=incoming_type,
        )
    if _special_winner(field_name, current_value, value):
        return MergeDecision(
            field=field_name,
            action=ACTION_SPECIAL,
            kept=value,
            rejected=current_value,
            reason=f"{field_name}: the longer/more complete value wins",
            source_type=incoming_type,
        )
    return MergeDecision(
        field=field_name,
        action=ACTION_CONFLICT,
        kept=current_value,
        rejected=value,
        reason="keep the current value and record the disagreement",
        source_type=incoming_type,
    )


def _same_value_deep(left: Any, right: Any) -> bool:
    """Structural equality for list/dict claims (JSON round-trip tolerant)."""
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(_same_value_deep(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _same_value_deep(a, b) for a, b in zip(left, right)
        )
    return prov._same_value(left, right)


def apply_decision(
    session: Session,
    paper: Paper,
    decision: MergeDecision,
    value: Any,
    *,
    source_id: str | None = None,
    confidence: float | None = None,
) -> MergeDecision:
    """Publish or archive ``value`` according to ``decision``."""
    if decision.action == ACTION_UNCHANGED:
        return decision

    if decision.action == ACTION_FILLED:
        prov.set_field(
            session,
            paper,
            decision.field,
            value,
            source_id=source_id,
            confidence=confidence,
            decided_by=prov.DECIDED_INITIAL,
        )
        return decision

    if decision.action in (ACTION_OVERRIDDEN, ACTION_SPECIAL):
        if decision.reason == "manual override":
            decided_by = prov.DECIDED_MANUAL
        elif decision.action == ACTION_OVERRIDDEN or is_structured(decision.source_type):
            decided_by = prov.DECIDED_STRUCTURED_OVERRIDE
        else:
            decided_by = prov.DECIDED_INITIAL
        prov.set_field(
            session,
            paper,
            decision.field,
            value,
            source_id=source_id,
            confidence=confidence,
            decided_by=decided_by,
            override=True,
        )
        return decision

    # Conflict: keep the current value, file the other one as history.
    prov.record_claim(
        session,
        paper_id=paper.id,
        field=decision.field,
        value=value,
        source_id=source_id,
        confidence=confidence,
        make_current=False,
    )
    return decision


def merge_values(
    session: Session,
    paper: Paper,
    values: Mapping[str, Any],
    *,
    source_type: str | None,
    source_id: str | None = None,
    confidence: float | None = None,
    dry_run: bool = False,
) -> MergeReport:
    """Merge a mapping of ``field -> value`` into a paper (rule R2).

    ``dry_run=True`` computes every decision and writes nothing, which is what the
    importer's default ``dry_run`` reports are built from.
    """
    report = MergeReport()
    for field_name, value in values.items():
        decision = decide(session, paper, field_name, value, source_type=source_type)
        report.decisions.append(decision)
        if dry_run or decision.action == ACTION_UNCHANGED:
            continue
        apply_decision(
            session,
            paper,
            decision,
            value,
            source_id=source_id,
            confidence=confidence,
        )
    session.flush()
    return report


def field_values_from_paper(paper: Paper) -> dict[str, Any]:
    """Current claim-shaped values of the fields a source may state."""
    values: dict[str, Any] = {}
    for field_name in (
        prov.FIELD_TITLE,
        prov.FIELD_ABSTRACT,
        prov.FIELD_YEAR,
        prov.FIELD_VOLUME,
        prov.FIELD_ISSUE,
        prov.FIELD_PAGES,
        prov.FIELD_PUBLICATION_DATE,
        prov.FIELD_PAPER_TYPE,
        prov.FIELD_URL,
        prov.FIELD_AUTHORS,
        prov.FIELD_VENUE,
    ):
        value = prov.read_field(paper, field_name)
        if not _is_blank(value):
            values[field_name] = value
    return values


def conflict_report(report: MergeReport) -> list[dict[str, Any]]:
    """The ``conflicts`` block of an import report."""
    return [
        {
            "field": item.field,
            "kept": item.kept,
            "rejected": item.rejected,
            "source": item.source_type,
            "reason": item.reason,
        }
        for item in report.conflicts
    ]


def decisions_summary(decisions: Sequence[MergeDecision]) -> dict[str, int]:
    """Count decisions by action (report/telemetry helper)."""
    summary: dict[str, int] = {}
    for item in decisions:
        summary[item.action] = summary.get(item.action, 0) + 1
    return summary


__all__ = [
    "ABSTRACT_FIELD",
    "ACTION_CONFLICT",
    "ACTION_FILLED",
    "ACTION_OVERRIDDEN",
    "ACTION_SPECIAL",
    "ACTION_UNCHANGED",
    "AUTHORS_FIELD",
    "HEURISTIC_SOURCE_TYPES",
    "WEAK_SOURCE_TYPES",
    "SOURCE_TYPE_FILENAME",
    "SOURCE_TYPES",
    "SOURCE_TYPE_ARXIV_API",
    "SOURCE_TYPE_CROSSREF",
    "SOURCE_TYPE_IEEE_API",
    "SOURCE_TYPE_IMPORT_FILE",
    "SOURCE_TYPE_MANUAL",
    "SOURCE_TYPE_PDF_EMBEDDED",
    "SOURCE_TYPE_PDF_HEURISTIC",
    "STRUCTURED_SOURCE_TYPES",
    "UNRESTRICTED_SOURCE_TYPES",
    "YEAR_FIELD",
    "MergeDecision",
    "MergeReport",
    "apply_decision",
    "conflict_report",
    "current_source_type",
    "decide",
    "decisions_summary",
    "field_values_from_paper",
    "is_heuristic",
    "is_structured",
    "merge_values",
]