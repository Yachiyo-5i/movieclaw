"""add quality-upgrade (洗版) columns

洗版功能（docs/design/quality-upgrade.md）的模型地基：

- wanted_item.quality：满足该单元的当前版本质量快照（QualitySnapshot JSON），
  洗版比较的唯一基线；NULL=未满足或无法识别；
- wanted_item.upgrade_verify_failures：洗版候选连续证伪计数（熔断输入），
  确认升级时清零；
- subscription_download_attempt.purpose：投递目的（download/upgrade），
  洗版在途去重与活动文案分流的依据。

三列都有安全缺省且无应用端必填约束：用户回退旧版本后旧代码可继续写入
两张表，符合应用内一键回退的向前兼容契约（洗版字段被旧代码忽略即等于
不洗版）。

Revision ID: b6d9e2f4a780
Revises: a5c8d1e3f679
Create Date: 2026-08-16 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d9e2f4a780"
down_revision: str | None = "a5c8d1e3f679"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.add_column(sa.Column("quality", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "upgrade_verify_failures",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
    with op.batch_alter_table("subscription_download_attempt", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "purpose",
                sa.String(),
                nullable=False,
                server_default="download",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("subscription_download_attempt", schema=None) as batch_op:
        batch_op.drop_column("purpose")
    with op.batch_alter_table("wanted_item", schema=None) as batch_op:
        batch_op.drop_column("upgrade_verify_failures")
        batch_op.drop_column("quality")
