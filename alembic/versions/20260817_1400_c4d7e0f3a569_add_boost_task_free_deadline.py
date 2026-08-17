"""刷流台账补记免费截止快照，支撑「免费窗口已过仍未下完」的止损放弃

准入时把索引里的 free_deadline 快照进台账：对账时发现窗口已过、任务还差
不少没下完，就删除止损——继续下载是付费下载，正是刷流要避免的。

向前兼容说明：nullable 新列无需默认值，已有行保持 NULL（语义=无截止，
止损不触发，行为与加列前一致）；旧代码回退后忽略本列，不丢数据。

Revision ID: c4d7e0f3a569
Revises: b3c6d9e2f458
Create Date: 2026-08-17 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d7e0f3a569"
down_revision: str | None = "b3c6d9e2f458"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ratio_boost_task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("free_deadline", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ratio_boost_task", schema=None) as batch_op:
        batch_op.drop_column("free_deadline")
