"""批量重建索引：检测"为什么要重建"，选出要重建的论文，然后按论文入队。

**为什么需要一个服务层**：同一个动作现在有两个入口 —— 运维用的 ``scripts/reindex.py``
（同步在本进程跑，能 ``--dry-run``、能被 Ctrl-C）和新加的批量端点
``POST /api/papers/reindex``（只能入队后立即返回）。两边如果各写一套"选哪些论文"，
迟早分叉；所以选择与检测都在这里，脚本与端点只负责"怎么执行"。

**检测什么**（每种情况都是"前提变了，旧产物不能再用"）：

===========================  ====================================================
``embedding_model_changed``  索引里的 chunk 是用**另一个** embedding 模型嵌的（换了模型）
``parser_backend_changed``   论文的解析戳与当前 ``PARSER_BACKEND`` 不一致（换了解析器）
``open_degradations``         上次流水线让步了（docling 不可达、语义分块降级…）且未解决
``missing_chunks``            还没有 chunk（首次回填）
===========================  ====================================================

扩展方式：给 :data:`DETECTORS` 加一个函数即可 —— 它返回 :class:`ReindexReason`（或 None），
接口、脚本、报告都自动带上它，不需要改任何调用方。这是刻意设计的，因为"需要整库重建"
的理由只会变多（新的分块策略、新的索引 mapping、新的文档字段……）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field as dataclass_field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Paper, PaperChunk
from app.services import degradation_service

logger = get_logger(__name__)

#: 检测到的原因码（也是 ``POST /api/papers/reindex`` 的 ``reasons`` 取值）。
REASON_EMBEDDING_DRIFT = "embedding_model_changed"
REASON_PARSER_DRIFT = "parser_backend_changed"
REASON_OPEN_DEGRADATIONS = "open_degradations"
REASON_MISSING_CHUNKS = "missing_chunks"

#: 一次请求最多入队多少篇（防手滑把队列塞爆；脚本不受此限）。
MAX_QUEUED_PER_REQUEST = 500


@dataclass(frozen=True)
class ReindexReason:
    """一条"为什么要重建"的证据。

    ``scope`` 是 ``all`` 时表示"前提变了，所以每一篇都要重建"（换模型就是这种）；
    ``papers`` 是命中的论文数，``detail`` 给人看，界面直接显示。
    """

    code: str
    detail: str
    papers: int
    scope: str = "subset"

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "detail": self.detail,
            "papers": self.papers,
            "scope": self.scope,
        }


@dataclass
class ReindexPlan:
    """选中的论文 + 为什么选它们的完整交代。"""

    papers: list[Paper] = dataclass_field(default_factory=list)
    reasons: list[ReindexReason] = dataclass_field(default_factory=list)
    #: 未选中但值得说明的检测结果（例如"检测到 X，但调用方只要求 Y"）。
    skipped_reasons: list[ReindexReason] = dataclass_field(default_factory=list)
    embedding: dict[str, object] = dataclass_field(default_factory=dict)
    parser: dict[str, object] = dataclass_field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "selected": len(self.papers),
            "reasons": [reason.as_dict() for reason in self.reasons],
            "skipped_reasons": [reason.as_dict() for reason in self.skipped_reasons],
            "embedding": self.embedding,
            "parser": self.parser,
        }


# --------------------------------------------------------------------------- #
# 检测
# --------------------------------------------------------------------------- #
def _live_papers(session: Session) -> list[Paper]:
    return list(
        session.execute(
            select(Paper).where(Paper.deleted_at.is_(None)).order_by(Paper.created_at)
        ).scalars()
    )


def embedding_census(session: Session) -> dict[str, object]:
    """当前索引里的 embedding 模型 vs 配置的模型。

    读 ``paper_chunks.embedding_model``（每个 chunk 都带戳）。返回 ``seen`` 是各模型的
    chunk 数，``configured`` 是 ``EMBEDDING_MODEL``，``drifted`` 说明两者不一致或存在
    ``NULL`` 戳（``NULL`` = 这一列存在之前的产物，同样需要重嵌）。
    """
    configured = settings.embedding_model
    rows = session.execute(
        select(PaperChunk.embedding_model, func.count(PaperChunk.id)).group_by(
            PaperChunk.embedding_model
        )
    ).all()
    seen = {(model or ""): int(count) for model, count in rows}
    models = {model for model in seen if model}
    drifted = bool(models - {configured}) or any(model == "" for model in seen)
    return {
        "configured": configured,
        "seen": dict(sorted(seen.items())),
        "drifted": drifted,
    }


def parser_census(session: Session) -> dict[str, object]:
    """论文的解析戳分布 vs 当前 ``PARSER_BACKEND``。"""
    configured = settings.parser_backend
    rows = session.execute(
        select(Paper.parser_backend, func.count(Paper.id))
        .where(Paper.deleted_at.is_(None))
        .group_by(Paper.parser_backend)
    ).all()
    stamps = {(stamp or "unknown"): int(count) for stamp, count in rows}
    stale = sum(count for stamp, count in stamps.items() if stamp != configured)
    return {
        "configured": configured,
        "stamps": dict(sorted(stamps.items())),
        "stale": stale,
    }


def detect_embedding_drift(session: Session) -> ReindexReason | None:
    census = embedding_census(session)
    if not census["drifted"]:
        return None
    seen = ", ".join(f"{model or '(无戳)'} x{count}" for model, count in census["seen"].items())
    return ReindexReason(
        code=REASON_EMBEDDING_DRIFT,
        detail=(
            f"索引里的 chunk 是用别的模型嵌的（现在配的是 {census['configured']}；"
            f"实际见到 {seen}）—— 换模型必须整库重嵌，旧向量与新查询不可比"
        ),
        papers=len(_live_papers(session)),
        scope="all",
    )


def detect_parser_drift(session: Session) -> ReindexReason | None:
    census = parser_census(session)
    if not census["stale"]:
        return None
    stamps = ", ".join(f"{stamp} x{count}" for stamp, count in census["stamps"].items())
    return ReindexReason(
        code=REASON_PARSER_DRIFT,
        detail=(
            f"有论文的解析戳与当前解析器不一致（现在配的是 {census['configured']}；"
            f"实际分布 {stamps}）—— 换了解析器要重新解析才能吃到新产物"
        ),
        papers=int(census["stale"]),
        scope="subset",
    )


def detect_open_degradations(session: Session) -> ReindexReason | None:
    paper_ids = degradation_service.paper_ids_with_open_degradations(session)
    if not paper_ids:
        return None
    return ReindexReason(
        code=REASON_OPEN_DEGRADATIONS,
        detail="上次流水线让步过（docling 不可达、语义分块降级…）且尚未解决",
        papers=len(paper_ids),
        scope="subset",
    )


def detect_missing_chunks(session: Session) -> ReindexReason | None:
    ids = papers_without_chunks(session)
    if not ids:
        return None
    return ReindexReason(
        code=REASON_MISSING_CHUNKS,
        detail="这些论文还没有 chunk（首次回填或上次失败）",
        papers=len(ids),
        scope="subset",
    )


#: 检测器注册表 —— 加一条新理由只要往这里加函数。
DETECTORS: tuple[Callable[[Session], ReindexReason | None], ...] = (
    detect_embedding_drift,
    detect_parser_drift,
    detect_open_degradations,
    detect_missing_chunks,
)

def papers_with_stamp(session: Session, stamp: str) -> set[str]:
    """论文 id：解析戳**等于** ``stamp``（``unknown`` = 那一列还是空）。

    注意方向：脚本的 ``--parser-backend pypdf`` 是"只挑 pypdf 解析的那些"
    （手册里的用法：``--parser-backend pypdf`` 之后 ``--parser-backend unknown``，
    把"不是 docling 产出"的全重新解析一遍）。**检测器要的是补集**，由
    :func:`papers_without_stamp` 提供，别把两者搞混（我第一版就搞反了，测试当场抓住）。
    """
    if stamp == "unknown":
        return {paper.id for paper in _live_papers(session) if not paper.parser_backend}
    return {
        paper.id for paper in _live_papers(session) if (paper.parser_backend or "") == stamp
    }


def papers_without_stamp(session: Session, stamp: str | None = None) -> set[str]:
    """论文 id：解析戳与 ``stamp``（默认当前 ``PARSER_BACKEND``）**不一致**。

    这是"换了解析器之后需要重新解析"的那一批 —— 包括从未打过戳的行。
    """
    wanted = (stamp or settings.parser_backend) or ""
    return {
        paper.id
        for paper in _live_papers(session)
        if (paper.parser_backend or "unknown") != wanted
    }


def papers_without_chunks(session: Session) -> set[str]:
    """论文 id：还没有任何 chunk（首次回填、或上次跑到一半炸了）。"""
    have = set(
        session.execute(select(PaperChunk.paper_id).group_by(PaperChunk.paper_id)).scalars()
    )
    return {paper.id for paper in _live_papers(session) if paper.id not in have}


#: 原因码 → 该码命中的论文选择器（``all`` 语义的码由 :func:`select_papers` 单独处理）。
_SUBSET_SELECTORS: dict[str, Callable[[Session], set[str]]] = {
    REASON_PARSER_DRIFT: papers_without_stamp,
    REASON_OPEN_DEGRADATIONS: lambda session: set(
        degradation_service.paper_ids_with_open_degradations(session)
    ),
    REASON_MISSING_CHUNKS: papers_without_chunks,
}


def detect_all(session: Session) -> list[ReindexReason]:
    """跑完所有检测器，返回命中的理由（顺序即注册表顺序）。"""
    reasons: list[ReindexReason] = []
    for detector in DETECTORS:
        reason = detector(session)
        if reason is not None:
            reasons.append(reason)
    return reasons


# --------------------------------------------------------------------------- #
# 选择
# --------------------------------------------------------------------------- #
def select_papers(
    session: Session,
    *,
    paper_ids: Sequence[str] | None = None,
    reasons: Iterable[str] | None = None,
    include_all: bool = False,
) -> ReindexPlan:
    """决定这次重建哪些论文，并交代理由。

    名字不叫 ``select``：那会顶掉模块里的 ``sqlalchemy.select``，整个文件的其他查询都会炸
    （真实踩过）。``paper_ids``（人来指名的）> ``include_all``（全库）> ``reasons``（只按指定理由）
    > 默认（所有检测到的理由的并集）。没有检测到任何理由且没显式指定时，选中 0 篇 ——
    "什么都不用做"是一个正常答案，不该顺手把整库重跑一遍。
    """
    plan = ReindexPlan(embedding=embedding_census(session), parser=parser_census(session))
    detected = detect_all(session)
    by_code = {reason.code: reason for reason in detected}

    if paper_ids:
        wanted = list(dict.fromkeys(paper_ids))
        papers = [
            paper
            for paper in _live_papers(session)
            if paper.id in set(wanted)
        ]
        missing = [paper_id for paper_id in wanted if paper_id not in {p.id for p in papers}]
        plan.papers = papers
        plan.reasons = [
            ReindexReason(
                code="explicit",
                detail=f"调用方指名了 {len(wanted)} 篇论文",
                papers=len(papers),
                scope="subset",
            )
        ]
        if missing:
            plan.skipped_reasons.append(
                ReindexReason(
                    code="unknown_paper",
                    detail=f"{len(missing)} 个 id 不存在或已删除",
                    papers=len(missing),
                    scope="subset",
                )
            )
        plan.skipped_reasons.extend(detected)
        return plan

    if include_all:
        plan.papers = _live_papers(session)
        plan.reasons = [
            ReindexReason(
                code="all",
                detail="调用方要求全库重建（不看检测结果）",
                papers=len(plan.papers),
                scope="all",
            )
        ]
        plan.skipped_reasons.extend(detected)
        return plan

    chosen = list(reasons) if reasons is not None else [reason.code for reason in detected]
    unknown = [code for code in chosen if code not in by_code]
    if unknown:
        raise ValueError(f"unknown reason(s): {', '.join(sorted(unknown))}")

    if REASON_EMBEDDING_DRIFT in chosen:
        plan.papers = _live_papers(session)
        plan.reasons = [by_code[REASON_EMBEDDING_DRIFT]]
    else:
        wanted_ids: set[str] = set()
        for code in chosen:
            selector = _SUBSET_SELECTORS.get(code)
            if selector is not None:
                wanted_ids |= selector(session)
        plan.papers = [paper for paper in _live_papers(session) if paper.id in wanted_ids]
        plan.reasons = [by_code[code] for code in chosen]

    plan.skipped_reasons.extend(
        reason for reason in detected if reason.code not in chosen
    )
    return plan


# --------------------------------------------------------------------------- #
# 入队（API 走这条；脚本直接同步跑）
# --------------------------------------------------------------------------- #
def queue(
    session: Session, papers: Sequence[Paper], *, limit: int = MAX_QUEUED_PER_REQUEST
) -> tuple[list[str], list[dict[str, str]]]:
    """给每篇论文排一个重建作业，返回 ``(job_ids, skipped)``。

    跳过的情况：没有原始文件（没字节就没法重新解析）—— 逐条记原因，不静默丢。
    ``limit`` 是防手滑的上限；被截断的部分也算跳过并说明，调用方能看到自己少排了多少。
    """
    from app.services import ingestion_service as ingest
    from app.services import paper_service

    job_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    for index, paper in enumerate(papers):
        if index >= limit:
            skipped.append({"paper_id": paper.id, "reason": f"超出单次上限 {limit} 篇，未入队"})
            continue
        record = paper_service.original_file(paper)
        if record is None:
            skipped.append({"paper_id": paper.id, "reason": "没有原始文件，无法重新解析"})
            continue
        job = ingest.create_reindex_job(session, paper, record)
        job_ids.append(job.id)
    logger.info(
        "bulk reindex queued",
        extra={"extra_fields": {"queued": len(job_ids), "skipped": len(skipped)}},
    )
    return job_ids, skipped
