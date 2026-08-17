"""刷流小时桶统计表：支撑「近 24 小时/7 天用 X GB 种子贡献 Y GB 上传」展示

每站每小时一行（uploaded 增量 + 在池占用采样），保留 30 天由引擎清理。

向前兼容说明：独立新表，旧版本回退时被忽略；表内是可再生的统计数据，
丢失只影响展示窗口的完整性，不影响任何决策逻辑。

Revision ID: d5e8f1a4b670
Revises: c4d7e0f3a569
Create Date: 2026-08-17 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e8f1a4b670"
down_revision: str | None = "c4d7e0f3a569"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ratio_boost_stat",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Text(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(), nullable=False),
        sa.Column("uploaded_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_bytes_sum", sa.BigInteger(), nullable=False),
        sa.Column("tick_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "bucket_start"),
    )
    for name in ("site_id", "bucket_start"):
        op.create_index(
            f"ix_ratio_boost_stat_{name}", "ratio_boost_stat", [name], unique=False
        )


def downgrade() -> None:
    for name in ("bucket_start", "site_id"):
        op.drop_index(f"ix_ratio_boost_stat_{name}", table_name="ratio_boost_stat")
    op.drop_table("ratio_boost_stat")
