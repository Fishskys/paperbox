"""``decision_at``：人类裁决这一行的时刻（2026-10-10，迁移 ``a41f7c2d9b30``）。

为什么单独立一个文件：这条口径是被真机排查逼出来的 —— ``decided_at`` 名字像裁决时刻，实际只在
声明写入账本时赋值，裁决接口从不更新它，于是"这几条冲突什么时候被裁决的"只能去翻访问日志。
这里把三件事钉死：①三个裁决入口都盖时间戳；②机器合并**不**盖（导入一份人工来源也会写
``decided_by='manual'``，那是归属不是裁决）；③接口把这一列透出去，不然修了也没人看得见。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.security import require_api_key, require_write
from app.db.models import Paper, PaperFieldProvenance
from app.db.session import get_db
from app.main import app
from app.services import metadata_manual, metadata_merge, provenance_service
from app.services import metadata_sources as sources


def make_paper(session, **overrides) -> Paper:
    values = {
        "id": provenance_service.new_uuid(),
        "title": "Original title",
        "fingerprint": f"sha256:{provenance_service.new_uuid()}",
        "status": "INDEXED",
    }
    values.update(overrides)
    paper = Paper(**values)
    session.add(paper)
    session.flush()
    return paper


def seed_claim(session, paper, field: str, value, *, source_type="ieee_api"):
    """写一条来源声明的 claim（机器路径：没有 human 标记）。"""
    source = sources.upsert_source(
        session,
        source_type=source_type,
        source_ref=f"{source_type}:{paper.id}:{field}",
        raw={},
        paper_id=paper.id,
        match_status=sources.MATCH_STATUS_MATCHED,
    )
    return provenance_service.set_field(
        session, paper, field, value, source_id=source.id, override=True
    )


def claims_for(session, paper, field: str) -> list[PaperFieldProvenance]:
    return provenance_service.provenance_for_paper(session, paper.id)[field]


def as_utc(moment: datetime) -> datetime:
    """测试库是 sqlite，``DateTime(timezone=True)`` 读回来是 naive —— 值本身仍是 UTC。

    生产是 ``timestamptz``，不会出现这一层；归一化只是让断言与库无关。
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def assert_stamped(moment: datetime | None) -> None:
    assert moment is not None, "裁决路径必须写 decision_at"
    assert as_utc(moment) >= datetime.now(timezone.utc) - timedelta(seconds=2)
    assert as_utc(moment) <= datetime.now(timezone.utc) + timedelta(seconds=2)


# --------------------------------------------------------------------------- #
# 助手本身
# --------------------------------------------------------------------------- #


def test_record_human_decision_stamps_the_moment(db_session) -> None:
    paper = make_paper(db_session)
    claim = seed_claim(db_session, paper, "title", "A title")

    assert claim.decision_at is None, "机器写入：没有任何人类裁决过"

    provenance_service.record_human_decision(claim)

    assert_stamped(claim.decision_at)
    # 不给 decided_by 就只盖时间戳（调用方可能已经自己写过了）
    assert claim.decided_by == provenance_service.DECIDED_INITIAL


def test_record_human_decision_can_write_both_halves(db_session) -> None:
    paper = make_paper(db_session)
    claim = seed_claim(db_session, paper, "title", "A title")

    provenance_service.record_human_decision(
        claim, provenance_service.DECIDED_DISMISSED
    )

    assert claim.decided_by == provenance_service.DECIDED_DISMISSED
    assert_stamped(claim.decision_at)


# --------------------------------------------------------------------------- #
# 三个裁决入口都要盖
# --------------------------------------------------------------------------- #


def test_dismissing_a_conflict_records_when(db_session) -> None:
    paper = make_paper(db_session)
    keeper = seed_claim(db_session, paper, "title", "First title")
    loser = seed_claim(db_session, paper, "title", "Second title")

    assert loser.is_current is True and keeper.is_current is False

    provenance_service.dismiss_claim(
        db_session, paper_id=paper.id, field="title", provenance_id=keeper.id
    )

    assert keeper.decided_by == provenance_service.DECIDED_DISMISSED
    assert_stamped(keeper.decision_at)


def test_rolling_back_records_both_sides(db_session) -> None:
    """「采纳被拒值」：被扶正的那条与被顶掉的那条都有人类动作时间。"""
    paper = make_paper(db_session)
    first = seed_claim(db_session, paper, "title", "First title")
    second = seed_claim(db_session, paper, "title", "Second title")
    assert second.is_current and not first.is_current

    provenance_service.rollback_field(db_session, paper, "title", first.id)

    db_session.refresh(first)
    db_session.refresh(second)
    assert first.is_current is True
    assert_stamped(first.decision_at)
    assert second.decided_by == provenance_service.DECIDED_DISMISSED
    assert_stamped(second.decision_at)


def test_a_manual_edit_records_when(db_session) -> None:
    paper = make_paper(db_session)
    seed_claim(db_session, paper, "title", "Original title")

    metadata_manual.patch_metadata(db_session, paper, {"title": "Edited title"})

    edited = [row for row in claims_for(db_session, paper, "title") if row.value == "Edited title"]
    assert len(edited) == 1
    assert edited[0].decided_by == provenance_service.DECIDED_MANUAL
    assert_stamped(edited[0].decision_at)


# --------------------------------------------------------------------------- #
# 机器路径不许冒充裁决
# --------------------------------------------------------------------------- #


def test_machine_merges_carry_no_decision_time(db_session) -> None:
    """合并引擎写的 claim 不该有裁决时间 —— 有的话，清单会显示"人类裁决过"的假象。"""
    paper = make_paper(db_session)
    # 先有一条结构化声明，再让合并引擎覆盖它（走 set_field 的机器分支）
    seed_claim(db_session, paper, "venue", "First venue")
    metadata_merge.merge_values(
        db_session, paper, {"venue": "Second venue"}, source_type="crossref"
    )

    rows = claims_for(db_session, paper, "venue")
    assert len(rows) >= 2
    for claim in rows:
        assert claim.decision_at is None


def test_an_imported_manual_source_is_attribution_not_a_verdict(db_session) -> None:
    """``decided_by='manual'`` 也可能是**导入**一份人工来源 —— 那种不算裁决。

    这正是"用 ``decided_by`` 猜裁决时刻"会错的地方：导入时刻会被冒充成裁决时刻。
    """
    paper = make_paper(db_session)
    source = sources.upsert_source(
        db_session,
        source_type="manual",  # 人工来源，但这次写入发生在导入期
        source_ref=f"manual:{paper.id}",
        raw={},
        paper_id=paper.id,
        match_status=sources.MATCH_STATUS_MATCHED,
    )
    claim = provenance_service.set_field(
        db_session,
        paper,
        "title",
        "Imported by hand",
        source_id=source.id,
        decided_by=provenance_service.DECIDED_MANUAL,
        override=True,
    )

    assert claim is not None
    assert claim.decided_by == provenance_service.DECIDED_MANUAL
    assert claim.decision_at is None, "导入期写入没有人的点击，不能盖裁决时间"


# --------------------------------------------------------------------------- #
# 接口要透出去
# --------------------------------------------------------------------------- #


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


def test_the_metadata_ledger_exposes_both_timestamps(client, session_factory) -> None:
    """账本里 ``decided_at`` 与 ``decision_at`` 必须都在，且语义可区分。"""
    session = session_factory()
    try:
        paper = make_paper(session)
        keeper = seed_claim(session, paper, "title", "First title")
        seed_claim(session, paper, "title", "Second title")
        provenance_service.dismiss_claim(
            session, paper_id=paper.id, field="title", provenance_id=keeper.id
        )
        session.commit()
        paper_id = paper.id
    finally:
        session.close()

    payload = client.get(f"/api/papers/{paper_id}/metadata").json()
    rows = payload["provenance"]["title"]
    dismissed = [row for row in rows if row["decided_by"] == "dismissed"]

    assert dismissed, "驳回过的声明应该留在账本里"
    assert dismissed[0]["decision_at"] is not None, "裁决时刻要能读出来"
    # 语义不同：一个来自 server_default（写入账本），一个是人的点击
    assert dismissed[0]["decided_at"] is not None
    assert dismissed[0]["decision_at"] != dismissed[0]["decided_at"]
