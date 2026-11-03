"""Preview and resolve source attribution while preserving raw input and history."""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import MetadataSourceReviewEvent, Paper, PaperSource
from app.services import metadata_identifiers as ids, metadata_import as importer
from app.services import metadata_matcher as matcher, metadata_merge as merge
from app.services import metadata_shell, metadata_sources as sources, paper_service


class ReviewError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _load(session: Session, source_id: str, *, lock: bool = False) -> PaperSource:
    source = session.scalar(select(PaperSource).where(PaperSource.id == source_id).with_for_update()) if lock else session.get(PaperSource, source_id)
    if source is None:
        raise ReviewError("来源记录不存在", 404)
    if lock:
        session.refresh(source)
    return source


def _parsed(source: PaperSource):
    try:
        parsed = importer.parse_records([source.raw])[0]
        if not parsed.values:
            raise ValueError("empty metadata")
        parsed.match_input().normalized_identifiers()
        return parsed
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise ReviewError("来源记录无法解析，请核对原始数据", 422) from exc


def _revision(session: Session, source: PaperSource) -> str:
    events = list(session.scalars(select(MetadataSourceReviewEvent.id).where(
        MetadataSourceReviewEvent.source_id == source.id).order_by(MetadataSourceReviewEvent.id)))
    state = [sources.serialize_source(source), source.raw, events]
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def _state(source: PaperSource) -> dict:
    return {key: getattr(source, key) for key in ("paper_id", "match_status", "match_method", "match_confidence")}


def _event(session: Session, source: PaperSource, action: str, actor: str, reason: str, before: dict, **details):
    session.add(MetadataSourceReviewEvent(source_id=source.id, action=action, actor=actor,
        reason=reason, payload={"before": before, "after": _state(source), **details}, created_at=datetime.now(timezone.utc)))
    session.flush()


def _pending(session: Session, source_id: str, revision: str | None) -> PaperSource:
    source = _load(session, source_id, lock=True)
    if not revision or revision != _revision(session, source):
        raise ReviewError("来源状态已变化，请刷新详情后重试")
    if source.paper_id or source.match_status not in sources.REVIEW_STATUSES:
        raise ReviewError("该来源已处理，请刷新列表")
    return source


def _paper(session: Session, paper_id: str) -> dict:
    paper = paper_service.get_paper(session, paper_id)
    if paper is None:
        return {"paper_id": paper_id}
    file = paper_service.original_file(paper)
    return {"paper_id": paper.id, "title": paper.title, "authors": paper_service.paper_author_names(paper),
            "year": paper.year, "doi": paper.doi, "arxiv_id": paper.arxiv_id, "status": paper.status,
            "filename": file.filename if file else None}


def detail(session: Session, source_id: str) -> dict:
    source = _load(session, source_id)
    error, values, match = None, {}, None
    try:
        parsed = _parsed(source)
        values = parsed.values
        result = matcher.match_record(session, parsed.match_input())
        match = result.as_dict()
        match["candidates"] = [{**_paper(session, row["paper_id"]), **row} for row in result.candidates]
        if result.paper_id and not any(row["paper_id"] == result.paper_id for row in match["candidates"]):
            match["candidates"].append(_paper(session, result.paper_id))
    except ReviewError as exc:
        error = str(exc)
    history = session.scalars(select(MetadataSourceReviewEvent).where(
        MetadataSourceReviewEvent.source_id == source.id).order_by(
            MetadataSourceReviewEvent.created_at.desc(), MetadataSourceReviewEvent.id).limit(50)).all()
    return {"source": sources.serialize_source(source), "revision": _revision(session, source),
            "values": values, "match": match, "parse_error": error,
            "history": [{"id": row.id, "action": row.action, "actor": row.actor, "reason": row.reason,
                         "created_at": row.created_at, **row.payload} for row in history]}


def _check_identifiers(session: Session, parsed, target_id: str | None = None):
    for scheme, value in parsed.match_input().normalized_identifiers().items():
        row = ids.find_identifier(session, scheme, value)
        if row is not None and row.paper_id != target_id:
            raise ReviewError(f"标识符 {scheme}:{value} 已属于另一篇论文，请先核对归属")


def _merge(session: Session, source: PaperSource, paper: Paper, parsed) -> dict:
    report = merge.merge_values(session, paper, parsed.values, source_type=source.source_type,
                                source_id=source.id, confidence=source.match_confidence or 1.0)
    ids.refresh_primary(session, paper.id)
    ids.mirror_legacy_columns(session, paper)
    ids.upgrade_fingerprint(session, paper, sha256=parsed.sha256)
    return {"merged_fields": [row.field for row in report.applied], "conflicts": merge.conflict_report(report)}


def attach(session: Session, source_id: str, paper_id: str, *, actor: str,
           revision: str | None = None) -> dict:
    # The legacy attach contract has no revision; it still receives identity
    # guards and row locking. New review clients always submit a revision.
    source = _pending(session, source_id, revision) if revision else _load(session, source_id, lock=True)
    if source.paper_id or source.match_status not in sources.REVIEW_STATUSES:
        raise ReviewError("该来源已处理，请刷新列表")
    paper = paper_service.get_paper(session, paper_id)
    if paper is None:
        raise ReviewError("目标论文不存在", 404)
    parsed = _parsed(source)
    _check_identifiers(session, parsed, paper.id)
    conflicts = matcher.association_conflicts(session, paper, parsed.match_input(), manual=True)
    if conflicts:
        raise ReviewError("目标存在身份冲突：" + "; ".join(conflicts))
    before = _state(source)
    sources.attach_source(session, source, paper, method=matcher.METHOD_MANUAL)
    merged = _merge(session, source, paper, parsed)
    _event(session, source, "attach", actor, "人工确认关联论文", before, **merged)
    return {**sources.serialize_source(source), **merged}


def rematch(session: Session, source_id: str, *, actor: str, apply: bool = False,
            revision: str | None = None, expected_paper_id: str | None = None) -> dict:
    if not apply:
        return {**detail(session, source_id), "applied": False}
    source = _pending(session, source_id, revision)
    parsed = _parsed(source)
    result = matcher.match_record(session, parsed.match_input())
    if result.paper_id != expected_paper_id:
        raise ReviewError("匹配结果已变化，请重新预览")
    before, merged = _state(source), {"merged_fields": [], "conflicts": []}
    if result.matched:
        _check_identifiers(session, parsed, result.paper_id)
        sources.attach_source(session, source, result.paper, method=result.method, confidence=result.confidence)
        merged = _merge(session, source, result.paper, parsed)
    else:
        source.match_status = sources.MATCH_STATUS_AMBIGUOUS if result.status == matcher.STATUS_AMBIGUOUS else sources.MATCH_STATUS_PENDING
        source.match_method, source.match_confidence = result.method, result.confidence
    _event(session, source, "rematch", actor, result.reason, before, match=result.as_dict(), **merged)
    return {**detail(session, source_id), "applied": result.matched, **merged}


def create_paper(session: Session, source_id: str, *, actor: str, revision: str) -> dict:
    source = _pending(session, source_id, revision)
    parsed = _parsed(source)
    if not str(parsed.values.get("title") or "").strip():
        raise ReviewError("缺少论文标题，不能独立入库", 422)
    _check_identifiers(session, parsed)
    result = matcher.match_record(session, parsed.match_input())
    if result.matched and result.method != matcher.METHOD_TITLE_YEAR_AUTHOR:
        raise ReviewError("已有相同标识符或文件对应的论文，请关联现有论文")
    before = _state(source)
    try:
        with session.begin_nested():
            paper, _ = metadata_shell.create_shell(session, parsed.values, source_type=source.source_type,
                source_ref=source.source_ref, raw=source.raw, content_type=source.content_type, importer=source.importer)
    except IntegrityError as exc:
        raise ReviewError("论文或标识符已存在，请刷新候选论文") from exc
    source.match_method, source.match_confidence = "shell", 1.0
    _event(session, source, "create", actor, "人工确认为独立论文", before, paper_id=paper.id)
    return {**detail(session, source_id), "applied": True, "created_paper_id": paper.id}


def reject(session: Session, source_id: str, *, actor: str, revision: str) -> dict:
    source = _pending(session, source_id, revision)
    before = _state(source)
    source.match_status = sources.MATCH_STATUS_REJECTED
    _event(session, source, "reject", actor, "人工拒绝来源", before)
    return {**detail(session, source_id), "applied": True}


def restore(session: Session, source_id: str, *, actor: str, revision: str) -> dict:
    source = _load(session, source_id, lock=True)
    if revision != _revision(session, source) or source.paper_id or source.match_status != sources.MATCH_STATUS_REJECTED:
        raise ReviewError("来源状态已变化，请刷新详情后重试")
    before = _state(source)
    source.match_status = sources.MATCH_STATUS_PENDING
    _event(session, source, "restore", actor, "恢复待复核", before)
    return {**detail(session, source_id), "applied": True}


def rematch_batch(session: Session, entries: list[dict], *, actor: str, apply: bool) -> dict:
    items = []
    for entry in entries:
        try:
            with session.begin_nested():
                result = rematch(session, entry["source_id"], actor=actor, apply=apply,
                    revision=entry.get("revision"), expected_paper_id=entry.get("expected_paper_id"))
            items.append({"source_id": entry["source_id"], **result})
        except ReviewError as exc:
            items.append({"source_id": entry["source_id"], "error": str(exc), "status": exc.status})
        except IntegrityError:
            items.append({"source_id": entry["source_id"], "error": "论文或标识符已变化，请刷新候选", "status": 409})
    return {"items": items, "applied": sum(bool(row.get("applied")) for row in items)}
