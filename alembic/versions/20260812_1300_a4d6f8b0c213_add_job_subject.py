"""add job subject: 任务中心用户可读对象名

Job 首版迁移可能已经在开发库执行，新增字段必须使用独立向前迁移，不能修改
已落库 revision 后期待 Alembic 重跑。字段可空，旧版本忽略，回退安全。

Revision ID: a4d6f8b0c213
Revises: f3c5d7e9a102
Create Date: 2026-08-12 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4d6f8b0c213"
down_revision: str | None = "f3c5d7e9a102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job", sa.Column("subject", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("job", "subject")
