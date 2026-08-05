"""Jellyfin 兼容格式器（docs/design/webhook.md §3）。

兼容等式：MovieClaw 提供与 jellyfin-plugin-webhook 同名的变量字典 + 渲染
用户从下游文档粘贴的同一段 Handlebars 模板。变量子集按插件文档实现
（对齐插件 master @ 2026-08，见设计文档 §3.3），缺失项渲染为空串。

关键映射（§3.2）：
- ``playback.completed`` → ``PlaybackStop`` + ``PlayedToCompletion=True``
  （Jellyfin 没有独立的 completed 事件）；
- ``ItemId`` / ``SeriesId`` 与 Jellyfin 兼容 API 对外的条目 ID **同源**
  （movieclaw_jellyfin.ids 结构化 GUID），下游可回调兼容 API 查详情拉海报；
- ticks 换算只发生在这里：``position_ms × 10000``，与入站方向对称。
"""

from __future__ import annotations

from datetime import UTC

from movieclaw_events import OutboundEvent
from movieclaw_jellyfin.ids import episode_guid, item_guid, season_guid, user_guid

TICKS_PER_MS = 10_000

#: MovieClaw 事件 → Jellyfin NotificationType；不在表内的事件对
#: jellyfin 格式 endpoint 不可达（目录层已禁止订阅，这里兜底跳过）
NOTIFICATION_TYPES: dict[str, str] = {
    "playback.started": "PlaybackStart",
    "playback.stopped": "PlaybackStop",
    "playback.completed": "PlaybackStop",
    "playback.progress": "PlaybackProgress",
    "playback.marked_played": "UserDataSaved",
    "playback.marked_unplayed": "UserDataSaved",
    "item.favorited": "UserDataSaved",
    "item.unfavorited": "UserDataSaved",
    "webhook.test": "PlaybackStop",  # 测试事件按最常见的模板分支渲染
}

_ITEM_TYPES = {"movie": "Movie", "episode": "Episode", "season": "Season", "series": "Series"}


def _hhmmss(ms: int | None) -> str:
    if not ms or ms < 0:
        return "00:00:00"
    seconds = ms // 1000
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _item_guid_for(media: dict) -> str:
    item_id = media.get("item_id") or 0
    kind = media.get("type")
    if kind == "season":
        return season_guid(item_id, media.get("season_number") or 0)
    if kind == "episode":
        return episode_guid(
            item_id, media.get("season_number") or 0, media.get("episode_number") or 0
        )
    return item_guid(item_id)


def jellyfin_variables(event: OutboundEvent, server: dict) -> dict | None:
    """OutboundEvent → 插件同名变量字典；无映射的事件返回 None（跳过投递）。

    ``server`` 需含 id/name/version/url/username（由 dispatcher 的
    ``_server_info`` 提供）。
    """
    notification_type = NOTIFICATION_TYPES.get(event.event)
    if notification_type is None:
        return None

    media = event.data.get("media") or {}
    playback = event.data.get("playback") or {}
    client = event.data.get("client") or {}
    occurred = event.occurred_at

    values: dict = {
        # Server（全体通知可用）
        "ServerId": server.get("id", ""),
        "ServerName": server.get("name", ""),
        "ServerVersion": server.get("version", ""),
        "ServerUrl": server.get("url", ""),
        "NotificationType": notification_type,
        # User（单管理员形态：兼容层虚拟用户）
        "UserId": user_guid(),
        "NotificationUsername": server.get("username", ""),
        "Username": server.get("username", ""),
        # Device & Client
        "DeviceName": client.get("device_name") or "",
        "DeviceId": client.get("device_id") or "",
        "ClientName": client.get("name") or "",
        "Client": client.get("name") or "",
        # BaseItem
        "Timestamp": occurred.astimezone().isoformat(),
        "UtcTimestamp": occurred.astimezone(UTC).isoformat(),
        "ItemId": _item_guid_for(media),
        "ItemType": _ITEM_TYPES.get(media.get("type") or "", ""),
        "Name": media.get("episode_title") or media.get("title") or "",
        "Overview": "",
        "Year": media.get("year") or "",
        "Provider_tmdb": media.get("tmdb_id") or "",
        "Provider_imdb": media.get("imdb_id") or "",
        "RunTime": _hhmmss(playback.get("duration_ms")),
        "RunTimeTicks": (playback.get("duration_ms") or 0) * TICKS_PER_MS,
        # Playback / UserData
        "PlaybackPosition": _hhmmss(playback.get("position_ms")),
        "PlaybackPositionTicks": (playback.get("position_ms") or 0) * TICKS_PER_MS,
        "PlayedToCompletion": event.event in ("playback.completed", "webhook.test"),
        "Played": bool(playback.get("played")),
        "PlayCount": playback.get("play_count") or 0,
        "Favorite": bool(event.data.get("favorite")),
        "IsPaused": False,
    }

    if media.get("type") == "episode":
        season = media.get("season_number") or 0
        episode = media.get("episode_number") or 0
        values.update(
            {
                "SeriesName": media.get("title") or "",
                "SeriesId": item_guid(media.get("item_id") or 0),
                "SeasonNumber": season,
                "SeasonNumber00": f"{season:02d}",
                "SeasonNumber000": f"{season:03d}",
                "EpisodeNumber": episode,
                "EpisodeNumber00": f"{episode:02d}",
                "EpisodeNumber000": f"{episode:03d}",
            }
        )
    return values


def _unit_key(event: OutboundEvent) -> tuple:
    media = event.data.get("media") or {}
    return (
        media.get("item_id"),
        media.get("season_number"),
        media.get("episode_number"),
    )


def merge_for_jellyfin(events: list[OutboundEvent]) -> list[OutboundEvent]:
    """去重规则（§3.2）：同批出现**同一单元**的 stopped + completed 时，
    completed 吸收 stopped——两者都映射到 PlaybackStop，双发会让下游收到
    重复事件。按单元比对：其他条目的 stopped 不受牵连。"""
    completed_units = {
        _unit_key(e) for e in events if e.event == "playback.completed"
    }
    if not completed_units:
        return events
    return [
        e
        for e in events
        if not (e.event == "playback.stopped" and _unit_key(e) in completed_units)
    ]


def format_jellyfin(
    event: OutboundEvent, *, server: dict, template: str
) -> tuple[bytes, dict[str, str]]:
    """渲染用户模板为请求体。模板语法错误抛 ``TemplateError``（进投递记录）。"""
    from movieclaw_api.services.webhook.handlebars import render

    values = jellyfin_variables(event, server)
    if values is None:
        raise ValueError(
            f"事件 {event.event} 没有 Jellyfin 对应物，不能投递到 jellyfin 格式 endpoint"
        )
    body = render(template, values).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": f"MovieClaw/{server.get('version', '')}",
    }
    return body, headers
