"""add member dimensions: 播放状态/订阅归属/Jellyfin 设备接入成员维度

成员管理（docs/design/member-management.md）P1 的数据层：

- ``playback_state.member_id``（NOT NULL DEFAULT 0，0=超管哨兵）进唯一
  约束——进度/已看/收藏按人隔离。刻意不用可空列：SQLite 的 UNIQUE 视
  NULL 互不相等，可空列进唯一约束会让同一单元插出无限行。存量行保持 0，
  即历史进度归超管（此前只有超管在用，语义正确）；
- ``subscription.created_by_member_id``（可空外键，NULL=超管发起，成员
  删除时 SET NULL 自动转超管）+ ``subscription_follower`` 关注表——订阅
  保持全局唯一，第二个成员再订同一部转为关注；
- ``jellyfin_device.member_id``（NOT NULL DEFAULT 0）——电视端登录绑定
  真实身份，Policy 与观看状态按成员投影。

唯一约束重建走 batch 模式（SQLite 建新表拷贝），其余为新表与带默认值的
加列，向前兼容。

Revision ID: b8c4d0f62e95
Revises: a7b3c9e51d84
Create Date: 2026-08-10 16:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c4d0f62e95"
down_revision: str | None = "a7b3c9e51d84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 播放状态：加成员列并把唯一约束扩为 (member, item, season, episode)
    with op.batch_alter_table("playback_state", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("member_id", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_index("ix_playback_state_member_id", ["member_id"], unique=False)
        batch_op.drop_constraint("uq_playback_state_unit", type_="unique")
        batch_op.create_unique_constraint(
            "uq_playback_state_unit",
            ["member_id", "media_item_id", "season_number", "episode_number"],
        )

    # 订阅归属：发起人可空外键（成员删除 SET NULL = 转超管发起）
    with op.batch_alter_table("subscription", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("created_by_member_id", sa.Integer(), nullable=True)
        )
        batch_op.create_index(
            "ix_subscription_created_by_member_id", ["created_by_member_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_subscription_created_by_member",
            "member",
            ["created_by_member_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 订阅关注表
    op.create_table(
        "subscription_follower",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscription.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subscription_id", "member_id", name="uq_subscription_follower"),
    )
    op.create_index(
        "ix_subscription_follower_subscription_id",
        "subscription_follower",
        ["subscription_id"],
        unique=False,
    )
    op.create_index(
        "ix_subscription_follower_member_id",
        "subscription_follower",
        ["member_id"],
        unique=False,
    )

    # Jellyfin 设备：登录身份（0=超管哨兵）
    with op.batch_alter_table("jellyfin_device", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("member_id", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_index("ix_jellyfin_device_member_id", ["member_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("jellyfin_device", schema=None) as batch_op:
        batch_op.drop_index("ix_jellyfin_device_member_id")
        batch_op.drop_column("member_id")

    op.drop_index("ix_subscription_follower_member_id", table_name="subscription_follower")
    op.drop_index(
        "ix_subscription_follower_subscription_id", table_name="subscription_follower"
    )
    op.drop_table("subscription_follower")

    with op.batch_alter_table("subscription", schema=None) as batch_op:
        batch_op.drop_constraint("fk_subscription_created_by_member", type_="foreignkey")
        batch_op.drop_index("ix_subscription_created_by_member_id")
        batch_op.drop_column("created_by_member_id")

    with op.batch_alter_table("playback_state", schema=None) as batch_op:
        batch_op.drop_constraint("uq_playback_state_unit", type_="unique")
        batch_op.create_unique_constraint(
            "uq_playback_state_unit",
            ["media_item_id", "season_number", "episode_number"],
        )
        batch_op.drop_index("ix_playback_state_member_id")
        batch_op.drop_column("member_id")
