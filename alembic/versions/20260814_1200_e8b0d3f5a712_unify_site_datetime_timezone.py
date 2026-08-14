"""unify built-in site datetimes to UTC

所有内置站点历史解析值原本都是中国标准时间墙上时钟，但数据库契约是 naive UTC。
上一迁移只修正了 CHDBits 发布时间；本迁移补齐其它站点、促销截止、搜索快照与
投递活动，并清空可重算的发布时间预测缓存。

Revision ID: e8b0d3f5a712
Revises: d7a9c2e4f601
Create Date: 2026-08-14 12:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision: str = "e8b0d3f5a712"
down_revision: str | None = "d7a9c2e4f601"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHDBITS = "chdbits"
_BUILTIN_SITE_IDS = frozenset(
    {
        "agsvpt",
        "audiences",
        "chdbits",
        "hdarea",
        "hddolby",
        "hdfans",
        "hdhome",
        "hdsky",
        "hdtime",
        "hdvideo",
        "hhanclub",
        "keepfrds",
        "mteam",
        "nicept",
        "ourbits",
        "piggo",
        "pter",
        "pthome",
        "pttime",
        "soulvoice",
        "ssd",
        "tjupt",
        "ttg",
    }
)
_SITE_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _local_to_utc(value: datetime) -> datetime:
    """把旧库的站点墙上时间重新解释为 Asia/Shanghai，再转 naive UTC。"""
    local = value.replace(tzinfo=None).replace(tzinfo=_SITE_TIMEZONE)
    return local.astimezone(UTC).replace(tzinfo=None)


def _json_time_to_utc(value: object) -> datetime | None:
    """解析快照时间；带 offset 的新值尊重 offset，旧 naive 值按站点时区解释。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return _local_to_utc(parsed)
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _utc_text(value: datetime) -> str:
    """把 naive UTC 输出为跨 API/JSON 层不会歧义的带 offset ISO 文本。"""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()


def _payload_dict(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def upgrade() -> None:
    bind = op.get_bind()
    torrent = sa.table(
        "site_torrent",
        sa.column("id", sa.Integer()),
        sa.column("site_id", sa.String()),
        sa.column("torrent_id", sa.String()),
        sa.column("publish_time", sa.DateTime()),
        sa.column("free_deadline", sa.DateTime()),
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
    search_history = sa.table(
        "search_history",
        sa.column("id", sa.Integer()),
        sa.column("vertical", sa.String()),
        sa.column("snapshot_json", sa.String()),
    )
    wanted = sa.table(
        "wanted_item",
        sa.column("id", sa.Integer()),
        sa.column("release_forecast", sa.JSON()),
    )

    # 先修正索引，并建立活动/搜索快照可复用的权威时间链。
    indexed: dict[tuple[str, str], tuple[datetime | None, datetime]] = {}
    rows = bind.execute(
        sa.select(
            torrent.c.id,
            torrent.c.site_id,
            torrent.c.torrent_id,
            torrent.c.publish_time,
            torrent.c.free_deadline,
            torrent.c.created_at,
        ).where(torrent.c.site_id.in_(_BUILTIN_SITE_IDS))
    ).mappings()
    for row in rows:
        publish_time = row["publish_time"]
        # d7a9c2e4f601 已修正 CHDBits 发布时间，不能重复减 8 小时。
        if publish_time is not None and row["site_id"] != _CHDBITS:
            publish_time = _local_to_utc(publish_time)
        free_deadline = row["free_deadline"]
        if free_deadline is not None:
            free_deadline = _local_to_utc(free_deadline)
        values: dict[str, datetime] = {}
        if publish_time is not None and row["site_id"] != _CHDBITS:
            values["publish_time"] = publish_time
        if free_deadline is not None:
            values["free_deadline"] = free_deadline
        if values:
            bind.execute(
                sa.update(torrent).where(torrent.c.id == row["id"]).values(**values)
            )
        indexed[(row["site_id"], row["torrent_id"])] = (
            publish_time,
            row["created_at"],
        )

    cursor_rows = bind.execute(
        sa.select(
            cursor.c.id,
            cursor.c.site_id,
            cursor.c.newest_publish_time,
        ).where(
            cursor.c.site_id.in_(_BUILTIN_SITE_IDS),
            cursor.c.site_id != _CHDBITS,
            cursor.c.newest_publish_time.is_not(None),
        )
    ).mappings()
    for row in cursor_rows:
        bind.execute(
            sa.update(cursor)
            .where(cursor.c.id == row["id"])
            .values(newest_publish_time=_local_to_utc(row["newest_publish_time"]))
        )

    # 投递活动是用户可见的冻结证据：优先回填修正后的索引值；索引已淘汰时，
    # 非 CHD 历史值仍按旧墙上时间纠正。CHD 的活动已由上一迁移处理。
    activity_rows = bind.execute(
        sa.select(
            activity.c.id,
            activity.c.payload,
            activity.c.created_at,
        ).where(activity.c.type == "grabbed")
    ).mappings()
    for row in activity_rows:
        payload = _payload_dict(row["payload"])
        if payload is None or payload.get("site_id") not in _BUILTIN_SITE_IDS:
            continue
        site_id = str(payload["site_id"])
        torrent_id = str(payload.get("torrent_id", ""))
        snapshot = indexed.get((site_id, torrent_id))
        if snapshot is not None and snapshot[0] is not None:
            payload["resource_publish_time"] = _utc_text(snapshot[0])
        elif site_id != _CHDBITS and isinstance(payload.get("resource_publish_time"), str):
            try:
                old_publish = datetime.fromisoformat(payload["resource_publish_time"])
            except ValueError:
                old_publish = None
            if old_publish is not None:
                payload["resource_publish_time"] = _utc_text(_local_to_utc(old_publish))
        if not payload.get("resource_first_seen_at") and snapshot is not None:
            payload["resource_first_seen_at"] = _utc_text(snapshot[1])
        if not payload.get("submitted_at"):
            payload["submitted_at"] = _utc_text(row["created_at"])
        bind.execute(
            sa.update(activity).where(activity.c.id == row["id"]).values(payload=payload)
        )

    # 搜索历史是 JSON 文本而非外键快照。命中索引时用权威值覆盖；不命中时按
    # 旧站点本地时间解释。统一输出 +00:00，浏览器不会再二次套用本地时区。
    history_rows = bind.execute(
        sa.select(search_history.c.id, search_history.c.snapshot_json).where(
            search_history.c.vertical == "torrent",
            search_history.c.snapshot_json.is_not(None),
        )
    ).mappings()
    for row in history_rows:
        try:
            snapshot_json = json.loads(row["snapshot_json"])
        except (TypeError, ValueError):
            continue
        changed = False
        for item in snapshot_json.get("items", []):
            if not isinstance(item, dict) or item.get("site_id") not in _BUILTIN_SITE_IDS:
                continue
            key = (str(item["site_id"]), str(item.get("torrent_id", "")))
            indexed_time = indexed.get(key, (None, datetime.min))[0]
            upload_time = indexed_time or _json_time_to_utc(item.get("upload_time"))
            if upload_time is not None:
                item["upload_time"] = _utc_text(upload_time)
                changed = True
            free_deadline = _json_time_to_utc(item.get("free_deadline"))
            if free_deadline is not None:
                item["free_deadline"] = _utc_text(free_deadline)
                changed = True
        if changed:
            bind.execute(
                sa.update(search_history)
                .where(search_history.c.id == row["id"])
                .values(
                    snapshot_json=json.dumps(
                        snapshot_json,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            )

    # 预测是派生缓存，来源时间整体纠偏后必须重算；明确写 SQL NULL，避免 JSON null。
    bind.execute(
        sa.update(wanted)
        .where(wanted.c.release_forecast.is_not(None))
        .values(release_forecast=sa.null())
    )


def downgrade() -> None:
    # 时间纠偏不可安全逆转：降级后新写入值已经是 UTC，统一加回 8 小时会再次污染。
    # 跨版本回退由更新前自动备份恢复数据库，符合项目的向前迁移约束。
    pass
