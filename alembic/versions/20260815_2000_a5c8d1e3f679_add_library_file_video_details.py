"""add frame rate and color space to library files

详情页需要展示当前物理文件的帧率与色彩空间，这两项必须来自 ffprobe 对文件
本体的探测，不能从资源名猜测。新增列保持可空：历史数据等待用户下次整库
扫描补齐；回退到旧应用版本后，旧代码也能继续读写同一张表。

Revision ID: a5c8d1e3f679
Revises: f4b7c0e5d268
Create Date: 2026-08-15 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5c8d1e3f679"
down_revision: str | None = "f4b7c0e5d268"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("frame_rate", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("color_space", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_column("color_space")
        batch_op.drop_column("frame_rate")
