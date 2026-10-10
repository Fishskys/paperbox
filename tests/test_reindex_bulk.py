"""批量重建索引：自动检测"为什么要重建" + 选择 + 入队。

两种真实场景驱动它（主人 2026-10-10 指明）：**换了 embedding 模型**（旧向量与新查询不可比）
与**从 pypdf 换成 docling**（不重新解析就吃不到新产物）。检测不止这两种，所以做成了
``DETECTORS`` 注册表 —— 这些用例就是在锁"注册表怎么被用"和"选择优先级"。

选择逻辑与 ``scripts/reindex.py`` 共用，所以这里锁住的也是脚本的行为。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import require_api_key, require_write
from app.db.models import Paper, PaperChunk, PaperDegradation, PaperFile, new_uuid
from app.db.session import get_db
from app.main import app
from app.services import reindex_service as reindex


def make_paper(session, **overrides) -> Paper:
    values = {
        "id": new_uuid(),
        "title": "A paper",
        "fingerprint": f"sha256:{new_uuid()}",
        "status": "INDEXED",
        # 解析戳默认就是"当前配置"那一个：想测解析器漂移的用例自己显式传别的值
        # （否则每篇论文都会因"无戳 = unknown"命中 parser drift）。
        "parser_backend": settings.parser_backend,
    }
    values.update(overrides)
    paper = Paper(**values)
    session.add(paper)
    session.flush()
    return paper


def add_chunks(session, paper, count: int = 2, model: str | None = None) -> None:
    for index in range(count):
        session.add(
            PaperChunk(
                id=new_uuid(),
                paper_id=paper.id,
                chunk_index=index,
                page_start=1,
                page_end=1,
                section="body",
                text=f"chunk {index}",
                token_count=3,
                char_count=7,
                embedding_model=model,
                embedding_dimension=1024 if model else None,
            )
        )
    session.flush()


def add_file(session, paper, kind: str = "original") -> None:
    session.add(
        PaperFile(
            id=new_uuid(),
            paper_id=paper.id,
            kind=kind,
            bucket=settings.minio_bucket,
            filename="paper.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            object_key=f"papers/{paper.id}/original.pdf",
            is_primary=True,
        )
    )
    session.flush()


def add_degradation(session, paper, stage: str = "chunking", code: str = "semantic_fallback") -> None:
    session.add(
        PaperDegradation(
            id=new_uuid(),
            paper_id=paper.id,
            stage=stage,
            code=code,
            detail={},
            occurrences=1,
        )
    )
    session.flush()


@pytest.fixture()
def client(session_factory):
    def _db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_api_key] = lambda: "test-key"
    app.dependency_overrides[require_write] = lambda: "test-key"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 检测
# --------------------------------------------------------------------------- #
def test_a_model_change_means_the_whole_library(db_session) -> None:
    """换模型是"前提变了"，不是"某几篇脏了" —— 旧向量与新查询不可比，全库都要重嵌。"""
    old = make_paper(db_session, title="Indexed with the old model")
    add_chunks(db_session, old, model="some/old-model")
    fresh = make_paper(db_session, title="No chunks yet")

    plan = reindex.select_papers(db_session)

    assert [reason.code for reason in plan.reasons] == [reindex.REASON_EMBEDDING_DRIFT]
    assert plan.reasons[0].scope == "all"
    assert {paper.id for paper in plan.papers} == {old.id, fresh.id}
    assert plan.embedding["drifted"] is True
    assert plan.embedding["configured"] == settings.embedding_model


def test_a_null_chunk_stamp_also_counts_as_drift(db_session) -> None:
    """``embedding_model`` 为 NULL 是那一列存在之前的产物，同样得重嵌。"""
    paper = make_paper(db_session)
    add_chunks(db_session, paper, model=None)

    assert reindex.detect_embedding_drift(db_session) is not None


def test_a_matching_model_means_nothing_to_do(db_session) -> None:
    """"什么都不用做"是一个正常答案：不该顺手把整库重跑一遍。"""
    paper = make_paper(db_session)
    add_chunks(db_session, paper, model=settings.embedding_model)

    plan = reindex.select_papers(db_session)

    assert plan.papers == []
    assert plan.reasons == []
    assert plan.skipped_reasons == []


def test_a_parser_switch_selects_only_the_stale_stamps(db_session) -> None:
    """从 pypdf 换到 docling：只挑戳还是旧解析器的那些，不是整库。"""
    stale = make_paper(db_session, title="Parsed by pypdf", parser_backend="pypdf")
    current = make_paper(db_session, title="Parsed by docling", parser_backend=settings.parser_backend)
    unstamped = make_paper(db_session, title="No stamp at all", parser_backend=None)
    for paper in (stale, current, unstamped):
        add_chunks(db_session, paper, model=settings.embedding_model)

    plan = reindex.select_papers(db_session)

    assert [reason.code for reason in plan.reasons] == [reindex.REASON_PARSER_DRIFT]
    assert plan.reasons[0].scope == "subset"
    assert {paper.id for paper in plan.papers} == {stale.id, unstamped.id}
    assert plan.parser["configured"] == settings.parser_backend


def test_open_degradations_are_selected_by_their_ledger_rows(db_session) -> None:
    paper = make_paper(db_session)
    add_chunks(db_session, paper, model=settings.embedding_model)
    add_degradation(db_session, paper)

    plan = reindex.select_papers(db_session)

    assert [reason.code for reason in plan.reasons] == [reindex.REASON_OPEN_DEGRADATIONS]
    assert [item.id for item in plan.papers] == [paper.id]


def test_a_resolved_degradation_is_not_a_reason(db_session) -> None:
    from datetime import datetime, timezone

    paper = make_paper(db_session)
    add_chunks(db_session, paper, model=settings.embedding_model)
    add_degradation(db_session, paper)
    row = db_session.query(PaperDegradation).one()
    row.resolved_at = datetime.now(timezone.utc)
    db_session.flush()

    assert reindex.detect_open_degradations(db_session) is None


def test_missing_chunks_are_a_reason(db_session) -> None:
    paper = make_paper(db_session)

    plan = reindex.select_papers(db_session)

    assert [reason.code for reason in plan.reasons] == [reindex.REASON_MISSING_CHUNKS]
    assert [item.id for item in plan.papers] == [paper.id]


def test_reasons_can_be_narrowed_to_one(db_session) -> None:
    """调用方可以只按某一个理由重建（其余理由会在报告里列为"未采纳"）。"""
    with_chunks = make_paper(db_session, title="Has chunks")
    add_chunks(db_session, with_chunks, model=settings.embedding_model)
    add_degradation(db_session, with_chunks)
    make_paper(db_session, title="No chunks")

    plan = reindex.select_papers(db_session, reasons=[reindex.REASON_OPEN_DEGRADATIONS])

    assert [item.id for item in plan.papers] == [with_chunks.id]
    assert [reason.code for reason in plan.reasons] == [reindex.REASON_OPEN_DEGRADATIONS]
    assert [reason.code for reason in plan.skipped_reasons] == [reindex.REASON_MISSING_CHUNKS]


def test_an_unknown_reason_is_rejected(db_session) -> None:
    make_paper(db_session)

    with pytest.raises(ValueError):
        reindex.select_papers(db_session, reasons=["because_i_said_so"])


def test_explicit_paper_ids_win_over_detection(db_session) -> None:
    first = make_paper(db_session, title="Wanted")
    second = make_paper(db_session, title="Not wanted")
    for paper in (first, second):
        add_chunks(db_session, paper, model=settings.embedding_model)
        add_degradation(db_session, paper)

    plan = reindex.select_papers(db_session, paper_ids=[first.id])

    assert [paper.id for paper in plan.papers] == [first.id]
    assert [reason.code for reason in plan.reasons] == ["explicit"]
    assert [reason.code for reason in plan.skipped_reasons] == [reindex.REASON_OPEN_DEGRADATIONS]


def test_a_deleted_paper_is_never_selected(db_session) -> None:
    from datetime import datetime, timezone

    gone = make_paper(db_session, deleted_at=datetime.now(timezone.utc))
    add_chunks(db_session, gone, model="some/old-model")

    plan = reindex.select_papers(db_session, include_all=True)

    assert plan.papers == []


def test_include_all_forces_the_whole_library(db_session) -> None:
    first = make_paper(db_session)
    second = make_paper(db_session)
    add_chunks(db_session, first, model=settings.embedding_model)

    plan = reindex.select_papers(db_session, include_all=True)

    assert {paper.id for paper in plan.papers} == {first.id, second.id}
    assert [reason.code for reason in plan.reasons] == ["all"]


# --------------------------------------------------------------------------- #
# 入队
# --------------------------------------------------------------------------- #
def test_queue_skips_papers_without_a_stored_file(db_session) -> None:
    """没有原始文件就没法重新解析：逐条记原因，不静默丢。"""
    with_file = make_paper(db_session, title="Has bytes")
    add_file(db_session, with_file)
    without = make_paper(db_session, title="No bytes")

    job_ids, skipped = reindex.queue(db_session, [with_file, without])

    assert len(job_ids) == 1
    assert skipped == [{"paper_id": without.id, "reason": "没有原始文件，无法重新解析"}]


def test_queue_honours_its_limit_and_says_so(db_session) -> None:
    papers = []
    for _ in range(3):
        paper = make_paper(db_session)
        add_file(db_session, paper)
        papers.append(paper)

    job_ids, skipped = reindex.queue(db_session, papers, limit=2)

    assert len(job_ids) == 2
    assert len(skipped) == 1
    assert "超出单次上限" in skipped[0]["reason"]


# --------------------------------------------------------------------------- #
# HTTP 契约
# --------------------------------------------------------------------------- #
def test_dry_run_is_the_default_over_http(client, session_factory) -> None:
    """试运行是默认：不带 dry_run 时只报告，不排作业。"""
    session = session_factory()
    try:
        paper = make_paper(session)
        add_file(session, paper)
        # 有 chunk 才不会另外命中 missing_chunks，本用例只想看降级那一条理由
        add_chunks(session, paper, model=settings.embedding_model)
        add_degradation(session, paper)
        session.commit()
    finally:
        session.close()

    response = client.post("/api/papers/reindex", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["selected"] == 1
    assert body["queued"] == 0 and body["job_ids"] == []
    assert [reason["code"] for reason in body["reasons"]] == [reindex.REASON_OPEN_DEGRADATIONS]
    assert "试运行" in body["note"]


def test_the_endpoint_queues_one_job_per_paper(client, session_factory, monkeypatch) -> None:
    # 测试里队列没起，``submit`` 会**内联执行**作业（然后因为没有真实字节而失败、把行改掉）。
    # 打桩成"只入队不执行"，才是在验证"端点排了几个作业"这件事本身。
    from app.workers import queue as job_queue

    monkeypatch.setattr(job_queue, "submit", lambda *args, **kwargs: None)

    session = session_factory()
    try:
        paper = make_paper(session)
        add_file(session, paper)
        add_degradation(session, paper)
        session.commit()
        paper_id = paper.id
    finally:
        session.close()

    response = client.post("/api/papers/reindex", json={"dry_run": False})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued"] == 1 and len(body["job_ids"]) == 1
    assert body["dry_run"] is False
    # 作业确实落到库里，并且指向那篇论文
    session = session_factory()
    try:
        from app.db.models import IngestionJob

        job = session.get(IngestionJob, body["job_ids"][0])
        assert job.paper_id == paper_id
        # "这是一次重建"写在 payload.source_type 里（create_job 的入参）；
        # job.kind 是行级分类，重建作业也是 ingest。
        assert (job.payload or {}).get("source_type") == "reindex"
    finally:
        session.close()


def test_the_endpoint_reports_an_unknown_reason_as_422(client, session_factory) -> None:
    session = session_factory()
    try:
        make_paper(session)
        session.commit()
    finally:
        session.close()

    response = client.post("/api/papers/reindex", json={"reasons": ["nope"]})

    assert response.status_code == 422
    assert "unknown reason" in response.json()["detail"]


def test_the_endpoint_says_so_when_nothing_needs_rebuilding(client, session_factory) -> None:
    session = session_factory()
    try:
        paper = make_paper(session)
        add_chunks(session, paper, model=settings.embedding_model)
        session.commit()
    finally:
        session.close()

    response = client.post("/api/papers/reindex", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["selected"] == 0
    assert "没有需要重建的论文" in body["note"]


def test_the_endpoint_rejects_unknown_body_keys(client) -> None:
    """extra="forbid"：写错字段名要报错，不能默默当成没写（默认为试运行会骗人）。"""
    response = client.post("/api/papers/reindex", json={"dry_run": False, "all": True})

    assert response.status_code == 422
