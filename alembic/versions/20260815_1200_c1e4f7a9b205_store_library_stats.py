"""store precomputed media library statistics

媒体库列表此前每次请求都扫描全部 library_file 行并在 Python 聚合；库存越大，
首页查询越慢。把派生统计落到 library 行，扫描及其他台账写路径在收尾时重算，
读路径的成本因此只与媒体库数量有关。

新增列保留服务端默认值：应用内回退到旧版本后，旧代码创建媒体库时不会提供
这些字段，数据库仍能正常插入，符合单向迁移的向前兼容约束。

Revision ID: c1e4f7a9b205
Revises: a0d2f5b7c934
Create Date: 2026-08-15 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "c1e4f7a9b205"
down_revision: str | None = "a0d2f5b7c934"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "stats_item_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "stats_episode_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "stats_file_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "stats_total_size_bytes",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "stats_unidentified_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "stats_missing_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "stats_ignored_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(sa.Column("stats_refreshed_at", sa.DateTime(), nullable=True))

    # 一次性回填只读取统计所需标量列，不加载音轨/字幕等大 JSON。迁移发生在
    # 应用启动、对外服务前；逐行 Python 聚合兼容所有现有 SQLite 版本，也避免
    # 依赖 UPDATE FROM 等较新的方言特性。
    connection = op.get_bind()
    library_file = sa.table(
        "library_file",
        sa.column("library_id", sa.Integer()),
        sa.column("media_item_id", sa.Integer()),
        sa.column("season_number", sa.Integer()),
        sa.column("episode_number", sa.Integer()),
        sa.column("size_bytes", sa.BigInteger()),
        sa.column("missing_since", sa.DateTime()),
        sa.column("ignored_at", sa.DateTime()),
    )
    library = sa.table(
        "library",
        sa.column("id", sa.Integer()),
        sa.column("kind", sa.String()),
        sa.column("stats_item_count", sa.Integer()),
        sa.column("stats_episode_count", sa.Integer()),
        sa.column("stats_file_count", sa.Integer()),
        sa.column("stats_total_size_bytes", sa.BigInteger()),
        sa.column("stats_unidentified_count", sa.Integer()),
        sa.column("stats_missing_count", sa.Integer()),
        sa.column("stats_ignored_count", sa.Integer()),
        sa.column("stats_refreshed_at", sa.DateTime()),
    )
    library_kinds = {
        int(row.id): str(row.kind)
        for row in connection.execute(
            sa.select(library.c.id, library.c.kind)
        ).mappings()
    }
    aggregates: dict[int, dict[str, object]] = {}
    for row in connection.execute(sa.select(library_file)).mappings():
        values = aggregates.setdefault(
            int(row.library_id),
            {
                "items": set(),
                "units": set(),
                "file_count": 0,
                "total_size_bytes": 0,
                "unidentified_count": 0,
                "missing_count": 0,
                "ignored_count": 0,
            },
        )
        if row.missing_since is not None:
            values["missing_count"] = int(values["missing_count"]) + 1
            continue
        values["file_count"] = int(values["file_count"]) + 1
        values["total_size_bytes"] = int(values["total_size_bytes"]) + int(
            row.size_bytes or 0
        )
        if row.media_item_id is not None:
            items = values["items"]
            assert isinstance(items, set)
            items.add(int(row.media_item_id))
            if library_kinds.get(int(row.library_id)) == "tv":
                units = values["units"]
                assert isinstance(units, set)
                units.add(
                    (
                        int(row.media_item_id),
                        int(row.season_number),
                        int(row.episode_number),
                    )
                )
        elif row.ignored_at is not None:
            values["ignored_count"] = int(values["ignored_count"]) + 1
        else:
            values["unidentified_count"] = int(values["unidentified_count"]) + 1

    refreshed_at = datetime.now(UTC).replace(tzinfo=None)
    for library_id, values in aggregates.items():
        items = values["items"]
        units = values["units"]
        assert isinstance(items, set)
        assert isinstance(units, set)
        connection.execute(
            sa.update(library)
            .where(library.c.id == library_id)
            .values(
                stats_item_count=len(items),
                stats_episode_count=len(units),
                stats_file_count=int(values["file_count"]),
                stats_total_size_bytes=int(values["total_size_bytes"]),
                stats_unidentified_count=int(values["unidentified_count"]),
                stats_missing_count=int(values["missing_count"]),
                stats_ignored_count=int(values["ignored_count"]),
                stats_refreshed_at=refreshed_at,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.drop_column("stats_refreshed_at")
        batch_op.drop_column("stats_ignored_count")
        batch_op.drop_column("stats_missing_count")
        batch_op.drop_column("stats_unidentified_count")
        batch_op.drop_column("stats_total_size_bytes")
        batch_op.drop_column("stats_file_count")
        batch_op.drop_column("stats_episode_count")
        batch_op.drop_column("stats_item_count")
