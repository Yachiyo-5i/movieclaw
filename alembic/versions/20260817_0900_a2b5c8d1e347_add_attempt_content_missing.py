"""下载尝试记录内容核验的负面记忆

种子下完后文件清单里明确没有的单元记在 ``content_missing`` 上，匹配层据此
跳过同一份内容，避免"退回重找 → 又选中同一个种子"的无限循环。

回退兼容：旧代码不认识本列，插入走库端默认 ``{}``、读取忽略，负面记忆仍
留在库里；回到新版本后继续生效——不丢数据只丢语义，符合应用内一键回退的
向前兼容契约。

Revision ID: a2b5c8d1e347
Revises: f1c4d7e9b025
Create Date: 2026-08-17 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2b5c8d1e347"
down_revision: str | None = "f1c4d7e9b025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("subscription_download_attempt", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "content_missing",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("subscription_download_attempt", schema=None) as batch_op:
        batch_op.drop_column("content_missing")
