"""add ingest_entry.media_item_id: 台账记住条目对应的作品

监听导入摘要行的「已入库 N」此前数的是台账条目数，而条目 = 源目录顶层项
（一次下载任务的产物）。一部剧的 S01/S02、同一季的不同发布组与 DV/HDR
版本，都是各自独立的条目却同属一部作品——于是入库 4 部剧显示成「已入库
11」，与用户认知严重不符。台账记下每个条目识别/认领到的作品，汇总时按它
去重，「已入库」才是"入库了几部"。

老数据回填：入库时的落点目录形如 ``<库根>/<标题> (年份)``，与 library_file
的 file_path 前缀一致，据此把条目关联回作品——只认唯一命中，一个落点目录
对应多个作品（理论上不该出现）则留空。回填不了的老条目在汇总时按条目数
计入（宁可少合并，不凭标题猜）。

仅加可空列 + 回填，向前兼容。

Revision ID: d1e4b8f30527
Revises: c9f1a7e2b406
Create Date: 2026-08-07 19:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e4b8f30527"
down_revision: str | None = "c9f1a7e2b406"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ingest_entry", schema=None) as batch_op:
        batch_op.add_column(sa.Column("media_item_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            "ix_ingest_entry_media_item_id", ["media_item_id"], unique=False
        )

    # 回填：从落点消息里解析出的目录无从取得（message 是自由文本），改用
    # 更稳的路径——入库文件的父目录即落点目录，而落点目录由「标题 (年份)」
    # 决定；同一条目入库的文件都在同一落点目录下。以 (标题, 年份) 唯一命中
    # 为准，多命中留空交给汇总兜底。
    op.execute(
        """
        UPDATE ingest_entry
        SET media_item_id = (
            SELECT mi.id FROM media_item mi
            WHERE ingest_entry.message LIKE '%《' || mi.title || '》%'
              AND (
                  mi.year IS NULL
                  OR ingest_entry.message LIKE '%(' || CAST(mi.year AS TEXT) || ')%'
              )
            LIMIT 1
        )
        WHERE status = 'imported'
          AND media_item_id IS NULL
          AND message IS NOT NULL
          AND (
              SELECT COUNT(*) FROM media_item mi2
              WHERE ingest_entry.message LIKE '%《' || mi2.title || '》%'
                AND (
                    mi2.year IS NULL
                    OR ingest_entry.message LIKE '%(' || CAST(mi2.year AS TEXT) || ')%'
                )
          ) = 1
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("ingest_entry", schema=None) as batch_op:
        batch_op.drop_index("ix_ingest_entry_media_item_id")
        batch_op.drop_column("media_item_id")
