"""add subscription download attempt ledger

下载换源需要同时保留旧源、试用源与失败候选，不能继续只靠
wanted_item.info_hash 表达。新表纯新增，旧版本会忽略它；wanted_item 结构不变，
跨版本回退仍能沿用当前主 infohash，符合应用内更新的向前兼容约束。

Revision ID: f9c1e4a6b823
Revises: e8b0d3f5a712
Create Date: 2026-08-14 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "f9c1e4a6b823"
down_revision: str | None = "e8b0d3f5a712"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscription_download_attempt",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("downloader_id", sa.Integer(), nullable=True),
        sa.Column("replaces_attempt_id", sa.Integer(), nullable=True),
        sa.Column("info_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("site_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("torrent_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("torrent_title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("units", sa.JSON(), nullable=False),
        sa.Column("quality", sa.JSON(), nullable=False),
        sa.Column("hit_and_run", sa.Boolean(), nullable=True),
        sa.Column("owned_by_movieclaw", sa.Boolean(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("last_downloader_state", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(), nullable=True),
        sa.Column("baseline_completed_bytes", sa.Integer(), nullable=True),
        sa.Column("last_completed_bytes", sa.Integer(), nullable=True),
        sa.Column("baseline_downloaded_bytes", sa.Integer(), nullable=True),
        sa.Column("last_downloaded_bytes", sa.Integer(), nullable=True),
        sa.Column("last_progress_at", sa.DateTime(), nullable=False),
        sa.Column("stalled_notified_at", sa.DateTime(), nullable=True),
        sa.Column("missing_observations", sa.Integer(), nullable=False),
        sa.Column("next_search_at", sa.DateTime(), nullable=True),
        sa.Column("search_attempts", sa.Integer(), nullable=False),
        sa.Column("last_search_at", sa.DateTime(), nullable=True),
        sa.Column("cleanup_note", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscription.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["downloader_id"], ["downloader_client.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["replaces_attempt_id"],
            ["subscription_download_attempt.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "info_hash",
            name="uq_subscription_download_attempt_hash",
        ),
    )
    with op.batch_alter_table("subscription_download_attempt", schema=None) as batch_op:
        batch_op.create_index(
            "ix_subscription_download_attempt_subscription_id",
            ["subscription_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_subscription_download_attempt_downloader_id",
            ["downloader_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_subscription_download_attempt_replaces_attempt_id",
            ["replaces_attempt_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_subscription_download_attempt_info_hash", ["info_hash"], unique=False
        )
        batch_op.create_index(
            "ix_subscription_download_attempt_status", ["status"], unique=False
        )
        batch_op.create_index(
            "ix_subscription_download_attempt_next_search_at",
            ["next_search_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_subscription_download_attempt_status_due",
            ["status", "next_search_at"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("subscription_download_attempt")
