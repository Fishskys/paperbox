"""api_keys: 一把钥匙只有一个标识符，prefix 放宽到 32

背景
----
``api_keys`` 原本有两个并列的唯一标识：``name``（约束 ``uq_api_keys_name``，人类可读）
与 ``prefix``（``uq_api_keys_prefix``，日志可见）。实践中两者语义重叠、又都必须由运维填写，
于是「创建一把钥匙」要起两个名字，还得先理解它们的区别；而 ``name`` 只在 MCP 侧的
``Envelope.meta.agent`` 与 ``mcp_call`` 审计行出现，REST 侧完全不可见 —— 一个字段存在的
理由不足以支撑它的维护成本。

改法
----
``prefix`` 成为唯一标识：它就是创建时填的那一个值，同时承担日志归因
（请求日志 ``[key=<prefix>]``、``search_queries.key_prefix``）与审计标签
（``meta.agent``）两个角色。同时把上限从 16 放到 32 —— 16 位连 ``alice-hermes-laptop``
都装不下，多用户/多机命名只能靠缩写，而日志归因最怕缩写歧义。

- 去掉唯一约束 ``uq_api_keys_name`` 与列 ``name``；
- ``prefix`` 由 ``varchar(16)`` 放宽到 ``varchar(32)``；
- 既有行的 ``prefix`` 值不变（本机为 ``webui`` / ``hermes``），所以既有客户端不受影响。

降级
----
降级会重建 ``name`` 并用 ``prefix`` 回填（旧值与当前 prefix 同义），但 ``prefix`` 缩回 16 位
会截断超长值 —— 那等于悄悄换掉一把钥匙的标识，所以只要存在超过 16 位的 prefix 就**拒绝降级**，
由运维先把它们改短。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '9c1d4b7a2e01'
down_revision: str | None = 'a41f7c2d9b30'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_api_keys_name", "api_keys", type_="unique")
    op.alter_column(
        "api_keys",
        "prefix",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
    )
    op.drop_column("api_keys", "name")


def downgrade() -> None:
    connection = op.get_bind()
    too_long = (
        connection.execute(
            sa.text("select prefix from api_keys where length(prefix) > 16 order by prefix")
        )
        .scalars()
        .all()
    )
    if too_long:
        raise RuntimeError(
            "refusing to downgrade: these prefixes exceed the old 16-character "
            f"ceiling and would be silently renamed: {list(too_long)}"
        )
    op.add_column("api_keys", sa.Column("name", sa.String(64), nullable=True))
    connection.execute(sa.text("update api_keys set name = prefix where name is null"))
    op.alter_column("api_keys", "name", existing_type=sa.String(64), nullable=False)
    op.create_unique_constraint("uq_api_keys_name", "api_keys", ["name"])
    op.alter_column(
        "api_keys",
        "prefix",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
    )
