"""add library_file lifecycle state（文件生命周期第三态：待回收）

docs/design/library-file-recycle.md：把"在位/缺失"从 missing_since 判空
推导升级为显式判别式 state，并新增「待回收」第三态的四个属性列：

- state：in_place / missing / trashed，唯一判别式，按 missing_since 回填存量；
- trashed_at：进入待回收的时间；
- trash_original_path：移入回收站前的原路径（恢复用）；NULL=原地待回收
  （做种保护形态，file_path 未变）；
- purge_after：预计自动删除时间；NULL=不自动删；
- trash_context：审计快照 JSON（reason + 触发方 + 中文 note）。

浏览热路径索引 ix_library_file_browse_unit 的 missing_since 列同步换成
state（口径收口后查询一律过滤 state）。

全部新列有安全缺省：旧代码回退后只认 missing_since，待回收行被当作在位
——已移入回收站的文件随后被对账标缺失、原地保留的照常在位，不丢数据只丢
语义，符合应用内一键回退的向前兼容契约。

Revision ID: d8f1a4b6c902
Revises: c7e0f3a5b891
Create Date: 2026-08-16 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8f1a4b6c902"
down_revision: str | None = "c7e0f3a5b891"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("state", sa.String(), nullable=False, server_default="in_place")
        )
        batch_op.add_column(sa.Column("trashed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("trash_original_path", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("purge_after", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("trash_context", sa.JSON(), nullable=True))
        batch_op.create_index("ix_library_file_state", ["state"])
        batch_op.drop_index("ix_library_file_browse_unit")
        batch_op.create_index(
            "ix_library_file_browse_unit",
            [
                "library_id",
                "state",
                "media_item_id",
                "season_number",
                "episode_number",
                "created_at",
            ],
        )
    # 按 missing_since 回填存量：此后 state 是唯一真相，时间戳只是附属属性
    op.execute(
        "UPDATE library_file SET state = CASE "
        "WHEN missing_since IS NULL THEN 'in_place' ELSE 'missing' END"
    )


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_index("ix_library_file_browse_unit")
        batch_op.create_index(
            "ix_library_file_browse_unit",
            [
                "library_id",
                "missing_since",
                "media_item_id",
                "season_number",
                "episode_number",
                "created_at",
            ],
        )
        batch_op.drop_index("ix_library_file_state")
        batch_op.drop_column("trash_context")
        batch_op.drop_column("purge_after")
        batch_op.drop_column("trash_original_path")
        batch_op.drop_column("trashed_at")
        batch_op.drop_column("state")
