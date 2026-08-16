"""add manual_download_intent.torrent_id（站点内种子 ID）

任务中心的「打开种子页」需要用 site_id + torrent_id 反查 site_torrent 的
详情页 URL。订阅投递台账早已记录这两个字段，手动下载的身份锚此前只存了
site_id，无法定位到站内种子——补上 torrent_id 后手动任务同样能回链种子页。

可空列、无默认值约束：旧代码忽略该列即回到"手动任务没有种子页入口"的
旧行为，符合应用内一键回退的向前兼容契约。

Revision ID: e9a2b5c7d013
Revises: d8f1a4b6c902
Create Date: 2026-08-16 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9a2b5c7d013"
down_revision: str | None = "d8f1a4b6c902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("manual_download_intent", schema=None) as batch_op:
        batch_op.add_column(sa.Column("torrent_id", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("manual_download_intent", schema=None) as batch_op:
        batch_op.drop_column("torrent_id")
