"""播放/收藏域的 webhook 事件装配（docs/design/webhook.md §1.4）。

只负责把领域状态翻译成 ``OutboundEvent`` 的 ``data`` 结构（纯装配，不投递），
保持协议无关：客户端信息以 ``ClientInfo`` 值对象传入，由协议层从自己的
身份体系（如 Jellyfin ``RequestIdentity``）转换而来。

关键翻译规则：
- 播放事件的 ``media.type`` 只有 movie / episode 两个值，电影的内部哨兵
  单元 (0,0) 外发为 season/episode = null；
- 收藏事件的 ``media.type`` 表达收藏目标层级（movie/series/season/episode），
  由收藏哨兵单元反推（(s,-1)=整季、(-1,-1)=整剧），哨兵数值绝不外泄。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel.ext.asyncio.session import AsyncSession

from movieclaw_db.models import MediaItem, PlaybackState
from movieclaw_events import OutboundEvent, new_ulid
from movieclaw_playback.state import Unit, get_states


@dataclass(frozen=True)
class ClientInfo:
    """上报客户端信息（协议无关的值对象）。"""

    name: str = ""
    device_name: str = ""
    device_id: str = ""
    version: str = ""

    def payload(self) -> dict:
        return {
            "name": self.name or None,
            "device_name": self.device_name or None,
            "device_id": self.device_id or None,
            "version": self.version or None,
        }


def _client_payload(client: ClientInfo | None) -> dict | None:
    return client.payload() if client is not None else None


def _base_media(item: MediaItem) -> dict:
    return {
        "tmdb_id": item.tmdb_id,
        "imdb_id": item.imdb_id,
        "title": item.title,
        "original_title": item.original_title,
        "year": item.year,
    }


def _playback_media(item: MediaItem, unit: Unit) -> dict:
    """播放事件的 media：电影不外泄 (0,0) 哨兵，剧集带真实季集号。"""
    media = _base_media(item)
    if item.kind == "movie":
        media.update({"type": "movie", "season_number": None, "episode_number": None})
    else:
        media.update(
            {"type": "episode", "season_number": unit[1], "episode_number": unit[2]}
        )
    return media


def _favorite_media(item: MediaItem, unit: Unit) -> dict:
    """收藏事件的 media：哨兵单元 → 收藏目标层级。"""
    _, season, episode = unit
    media = _base_media(item)
    if (season, episode) == (-1, -1):
        level, season_out, episode_out = "series", None, None
    elif episode == -1:
        level, season_out, episode_out = "season", season, None
    elif item.kind == "movie":
        level, season_out, episode_out = "movie", None, None
    else:
        level, season_out, episode_out = "episode", season, episode
    media.update(
        {"type": level, "season_number": season_out, "episode_number": episode_out}
    )
    return media


def _playback_payload(state: PlaybackState, *, duration_ms: int | None) -> dict:
    return {
        "position_ms": state.position_ms,
        "duration_ms": duration_ms,
        "played": state.played,
        "play_count": state.play_count,
    }


async def build_playback_event(
    session: AsyncSession,
    event: str,
    unit: Unit,
    state: PlaybackState,
    *,
    duration_ms: int | None = None,
    client: ClientInfo | None = None,
) -> OutboundEvent | None:
    """装配单条播放事件（started/stopped/completed）；条目已被删时返回 None。"""
    item = await session.get(MediaItem, unit[0])
    if item is None:
        return None
    return OutboundEvent(
        event=event,
        data={
            "media": _playback_media(item, unit),
            "playback": _playback_payload(state, duration_ms=duration_ms),
            "client": _client_payload(client),
        },
    )


async def build_marked_events(
    session: AsyncSession,
    event: str,
    units: list[Unit],
    *,
    client: ClientInfo | None = None,
) -> list[OutboundEvent]:
    """装配标记已看/未看事件：级联多单元逐条发，共享 batch_id 供下游聚合。"""
    if not units:
        return []
    item = await session.get(MediaItem, units[0][0])
    if item is None:
        return []
    states = await get_states(session, [u[0] for u in units])
    batch_id = new_ulid() if len(units) > 1 else None
    events: list[OutboundEvent] = []
    for unit in units:
        state = states.get(unit)
        if state is None:
            continue
        events.append(
            OutboundEvent(
                event=event,
                batch_id=batch_id,
                data={
                    "media": _playback_media(item, unit),
                    "playback": _playback_payload(state, duration_ms=None),
                    "client": _client_payload(client),
                },
            )
        )
    return events


async def build_favorite_event(
    session: AsyncSession,
    unit: Unit,
    *,
    favorite: bool,
    client: ClientInfo | None = None,
) -> OutboundEvent | None:
    """装配收藏/取消收藏事件；条目已被删时返回 None。"""
    item = await session.get(MediaItem, unit[0])
    if item is None:
        return None
    return OutboundEvent(
        event="item.favorited" if favorite else "item.unfavorited",
        data={
            "media": _favorite_media(item, unit),
            "favorite": favorite,
            "client": _client_payload(client),
        },
    )
