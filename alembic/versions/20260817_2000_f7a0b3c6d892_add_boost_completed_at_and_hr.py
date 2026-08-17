"""刷流台账：下载完成时间 + H&R 标注列

- ratio_boost_task.completed_at：首次观测到下载完成的时间。汰换判定窗口
  改为以完成时刻起算（完成前是在下载，谈不上"传得慢"），配合零产出速汰
  策略及时淘汰"下完却一直传不动"的种子。
- ratio_boost_task.hit_and_run：准入时该种是否明确标注 H&R 考核。True 的
  任务保留期取 max(站点保留期, 站点 hr_seed_hours)——真实考核时长优先于
  用户配置的保底值。

向前兼容说明：completed_at nullable（历史已完成任务回退用 created_at 估算，
判定只会更保守不会误杀）；hit_and_run 带 server_default=0（历史任务本就
只准入过非 H&R 种，语义一致）。旧代码忽略两列即可，回退不丢数据。

Revision ID: f7a0b3c6d892
Revises: e6f9a2b5c781
Create Date: 2026-08-17 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a0b3c6d892"
down_revision: str | None = "e6f9a2b5c781"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ratio_boost_task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "hit_and_run",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("ratio_boost_task", schema=None) as batch_op:
        batch_op.drop_column("hit_and_run")
        batch_op.drop_column("completed_at")
