"""Read-only audit: the ``papers.doi`` / ``papers.arxiv_id`` mirrors vs their
``paper_identifiers`` source of truth.

The mirrors are maintained by the application (``mirror_legacy_columns`` in
``provenance_service`` / ``metadata_identifiers``), not by the database: nothing
makes the two *impossible* to drift apart. This module is the watchdog that
makes drift *visible* without trying to prevent it -- strictly read-only, never
writes, never repairs.

What "consistent" means for one identifier scheme (``doi`` / ``arxiv``):

* the mirror column equals the ``normalized_value`` of the paper's identifier of
  that scheme (``papers.doi`` / ``papers.arxiv_id`` are stored normalized);
* there is at most one such identifier per paper (the schema enforces it, but a
  hand-written row could still surprise the mirror);
* a mirror value belongs to a live paper, and no identifier points at a
  soft-deleted or missing paper.

The check is cheap: two narrow queries, compared in Python. It is exposed as a
read-only endpoint (``GET /api/consistency/doi``) and a CLI
(``scripts/audit_identifier_mirrors.py``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Paper, PaperIdentifier
from app.db.session import SessionLocal

#: ``mirror column -> identifier scheme``. ``papers.doi`` mirrors scheme ``doi``;
#: ``papers.arxiv_id`` mirrors scheme ``arxiv``. Both store the normalized form.
MIRRORED_SCHEMES: tuple[tuple[str, str], ...] = (
    ("doi", "doi"),
    ("arxiv_id", "arxiv"),
)

#: Categories of drift, spelled out so an operator can act on each:
#:   mirror_without_identifier -- the column has a value but no identifier row
#:   identifier_without_mirror -- an identifier row exists but the column is empty
#:   value_mismatch            -- both exist but differ (should be normalization-only)
#:   duplicate_identifier      -- more than one identifier of the scheme on one paper
#:   orphan_identifier         -- identifier points at a missing/soft-deleted paper
#:   missing_normalized_value  -- an identifier row without a normalized_value
DRIFT_KINDS: tuple[str, ...] = (
    "mirror_without_identifier",
    "identifier_without_mirror",
    "value_mismatch",
    "duplicate_identifier",
    "orphan_identifier",
    "missing_normalized_value",
)


@dataclass
class MirrorDrift:
    """One paper/identifier pair that does not line up."""

    kind: str
    scheme: str
    paper_id: str
    mirror_value: Any = None
    identifier_values: list[str] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scheme": self.scheme,
            "paper_id": self.paper_id,
            "mirror_value": self.mirror_value,
            "identifier_values": list(self.identifier_values),
            "detail": self.detail,
        }


@dataclass
class IdentifierMirrorReport:
    """The whole answer of the audit."""

    checked_at: str
    papers_total: int = 0
    papers_live: int = 0
    #: scheme -> number of papers with a non-empty mirror for that scheme.
    mirrors_set: dict[str, int] = field(default_factory=dict)
    #: scheme -> number of identifier rows of that scheme (live papers only).
    identifiers_total: dict[str, int] = field(default_factory=dict)
    drifts: list[MirrorDrift] = field(default_factory=list)
    #: kind -> count (totals stay exact even when ``drifts`` is capped).
    drift_totals: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def consistent(self) -> bool:
        return not (self.drift_totals or self.errors)

    def add_drift(self, drift: MirrorDrift, limit: int) -> None:
        """Count a drift always; list it only up to ``limit``."""
        self.drift_totals[drift.kind] = self.drift_totals.get(drift.kind, 0) + 1
        if len(self.drifts) < limit:
            self.drifts.append(drift)
        else:
            self.truncated = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "consistent": self.consistent,
            "papers_total": self.papers_total,
            "papers_live": self.papers_live,
            "mirrors_set": dict(sorted(self.mirrors_set.items())),
            "identifiers_total": dict(sorted(self.identifiers_total.items())),
            "drift_totals": dict(sorted(self.drift_totals.items())),
            "drifts": [drift.as_dict() for drift in self.drifts],
            "errors": list(self.errors),
            "truncated": self.truncated,
        }


def audit_identifier_mirrors(
    session_factory: Callable[[], Session] = SessionLocal,
    *,
    limit: int = 50,
) -> IdentifierMirrorReport:
    """Compare the mirror columns against ``paper_identifiers`` (read-only).

    ``limit`` caps how many drift rows are listed; ``drift_totals`` stays exact.
    Never raises: a store error lands in ``errors``.
    """
    from datetime import datetime, timezone

    started = datetime.now(timezone.utc)
    report = IdentifierMirrorReport(
        checked_at=started.isoformat(),
        mirrors_set={scheme: 0 for _, scheme in MIRRORED_SCHEMES},
        identifiers_total={scheme: 0 for _, scheme in MIRRORED_SCHEMES},
    )

    session = session_factory()
    try:
        # 1) live papers: mirror columns only (narrow projection).
        mirror_columns = [getattr(Paper, column) for column, _ in MIRRORED_SCHEMES]
        papers = session.execute(
            select(Paper.id, Paper.deleted_at, *mirror_columns)
        ).all()
        report.papers_total = len(papers)
        live_ids: set[str] = set()
        mirror_by_paper: dict[str, dict[str, Any]] = {}
        for row in papers:
            paper_id = str(row[0])
            deleted = row[1] is not None
            values = {
                scheme: row[index + 2]
                for index, (_, scheme) in enumerate(MIRRORED_SCHEMES)
            }
            if not deleted:
                live_ids.add(paper_id)
                mirror_by_paper[paper_id] = values
                for scheme, value in values.items():
                    if (value or "").strip():
                        report.mirrors_set[scheme] += 1
        report.papers_live = len(live_ids)

        # 2) every identifier row of the mirrored schemes (any paper, so orphans
        #    and deleted-paper rows are seen too).
        schemes = [scheme for _, scheme in MIRRORED_SCHEMES]
        id_rows = session.execute(
            select(
                PaperIdentifier.paper_id,
                PaperIdentifier.scheme,
                PaperIdentifier.normalized_value,
                PaperIdentifier.value,
            ).where(PaperIdentifier.scheme.in_(schemes))
        ).all()

        ids_by_paper_scheme: dict[str, dict[str, list[tuple[str, str]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for paper_id, scheme, normalized, raw in id_rows:
            pid = str(paper_id)
            scheme = (scheme or "").strip().lower()
            if pid in live_ids:
                report.identifiers_total[scheme] = (
                    report.identifiers_total.get(scheme, 0) + 1
                )
            if not (normalized or "").strip():
                report.add_drift(
                    MirrorDrift(
                        kind="missing_normalized_value",
                        scheme=scheme,
                        paper_id=pid,
                        identifier_values=[str(raw)],
                        detail="identifier row has no normalized_value",
                    ),
                    limit,
                )
            ids_by_paper_scheme[pid][scheme].append(
                (str(raw or ""), str(normalized or ""))
            )
            if pid not in live_ids:
                report.add_drift(
                    MirrorDrift(
                        kind="orphan_identifier",
                        scheme=scheme,
                        paper_id=pid,
                        identifier_values=[str(normalized or raw)],
                        detail="identifier points at a missing or soft-deleted paper",
                    ),
                    limit,
                )

        # 3) per live paper, compare mirror vs identifier(s).
        for paper_id, mirrors in mirror_by_paper.items():
            for _, scheme in MIRRORED_SCHEMES:
                mirror_value = mirrors.get(scheme)
                rows = ids_by_paper_scheme.get(paper_id, {}).get(scheme, [])
                mirror_set = (mirror_value or "").strip()
                identifiers = [normalized for _, normalized in rows if normalized]
                if len(rows) > 1:
                    report.add_drift(
                        MirrorDrift(
                            kind="duplicate_identifier",
                            scheme=scheme,
                            paper_id=paper_id,
                            mirror_value=mirror_value,
                            identifier_values=identifiers,
                            detail=f"{len(rows)} {scheme} identifier rows on one paper",
                        ),
                        limit,
                    )
                if mirror_set and not rows:
                    report.add_drift(
                        MirrorDrift(
                            kind="mirror_without_identifier",
                            scheme=scheme,
                            paper_id=paper_id,
                            mirror_value=mirror_value,
                            detail="mirror column set but no identifier row",
                        ),
                        limit,
                    )
                elif not mirror_set and rows:
                    report.add_drift(
                        MirrorDrift(
                            kind="identifier_without_mirror",
                            scheme=scheme,
                            paper_id=paper_id,
                            mirror_value=mirror_value,
                            identifier_values=identifiers,
                            detail="identifier row present but mirror column empty",
                        ),
                        limit,
                    )
                elif mirror_set and identifiers and mirror_set not in identifiers:
                    report.add_drift(
                        MirrorDrift(
                            kind="value_mismatch",
                            scheme=scheme,
                            paper_id=paper_id,
                            mirror_value=mirror_value,
                            identifier_values=identifiers,
                            detail="mirror value differs from the identifier's normalized value",
                        ),
                        limit,
                    )
    except Exception as exc:  # noqa: BLE001 - a read-only audit reports, never raises
        report.errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        session.close()

    return report


__all__ = [
    "DRIFT_KINDS",
    "MIRRORED_SCHEMES",
    "IdentifierMirrorReport",
    "MirrorDrift",
    "audit_identifier_mirrors",
]
