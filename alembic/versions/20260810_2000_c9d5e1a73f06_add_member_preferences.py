"""add member preferences: 搜索历史归属搜索者 + 成员界面偏好

成员管理（docs/design/member-management.md）P2 的数据层：

- ``search_history.member_id``（NOT NULL DEFAULT 0，0=超管哨兵）——搜索
  历史是隐私性最强的个人数据之一，各人只看/只删自己的。存量行归超管
  （此前只有超管在搜，语义正确）；
- ``member.ui_prefs``（JSON 可空）——成员的界面偏好各存各的，NULL 回退
  默认值；超管继续用 ``ui.preferences`` 全局配置域。

带默认值的加列，向前兼容。

Revision ID: c9d5e1a73f06
Revises: b8c4d0f62e95
Create Date: 2026-08-10 20:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d5e1a73f06"
down_revision: str | None = "b8c4d0f62e95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("search_history", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("member_id", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_index("ix_search_history_member_id", ["member_id"], unique=False)

    with op.batch_alter_table("member", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ui_prefs", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("member", schema=None) as batch_op:
        batch_op.drop_column("ui_prefs")

    with op.batch_alter_table("search_history", schema=None) as batch_op:
        batch_op.drop_index("ix_search_history_member_id")
        batch_op.drop_column("member_id")
