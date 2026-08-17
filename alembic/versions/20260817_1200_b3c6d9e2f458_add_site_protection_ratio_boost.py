"""站点保护开关与自动刷分享率：site_credential 三列 + ratio_boost_task 台账表

设计见 docs/design/site-protection-ratio-boost.md。

向前兼容说明：
- site_credential 新列全部带 server_default（保护关、刷流关、预算 100 GiB），
  已有行原地补默认值；旧代码不认识这些列——不丢数据只丢语义，回退后所有
  站点表现为"未保护、未刷流"，符合应用内一键回退的向前兼容契约。
- ratio_boost_task 是独立新表，旧版本回退时被忽略；其中记录的下载器任务
  仍留在下载器里做种，升级回来后引擎按 infohash 对账无缝接管。

Revision ID: b3c6d9e2f458
Revises: a2b5c8d1e347
Create Date: 2026-08-17 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c6d9e2f458"
down_revision: str | None = "a2b5c8d1e347"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("site_credential", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "protected",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "boost_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "boost_budget_bytes",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("107374182400"),  # 100 GiB
            )
        )

    op.create_table(
        "ratio_boost_task",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Text(), nullable=False),
        sa.Column("torrent_id", sa.Text(), nullable=False),
        sa.Column("info_hash", sa.Text(), nullable=False),
        sa.Column("downloader_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("uploaded_bytes", sa.BigInteger(), nullable=False),
        sa.Column("upload_rate_ema", sa.Float(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("evicted_at", sa.DateTime(), nullable=True),
        sa.Column("evict_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["downloader_id"], ["downloader_client.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("info_hash"),
    )
    for name in ("site_id", "info_hash", "state"):
        op.create_index(
            f"ix_ratio_boost_task_{name}", "ratio_boost_task", [name], unique=False
        )


def downgrade() -> None:
    for name in ("state", "info_hash", "site_id"):
        op.drop_index(f"ix_ratio_boost_task_{name}", table_name="ratio_boost_task")
    op.drop_table("ratio_boost_task")
    with op.batch_alter_table("site_credential", schema=None) as batch_op:
        batch_op.drop_column("boost_budget_bytes")
        batch_op.drop_column("boost_enabled")
        batch_op.drop_column("protected")
