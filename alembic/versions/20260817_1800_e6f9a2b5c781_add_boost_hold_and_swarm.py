"""刷流保留期每站可配 + 台账蜂群快照列

- site_credential.boost_hold_days：汰换最低保留天数（H&R 安全垫），默认 3；
  无 H&R 考核的站点可调 0 = 自由汰换。此前是写死的 72 小时常量。
- ratio_boost_task.swarm_seeders/swarm_leechers：下载器每 tick 回报的蜂群
  快照，汰换策略的第二信源（死种立即汰换、年轻热种免于误杀）。

向前兼容说明：boost_hold_days 带 server_default=3（与旧常量等价，已有行为
不变）；蜂群两列 nullable 无默认，旧代码忽略即可，回退不丢数据。

Revision ID: e6f9a2b5c781
Revises: d5e8f1a4b670
Create Date: 2026-08-17 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f9a2b5c781"
down_revision: str | None = "d5e8f1a4b670"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("site_credential", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "boost_hold_days",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("3"),
            )
        )
    with op.batch_alter_table("ratio_boost_task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("swarm_seeders", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("swarm_leechers", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ratio_boost_task", schema=None) as batch_op:
        batch_op.drop_column("swarm_leechers")
        batch_op.drop_column("swarm_seeders")
    with op.batch_alter_table("site_credential", schema=None) as batch_op:
        batch_op.drop_column("boost_hold_days")
