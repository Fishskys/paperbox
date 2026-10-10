"""Manual metadata edits and rollback (decision 12, section 9 of the design).

``manual`` is the only source type rule R2 does not constrain: a human decision
beats whatever a platform or a heuristic said. It is still written as a claim, so
it can be inspected ("who changed this, when") and rolled back like anything else.

Every edit goes through the same writers as the import path
(:mod:`app.services.provenance_service`), which keeps the columns, the identifiers
and the provenance ledger from drifting apart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Paper, PaperFieldProvenance, PaperSource
from app.services import metadata_identifiers as identifiers
from app.services import metadata_sources as sources
from app.services import metadata_tags as tags
from app.services import provenance_service as prov

logger = get_logger(__name__)

#: ``PATCH`` keys -> provenance fields. Everything else in the payload is rejected
#: (silently ignoring a typo would look like a successful edit).
SIMPLE_PATCH_FIELDS: dict[str, str] = {
    "title": prov.FIELD_TITLE,
    "abstract": prov.FIELD_ABSTRACT,
    "language": prov.FIELD_LANGUAGE,
    "year": prov.FIELD_YEAR,
    "volume": prov.FIELD_VOLUME,
    "issue": prov.FIELD_ISSUE,
    "pages": prov.FIELD_PAGES,
    "paper_type": prov.FIELD_PAPER_TYPE,
    "publication_date": prov.FIELD_PUBLICATION_DATE,
    "url": prov.FIELD_URL,
}

IDENTIFIER_PATCH_FIELDS: dict[str, str] = {
    "doi": identifiers.SCHEME_DOI,
    "arxiv_id": identifiers.SCHEME_ARXIV,
}

#: Patch keys that need special handling (venue, authors, tags).
SPECIAL_PATCH_FIELDS: tuple[str, ...] = ("venue", "venue_year", "authors", "tags")

PATCH_FIELDS: tuple[str, ...] = tuple(SIMPLE_PATCH_FIELDS) + tuple(IDENTIFIER_PATCH_FIELDS) + SPECIAL_PATCH_FIELDS


@dataclass
class PatchResult:
    """What a manual edit changed."""

    paper_id: str
    fields: list[str] = dataclass_field(default_factory=list)
    fingerprint: str | None = None
    rejected: list[str] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "fields": self.fields,
            "fingerprint": self.fingerprint,
            "rejected": self.rejected,
        }


#: Patch keys whose "current value" comes from somewhere other than ``read_field``.
def current_value(session: Session, paper: Paper, key: str) -> Any:
    """Current value of one patch key, as the claim value shape.

    Used by the ``dry_run`` preview and by the before/after diff of a manual edit
    (contract section 5.2: ``paper_update_metadata`` reports "old -> new"). Unknown
    keys return ``None`` rather than raising: they are reported as ``rejected`` by
    :func:`patch_metadata`, and a preview is not the place to fail.
    """
    if key == "tags":
        try:
            return tags.tags_for_paper(session, paper)
        except Exception:  # pragma: no cover - defensive: tags are cosmetic
            return None
    if key == "venue_year":
        return getattr(paper, "venue_year", None)
    field = SIMPLE_PATCH_FIELDS.get(key) or IDENTIFIER_PATCH_FIELDS.get(key)
    if key == "venue":
        field = prov.FIELD_VENUE
    elif key == "authors":
        field = prov.FIELD_AUTHORS
    if field is None:
        return None
    try:
        return prov.read_field(paper, field)
    except Exception:  # pragma: no cover - defensive
        return None


def preview_patch(
    session: Session, paper: Paper, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """What a patch would look like right now: ``{key: current value}``.

    Read-only, so a ``dry_run`` can show the caller exactly which fields change
    without a claim row, a fingerprint or a single database write.
    """
    return {
        key: current_value(session, paper, key)
        for key in payload
        if key in PATCH_FIELDS
    }


def manual_source(session: Session, paper: Paper) -> PaperSource:
    """The paper's single ``manual`` source row (created on first edit)."""
    source = sources.find_source(
        session, sources.SOURCE_TYPE_MANUAL, sources.manual_ref(paper.id)
    )
    if source is not None:
        return source
    return sources.upsert_source(
        session,
        source_type=sources.SOURCE_TYPE_MANUAL,
        source_ref=sources.manual_ref(paper.id),
        raw={},
        paper_id=paper.id,
        match_status=sources.MATCH_STATUS_MATCHED,
        match_method="manual",
        match_confidence=1.0,
        importer="api",
    )


def _venue_claim(value: Any, *, venue_year: Any = None) -> dict[str, Any] | None:
    """Accept ``"ISSCC"``, ``{"name": …, "year": …}`` or ``None`` (clearing)."""
    if value is None:
        return None
    if isinstance(value, str):
        claim: dict[str, Any] = {"name": value}
    elif isinstance(value, Mapping):
        claim = dict(value)
    else:
        return None
    if venue_year is not None and "year" not in claim:
        claim["year"] = venue_year
    if not str(claim.get("name") or "").strip():
        return None
    return claim


def patch_metadata(
    session: Session, paper: Paper, payload: Mapping[str, Any]
) -> PatchResult:
    """Apply a manual edit; every changed field becomes a ``manual`` claim.

    Unknown keys are reported in ``rejected`` instead of being dropped silently.
    An identifier change replaces the old identifier (a human correcting a DOI
    means the previous value was wrong) and re-derives the fingerprint.
    """
    source = manual_source(session, paper)
    result = PatchResult(paper_id=paper.id)
    changed_identifier = False

    for key, value in payload.items():
        if key in ("venue_year",):
            continue
        if key not in PATCH_FIELDS:
            result.rejected.append(key)
            continue

        if key in SIMPLE_PATCH_FIELDS:
            if value is None:
                continue
            prov.set_field(
                session,
                paper,
                SIMPLE_PATCH_FIELDS[key],
                value,
                source_id=source.id,
                confidence=1.0,
                decided_by=prov.DECIDED_MANUAL,
                override=True,
                # 人工编辑就是一次裁决：盖 decision_at（只写 decided_by 的话，
                # "什么时候改的"只能去翻访问日志）
                human=True,
            )
            result.fields.append(key)
            continue

        if key in IDENTIFIER_PATCH_FIELDS:
            if value is None or not str(value).strip():
                continue
            scheme = IDENTIFIER_PATCH_FIELDS[key]
            prov.set_field(
                session,
                paper,
                prov.identifier_field(scheme),
                str(value),
                source_id=source.id,
                confidence=1.0,
                decided_by=prov.DECIDED_MANUAL,
                override=True,
                # 人工编辑就是一次裁决：盖 decision_at（只写 decided_by 的话，
                # "什么时候改的"只能去翻访问日志）
                human=True,
            )
            identifiers.replace_identifier(
                session,
                paper_id=paper.id,
                scheme=scheme,
                value=str(value),
                first_source_id=source.id,
            )
            identifiers.refresh_primary(session, paper.id)
            # A manual correction is authoritative: the mirror column must follow
            # the new identifier (fill-only would keep the superseded DOI).
            identifiers.mirror_legacy_columns(session, paper, force_scheme=scheme)
            changed_identifier = True
            result.fields.append(key)
            continue

        if key == "venue":
            claim = _venue_claim(value, venue_year=payload.get("venue_year"))
            if claim is None:
                continue
            prov.set_field(
                session,
                paper,
                prov.FIELD_VENUE,
                claim,
                source_id=source.id,
                confidence=1.0,
                decided_by=prov.DECIDED_MANUAL,
                override=True,
                # 人工编辑就是一次裁决：盖 decision_at（只写 decided_by 的话，
                # "什么时候改的"只能去翻访问日志）
                human=True,
            )
            result.fields.append(key)
            continue

        if key == "authors":
            names = _names(value)
            if not names:
                continue
            prov.set_field(
                session,
                paper,
                prov.FIELD_AUTHORS,
                names,
                source_id=source.id,
                confidence=1.0,
                decided_by=prov.DECIDED_MANUAL,
                override=True,
                # 人工编辑就是一次裁决：盖 decision_at（只写 decided_by 的话，
                # "什么时候改的"只能去翻访问日志）
                human=True,
            )
            result.fields.append(key)
            continue

        if key == "tags":
            names = _names(value)
            if not names:
                continue
            tags.replace_kind(session, paper, tags.KIND_SOURCE_TAG, names)
            prov.set_field(
                session,
                paper,
                prov.tag_field(tags.KIND_SOURCE_TAG),
                names,
                source_id=source.id,
                confidence=1.0,
                decided_by=prov.DECIDED_MANUAL,
                override=True,
                # 人工编辑就是一次裁决：盖 decision_at（只写 decided_by 的话，
                # "什么时候改的"只能去翻访问日志）
                human=True,
            )
            result.fields.append(key)
            continue

    if changed_identifier:
        _fingerprint, conflict = identifiers.upgrade_fingerprint(session, paper)
        result.fingerprint = paper.fingerprint
        if conflict:
            logger.warning(
                "manual identifier collides with another live paper",
                extra={
                    "extra_fields": {"paper_id": paper.id, "conflicting_paper_id": conflict}
                },
            )
    session.flush()
    logger.info(
        "manual metadata edit",
        extra={
            "extra_fields": {
                "paper_id": paper.id,
                "fields": result.fields,
                "rejected": result.rejected,
            }
        },
    )
    return result


def _names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(";") if part.strip()]
    if isinstance(value, Sequence):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


def rollback_metadata(
    session: Session, paper: Paper, field: str, provenance_id: str
) -> PaperFieldProvenance:
    """Roll one field back to a previous claim (section 9 of the design)."""
    row = prov.rollback_field(session, paper, field, provenance_id)
    if prov.scheme_of_identifier_field(field):
        identifiers.refresh_primary(session, paper.id)
        identifiers.mirror_legacy_columns(session, paper)
        identifiers.upgrade_fingerprint(session, paper)
    session.flush()
    return row


def dismiss_conflict(
    session: Session, paper: Paper, field: str, provenance_id: str
) -> prov.PaperFieldProvenance:
    """Human verdict on one field conflict: keep what is current, stop asking.

    The counterpart of :func:`rollback_metadata` (which is "the rejected value was
    right after all"). Both leave every claim in the ledger; they only change which
    one is current, or whether the disagreement is still open.
    """
    row = prov.dismiss_claim(
        session, paper_id=paper.id, field=field, provenance_id=provenance_id
    )
    session.flush()
    return row


def metadata_view(session: Session, paper: Paper) -> dict[str, Any]:
    """Current values plus, per field, who claimed what and when."""
    history = prov.provenance_summary(session, paper.id)
    return {
        "paper_id": paper.id,
        "status": paper.status,
        "fingerprint": paper.fingerprint,
        "values": _current_values(session, paper),
        "provenance": history,
        "sources": [sources.serialize_source(row) for row in sources.sources_for_paper(session, paper.id)],
        "identifiers": [
            {
                "scheme": row.scheme,
                "value": row.value,
                "normalized_value": row.normalized_value,
                "is_primary": bool(row.is_primary),
                "first_source_id": row.first_source_id,
            }
            for row in identifiers.identifiers_for_paper(session, paper.id)
        ],
        "tags": tags.tags_by_kind(session, paper),
    }


def _current_values(session: Session, paper: Paper) -> dict[str, Any]:
    """The claim-shaped current value of every field that has one."""
    values: dict[str, Any] = {}
    for field_name in (
        prov.FIELD_TITLE,
        prov.FIELD_ABSTRACT,
        prov.FIELD_LANGUAGE,
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
        if value not in (None, "", [], {}):
            values[field_name] = value
    return values


__all__ = [
    "IDENTIFIER_PATCH_FIELDS",
    "PATCH_FIELDS",
    "SIMPLE_PATCH_FIELDS",
    "SPECIAL_PATCH_FIELDS",
    "PatchResult",
    "manual_source",
    "metadata_view",
    "patch_metadata",
    "rollback_metadata",
]