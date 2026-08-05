"""Webhook 事件目录：全系统可外发事件的唯一注册表（docs/design/webhook.md §1.1）。

目录同时服务两端：
- 投递器按它校验事件名（未登记的事件直接拒发，防拼写错误静默丢失）；
- 管理 API 把它下发给前端渲染「订阅事件」多选（分组 + Jellyfin 可订阅性），
  新领域上线后前端零改动。

``jellyfin_type`` 为 None 表示该事件没有 Jellyfin NotificationType 对应物，
jellyfin 格式的 endpoint 不可订阅它（设置页禁用勾选）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventSpec:
    event: str
    """事件名：``<domain>.<action>``。"""

    group: str
    """设置页多选的分组标题。"""

    label: str
    """面向用户的中文说明。"""

    jellyfin_type: str | None = None
    """对应的 Jellyfin Webhook 插件 NotificationType；None = 无对应物。"""

    default_on: bool = True
    """新建 endpoint 时是否默认勾选（高频事件如 progress 默认关闭）。"""


EVENT_CATALOG: list[EventSpec] = [
    EventSpec("playback.started", "播放", "开始播放", "PlaybackStart"),
    EventSpec("playback.stopped", "播放", "停止播放", "PlaybackStop"),
    EventSpec("playback.completed", "播放", "看完（达到已看阈值）", "PlaybackStop"),
    EventSpec(
        "playback.progress",
        "播放",
        "播放进度（每单元最多 30 秒一条，高频）",
        "PlaybackProgress",
        default_on=False,
    ),
    EventSpec("playback.marked_played", "播放", "标记已看", "UserDataSaved"),
    EventSpec("playback.marked_unplayed", "播放", "取消已看", "UserDataSaved"),
    EventSpec("item.favorited", "收藏", "收藏", "UserDataSaved"),
    EventSpec("item.unfavorited", "收藏", "取消收藏", "UserDataSaved"),
    # 订阅域没有 Jellyfin 对应物（jellyfin_type=None），仅自有协议可订阅
    EventSpec("subscription.download_started", "订阅", "订阅命中并开始下载", None),
    EventSpec("subscription.fulfilled", "订阅", "订阅内容已入库", None),
]

_KNOWN_EVENTS = {spec.event for spec in EVENT_CATALOG}


def is_known_event(event: str) -> bool:
    return event in _KNOWN_EVENTS


def catalog_view() -> list[dict]:
    """管理 API 下发给前端的目录形态。"""
    return [
        {
            "event": spec.event,
            "group": spec.group,
            "label": spec.label,
            "jellyfin_supported": spec.jellyfin_type is not None,
            "default_on": spec.default_on,
        }
        for spec in EVENT_CATALOG
    ]
