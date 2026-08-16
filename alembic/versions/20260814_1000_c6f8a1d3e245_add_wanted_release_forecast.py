"""add wanted release forecast: 单集资源发布时间预测快照

预测只保存到对应 wanted 工单；原始发布观测继续以 site_torrent 为唯一事实源，
站点临时同步提示由预测快照与 site_sync_cursor 动态推导，不新增重复表。
字段可空、旧行无需回填，旧版本回退时忽略本列，符合向前兼容迁移约束。

Revision ID: c6f8a1d3e245
Revises: b5e7f9c1d324
Create Date: 2026-08-14 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6f8a1d3e245"
down_revision: str | None = "b5e7f9c1d324"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.add_column(sa.Column("release_forecast", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.drop_column("release_forecast")
