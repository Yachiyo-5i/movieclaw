"""add library_file.file_mtime_ns

媒体文件 mtime（纳秒）落库：扫描/入库时随 stat 顺手记录，Jellyfin 兼容层的
MediaSource ETag 改为直接由它派生。此前 ETag 在每次列表/详情请求里现场
stat 媒体文件本体，云盘挂载上一次 stat 就是一次网络往返——1200 部电影的
列表请求要 20 多秒（issue #88）。纯加列、可空，向前兼容：旧行 NULL 时
省略 ETag（纯缓存语义），下次扫描自动补齐。

Revision ID: f3a7c8d21b45
Revises: e9f2b5da1728
Create Date: 2026-08-05 14:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a7c8d21b45"
down_revision: str | None = "e9f2b5da1728"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("file_mtime_ns", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_column("file_mtime_ns")
