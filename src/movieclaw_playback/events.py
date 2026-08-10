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

from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from movieclaw_db.models import MediaEpisode, MediaItem, PlaybackState
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
        # item_id 是 MovieClaw 内部条目 ID：Jellyfin 兼容格式用它构造 ItemId，
        # 自有协议消费端可拿它回调 Jellyfin 兼容 API 查详情
        "item_id": item.id,
        "tmdb_id": item.tmdb_id,
        "imdb_id": item.imdb_id,
        "title": item.title,
        "original_title": item.original_title,
        "year": item.year,
    }


async def _episode_titles(
    session: AsyncSession, item_id: int, units: list[Unit]
) -> dict[tuple[int, int], str]:
    """批量取各 (season, episode) 的单集标题；查不到的键缺席。"""
    pairs = {(u[1], u[2]) for u in units}
    if not pairs:
        return {}
    rows = (
        await session.execute(
            select(
                MediaEpisode.season_number, MediaEpisode.episode_number, MediaEpisode.name
            ).where(MediaEpisode.media_item_id == item_id)
        )
    ).all()
    return {(s, e): name for s, e, name in rows if (s, e) in pairs and name}


def _playback_media(
    item: MediaItem, unit: Unit, episode_title: str | None = None
) -> dict:
    """播放事件的 media：电影不外泄 (0,0) 哨兵，剧集带真实季集号。"""
    media = _base_media(item)
    if item.kind == "movie":
        media.update(
            {
                "type": "movie",
                "season_number": None,
                "episode_number": None,
                "episode_title": None,
            }
        )
    else:
        media.update(
            {
                "type": "episode",
                "season_number": unit[1],
                "episode_number": unit[2],
                "episode_title": episode_title,
            }
        )
    return media


def _favorite_media(
    item: MediaItem, unit: Unit, episode_title: str | None = None
) -> dict:
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
        {
            "type": level,
            "season_number": season_out,
            "episode_number": episode_out,
            "episode_title": episode_title if level == "episode" else None,
        }
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
    titles = (
        await _episode_titles(session, unit[0], [unit]) if item.kind != "movie" else {}
    )
    return OutboundEvent(
        event=event,
        data={
            "media": _playback_media(item, unit, titles.get((unit[1], unit[2]))),
            "playback": _playback_payload(state, duration_ms=duration_ms),
            "client": _client_payload(client),
        },
    )


async def build_marked_events(
    session: AsyncSession,
    event: str,
    units: list[Unit],
    *,
    member_id: int,
    client: ClientInfo | None = None,
) -> list[OutboundEvent]:
    """装配标记已看/未看事件：级联多单元逐条发，共享 batch_id 供下游聚合。"""
    if not units:
        return []
    item = await session.get(MediaItem, units[0][0])
    if item is None:
        return []
    states = await get_states(session, [u[0] for u in units], member_id=member_id)
    titles = (
        await _episode_titles(session, units[0][0], units) if item.kind != "movie" else {}
    )
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
                    "media": _playback_media(item, unit, titles.get((unit[1], unit[2]))),
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
    is_episode_level = item.kind != "movie" and unit[1] >= 0 and unit[2] >= 0
    titles = (
        await _episode_titles(session, unit[0], [unit]) if is_episode_level else {}
    )
    return OutboundEvent(
        event="item.favorited" if favorite else "item.unfavorited",
        data={
            "media": _favorite_media(item, unit, titles.get((unit[1], unit[2]))),
            "favorite": favorite,
            "client": _client_payload(client),
        },
    )
