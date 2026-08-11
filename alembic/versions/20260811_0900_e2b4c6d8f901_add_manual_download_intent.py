"""add manual_download_intent: 手动下载任务的媒体身份锚

手动搜索投递到共享监听目录时，完成后的目录名不一定足以再次识别作品。
提交时已经完成的 TMDB 识别结果以 infohash 持久化，监听导入即可复用身份和
已选库，避免重复猜测或错误落到默认库。

仅新增独立表，不影响既有下载任务与监听台账，向前兼容。

Revision ID: e2b4c6d8f901
Revises: d0e6f2b84a17
Create Date: 2026-08-11 09:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b4c6d8f901"
down_revision: str | None = "d0e6f2b84a17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manual_download_intent",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("info_hash", sa.Text(), nullable=False),
        sa.Column("media_item_id", sa.Integer(), nullable=False),
        sa.Column("library_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(["library_id"], ["library.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_item_id"], ["media_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("manual_download_intent", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_manual_download_intent_info_hash"), ["info_hash"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_manual_download_intent_media_item_id"), ["media_item_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_manual_download_intent_library_id"), ["library_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("manual_download_intent", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_manual_download_intent_library_id"))
        batch_op.drop_index(batch_op.f("ix_manual_download_intent_media_item_id"))
        batch_op.drop_index(batch_op.f("ix_manual_download_intent_info_hash"))
    op.drop_table("manual_download_intent")
