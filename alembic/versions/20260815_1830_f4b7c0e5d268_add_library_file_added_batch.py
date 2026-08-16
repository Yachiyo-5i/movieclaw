"""record the library-file addition batch

「最近添加」此前只能拿到条目的累计季集数，无法区分本轮新入库的单元。
新增可空批次号，由扫描和监听入库在首次写台账时填写；历史行保持 NULL，
前端以入库时间安全兜底。

字段必须可空且没有应用端必填约束：用户回退到旧版本后，旧代码仍可继续
写入 library_file，符合应用内一键回退所需的向前兼容契约。

Revision ID: f4b7c0e5d268
Revises: e3a6b9d2f457
Create Date: 2026-08-15 18:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4b7c0e5d268"
down_revision: str | None = "e3a6b9d2f457"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("added_batch_id", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_column("added_batch_id")
