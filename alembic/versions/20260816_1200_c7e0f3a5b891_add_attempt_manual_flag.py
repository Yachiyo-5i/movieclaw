"""add subscription_download_attempt.manual（手动选种标记）

手动选种是用户对规则的显式覆盖，洗版验证对它的裁决必须不同于自动投递
（docs/design/quality-upgrade.md §13.8）：自动投递证伪 → 文件进回收站 +
熔断计数；手动投递未能证明更优 → 新旧版本**共存保留**——用户显式挑的
文件绝不能被系统丢弃，熔断与排除清单也不因用户的选择受罚。

布尔列、缺省 false、无应用端必填约束：旧代码忽略该列即回到"全部按自动
投递裁决"的旧行为，符合应用内一键回退的向前兼容契约。

Revision ID: c7e0f3a5b891
Revises: b6d9e2f4a780
Create Date: 2026-08-16 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e0f3a5b891"
down_revision: str | None = "b6d9e2f4a780"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("subscription_download_attempt", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "manual",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("subscription_download_attempt", schema=None) as batch_op:
        batch_op.drop_column("manual")
