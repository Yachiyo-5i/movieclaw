"""finalize job contract: 补齐版本追踪与资源消耗字段

首版 Job 迁移在开发库执行后，任务契约又增加了处理器/提示词/模型引用和
资源消耗字段。使用独立向前迁移补齐，保留所有已存在任务与事件。

Revision ID: b5e7f9c1d324
Revises: a4d6f8b0c213
Create Date: 2026-08-12 13:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5e7f9c1d324"
down_revision: str | None = "a4d6f8b0c213"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 非空列带服务端默认，存量 Job 原地回填；默认同时保留，保证极端情况下
    # 旧应用回退后仍可插入不破坏新约束的数据。
    op.add_column(
        "job",
        sa.Column("handler_revision", sa.Text(), nullable=False, server_default="1"),
    )
    op.add_column("job", sa.Column("prompt_revision", sa.Text(), nullable=True))
    op.add_column("job", sa.Column("provider_ref", sa.Text(), nullable=True))
    op.add_column(
        "job",
        sa.Column("usage", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("job", "usage")
    op.drop_column("job", "provider_ref")
    op.drop_column("job", "prompt_revision")
    op.drop_column("job", "handler_revision")
