"""add library_file browse index

Jellyfin 首页的 Latest 和默认电影库分页只需要库、在位状态、媒体单元与
入库时间。``library_file`` 同时存储音轨/字幕 JSON，缺少覆盖索引时 SQLite
必须扫描这些宽行；该索引让热点查询只读取索引页。

Revision ID: c9f1a7e2b406
Revises: f3a7c8d21b45
Create Date: 2026-08-06 19:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9f1a7e2b406"
down_revision: str | None = "f3a7c8d21b45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_library_file_browse_unit",
        "library_file",
        [
            "library_id",
            "missing_since",
            "media_item_id",
            "season_number",
            "episode_number",
            "created_at",
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_library_file_browse_unit", table_name="library_file")
