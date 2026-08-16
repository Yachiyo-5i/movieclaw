"""add subtitle columns: 外挂字幕台账 + 轨选择记忆

字幕支持（docs/design/jellyfin-subtitle.md）的数据层：

- ``library_file.external_subtitles``（JSON 可空）——外挂字幕台账，与
  内封 ``subtitle_streams`` 平行分列（数据来源与失效键不同）。三态：
  NULL=未发现过（存量行，重扫回填），[]=发现过但没有；
- ``playback_state.audio_track`` / ``playback_state.subtitle_track``
  （文本可空）——轨选择记忆，存协议无关的中性轨引用
  （"embedded:<k>" / "external:<文件名>" / 字幕特有 "off"），随
  playback_state 的成员维度天然按人隔离。NULL=从未上报。

纯增列、可空、无回填，向前兼容（发布规范硬约束 3）。

Revision ID: d0e6f2b84a17
Revises: c9d5e1a73f06
Create Date: 2026-08-10 22:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0e6f2b84a17"
down_revision: str | None = "c9d5e1a73f06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("external_subtitles", sa.JSON(), nullable=True))

    with op.batch_alter_table("playback_state", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_track", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("subtitle_track", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("playback_state", schema=None) as batch_op:
        batch_op.drop_column("subtitle_track")
        batch_op.drop_column("audio_track")

    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_column("external_subtitles")
