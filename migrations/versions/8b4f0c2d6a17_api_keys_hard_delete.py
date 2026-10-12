"""api_keys: 吊销即删除，去掉 revoked_at

背景
----
``revoke`` 原本写 ``revoked_at``（软删），行留在表里只为了 ``list`` 还能显示「已吊销」。
但一个没人持有的行买不到运维想要的东西：它**占着 prefix**（于是同一个 prefix 无法重发，
除非手写 SQL），而且让每次鉴权都要多过滤一个几乎恒为 ``NULL`` 的列。钥匙真正退役时的动作是
**重发**（同 prefix 再 ``create``，见 ``app/services/api_key_service.py::replace_key``），
那条路会替换掉这一行 —— 所以表里只留能被持有的钥匙。

改法
----
- 去掉列 ``revoked_at``；``revoke_key()`` 改为删除该行（硬删，不可恢复）；
- ``authenticate()`` 不再过滤 ``revoked_at``；``live_key_count()`` 改名 ``key_count()``
  （每一行都是可用钥匙，没有需要排除的退役状态）；
- ``manage_keys.py list`` 不再有 status 列。

降级
----
重新加回可空的 ``revoked_at`` 列（历史吊销事实无法还原，全部为 ``NULL``）：这是信息丢失，
不是数据损坏 —— 该列本就只用于显示。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '8b4f0c2d6a17'
down_revision: str | None = '9c1d4b7a2e01'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("api_keys", "revoked_at")


def downgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
