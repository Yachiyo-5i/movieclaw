"""fix chdbits publish time timezone: 北京时间归一为 UTC 并回填投递活动

CHDBits 的 NexusPHP 页面输出中国标准时间，旧解析器却把它原样写入约定为 UTC
的数据库，导致发布时间比首次索引时间晚 8 小时。迁移一次性修正种子索引和同步
游标，并把已投递活动的完整时间链冻结下来；相关预测是可重算缓存，受影响时清空。

Revision ID: d7a9c2e4f601
Revises: c6f8a1d3e245
Create Date: 2026-08-14 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "d7a9c2e4f601"
down_revision: str | None = "c6f8a1d3e245"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SITE_ID = "chdbits"
_LOCAL_OFFSET = timedelta(hours=8)


def _utc_text(value: datetime) -> str:
    """把库内 naive UTC 输出为活动 payload 使用的带时区 ISO 文本。"""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()


def _payload_dict(value: object) -> dict[str, Any] | None:
    """JSON 列正常会解码为 dict；异常历史值跳过，不能阻断应用启动迁移。"""
    return value if isinstance(value, dict) else None


def upgrade() -> None:
    bind = op.get_bind()
    torrent = sa.table(
        "site_torrent",
        sa.column("id", sa.Integer()),
        sa.column("site_id", sa.String()),
        sa.column("torrent_id", sa.String()),
        sa.column("publish_time", sa.DateTime()),
        sa.column("created_at", sa.DateTime()),
    )
    cursor = sa.table(
        "site_sync_cursor",
        sa.column("id", sa.Integer()),
        sa.column("site_id", sa.String()),
        sa.column("newest_publish_time", sa.DateTime()),
    )
    activity = sa.table(
        "subscription_activity",
        sa.column("id", sa.Integer()),
        sa.column("type", sa.String()),
        sa.column("payload", sa.JSON()),
        sa.column("created_at", sa.DateTime()),
    )
    wanted = sa.table(
        "wanted_item",
        sa.column("id", sa.Integer()),
        sa.column("release_forecast", sa.JSON()),
    )

    # 先建立 torrent_id → 修正后时间链映射，供老活动补齐冻结快照。
    torrent_rows = bind.execute(
        sa.select(
            torrent.c.id,
            torrent.c.torrent_id,
            torrent.c.publish_time,
            torrent.c.created_at,
        ).where(torrent.c.site_id == _SITE_ID)
    ).mappings()
    indexed: dict[str, tuple[datetime | None, datetime]] = {}
    chdbits_row_ids: set[int] = set()
    for row in torrent_rows:
        publish_time = row["publish_time"]
        corrected = publish_time - _LOCAL_OFFSET if publish_time is not None else None
        indexed[row["torrent_id"]] = (corrected, row["created_at"])
        chdbits_row_ids.add(row["id"])
        if corrected is not None:
            bind.execute(
                sa.update(torrent)
                .where(torrent.c.id == row["id"])
                .values(publish_time=corrected)
            )

    cursor_rows = bind.execute(
        sa.select(cursor.c.id, cursor.c.newest_publish_time).where(
            cursor.c.site_id == _SITE_ID,
            cursor.c.newest_publish_time.is_not(None),
        )
    ).mappings()
    for row in cursor_rows:
        bind.execute(
            sa.update(cursor)
            .where(cursor.c.id == row["id"])
            .values(newest_publish_time=row["newest_publish_time"] - _LOCAL_OFFSET)
        )

    activity_rows = bind.execute(
        sa.select(
            activity.c.id,
            activity.c.payload,
            activity.c.created_at,
        ).where(activity.c.type == "grabbed")
    ).mappings()
    for row in activity_rows:
        payload = _payload_dict(row["payload"])
        if payload is None or payload.get("site_id") != _SITE_ID:
            continue
        snapshot = indexed.get(str(payload.get("torrent_id", "")))
        frozen_publish = payload.get("resource_publish_time")
        if isinstance(frozen_publish, str):
            try:
                parsed = datetime.fromisoformat(frozen_publish)
            except ValueError:
                parsed = None
            if parsed is not None:
                payload["resource_publish_time"] = _utc_text(parsed - _LOCAL_OFFSET)
        elif snapshot is not None and snapshot[0] is not None:
            payload["resource_publish_time"] = _utc_text(snapshot[0])

        if not payload.get("resource_first_seen_at") and snapshot is not None:
            payload["resource_first_seen_at"] = _utc_text(snapshot[1])
        if not payload.get("submitted_at"):
            payload["submitted_at"] = _utc_text(row["created_at"])
        bind.execute(
            sa.update(activity)
            .where(activity.c.id == row["id"])
            .values(payload=payload)
        )

    # 预测快照会从 site_torrent 重算。只清理由 CHDBits 观测参与的快照，保留
    # 完全无关站点的预测与首次预测命中率基线。
    forecast_rows = bind.execute(
        sa.select(wanted.c.id, wanted.c.release_forecast).where(
            wanted.c.release_forecast.is_not(None)
        )
    ).mappings()
    for row in forecast_rows:
        forecast = _payload_dict(row["release_forecast"])
        if forecast is None:
            continue
        basis_ids = forecast.get("basis_torrent_row_ids")
        affected = isinstance(basis_ids, list) and any(
            isinstance(row_id, int) and row_id in chdbits_row_ids for row_id in basis_ids
        )
        if affected:
            bind.execute(
                sa.update(wanted)
                .where(wanted.c.id == row["id"])
                # JSON 类型默认会把 Python None 写成 JSON ``null``；这里明确落
                # SQL NULL，保持字段“尚无预测”的数据库语义。
                .values(release_forecast=sa.null())
            )


def downgrade() -> None:
    # 数据纠偏不可安全逆转：降级后新写入的 CHDBits 数据已经是正确 UTC，统一加回
    # 8 小时会再次污染数据。跨版本回退由更新前自动备份恢复，符合项目迁移约束。
    pass
