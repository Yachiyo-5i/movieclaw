"""repair a partially applied library statistics migration

SQLite 的批量改表会重建整张表，DDL 又不能随事务完整回滚。若迁移进程在
重建期间异常退出，Alembic 版本可能已经前进，但个别统计列没有落下。这里
只修复实际缺失的分集统计列，并从库存台账重新回填，避免覆盖其他统计快照。

Revision ID: e3a6b9d2f457
Revises: d2f5a8c1e346
Create Date: 2026-08-15 17:45:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3a6b9d2f457"
down_revision: str | None = "d2f5a8c1e346"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("library")}
    if "stats_episode_count" not in columns:
        with op.batch_alter_table("library", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "stats_episode_count",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )

    # 只重算本次修复的字段。分集身份由同一条目的季号、集号共同确定，
    # missing 行不代表当前库存，不能计入卡片与 Jellyfin 的分集总数。
    op.execute(
        sa.text(
            """
            UPDATE library
            SET stats_episode_count = CASE
                WHEN kind = 'tv' THEN (
                    SELECT COUNT(*)
                    FROM (
                        SELECT 1
                        FROM library_file
                        WHERE library_file.library_id = library.id
                          AND library_file.missing_since IS NULL
                          AND library_file.media_item_id IS NOT NULL
                        GROUP BY library_file.media_item_id,
                                 library_file.season_number,
                                 library_file.episode_number
                    ) AS distinct_episode
                )
                ELSE 0
            END
            """
        )
    )


def downgrade() -> None:
    # d2 的正常前置版本 c1 本就包含该列；修复迁移降级时必须保留 c1 契约。
    pass
