"""add library_file media_source_manual（片源人工标注保护位）

docs/design/media-source-annotation.md §3：扫描入库文件的片源常因文件名无
片源词而无法解析，用户可整季人工标注。标注值写入 media_source，本列置真
表示该值为人工判定——重扫/重识别的自动名称解析不得覆盖（upsert 保留、
重复行合并时人工值优先）。

回退兼容：旧代码不认识本列，插入走库端默认 false、读取忽略，人工标注值
仍留在 media_source 里可正常展示，只是失去覆盖保护——不丢数据只丢语义，
符合应用内一键回退的向前兼容契约。

Revision ID: f1c4d7e9b025
Revises: e9a2b5c7d013
Create Date: 2026-08-16 17:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1c4d7e9b025"
down_revision: str | None = "e9a2b5c7d013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "media_source_manual",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_column("media_source_manual")
