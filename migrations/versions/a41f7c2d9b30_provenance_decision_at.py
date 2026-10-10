"""provenance decision_at: when a human ruled on a claim

Revision ID: a41f7c2d9b30
Revises: b7e2f90a4c31
Create Date: 2026-10-10

为什么加这一列
--------------
``paper_field_provenance.decided_at`` 只在**声明写入账本**时由 ``server_default=func.now()``
赋值；两条人工裁决接口（``POST /api/papers/{id}/metadata/conflicts/dismiss``、
``.../metadata/rollback``）与手工 PATCH 都只写 ``decided_by``，从不更新它。于是列名承诺的
"裁决时刻"根本不存在：真机上想回答"这几条冲突是什么时候被裁决的"，只能去翻应用的访问日志，
而日志只覆盖当前窗口（2026-10-10 排查就是这么绕的）。

新列 ``decision_at`` 只记**人类动作的时刻**，``NULL`` 表示这一行没有任何人类裁决过
（机器合并的 ``initial`` / ``structured_override``）。``decided_at`` 保持原样不动 ——
它渲染的是"这条声明何时进入账本"，已有消费方按它排序（复核清单按落败声明的写入时间倒序）。

不回填历史数据
--------------
既有裁决时刻没有权威来源：日志窗口外的裁决无从考证，而按 ``decided_at`` 拿"声明写入时间"
冒充裁决时间正是这次要修的错误。所以宁可为空 —— 摘要里说"未知"，好过写一个看着像答案的错值。
（需要的话可以用 ``scripts/backfill_decision_times.py`` 按访问日志精确回填，那是可审计的一次性动作。）
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a41f7c2d9b30'
down_revision: str | None = 'b7e2f90a4c31'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper_field_provenance",
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("paper_field_provenance", "decision_at")
