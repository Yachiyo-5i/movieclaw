"""媒体库首页“最近观看”的业务查询。

Jellyfin 只负责把播放事实写进 ``playback_state``；首页直接读取领域表，
不反向调用 Jellyfin 协议接口。查询同时收紧到当前账号可见、文件仍在位的
媒体库，避免权限变更或文件丢失后继续泄露历史条目。
"""

from __future__ import annotations

from sqlalchemy import and_, case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.playback import RecentWatchItemView
from movieclaw_api.services.media_scrape import asset_version
from movieclaw_db.models import (
    Library,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaMetadata,
    PlaybackState,
)
from movieclaw_media.models import MediaKind


def _runtime_ms(
    file_duration_seconds: int | None,
    episode_runtime_minutes: int | None,
    item_runtime_minutes: int | None,
) -> int | None:
    """时长优先取真实文件，其次分集档案，最后退回条目常规片长。"""
    if file_duration_seconds and file_duration_seconds > 0:
        return file_duration_seconds * 1000
    runtime_minutes = episode_runtime_minutes or item_runtime_minutes
    return runtime_minutes * 60_000 if runtime_minutes and runtime_minutes > 0 else None


def _progress_percent(position_ms: int, duration_ms: int | None) -> int | None:
    """生成卡片进度；保留 1%~99%，完成态由 ``played`` 单独表达。"""
    if position_ms <= 0 or not duration_ms:
        return None
    return max(1, min(99, round(position_ms * 100 / duration_ms)))


async def recent_watch_items(
    session: AsyncSession,
    *,
    member_id: int,
    visible_library_ids: set[int] | None,
    limit: int,
) -> list[RecentWatchItemView]:
    """返回一个账号最近观看的作品，同一剧只保留最后活动的那一集。

    ``playback_state`` 不记录从哪个库/版本播放；同一作品跨库存在时按媒体库
    首页的展示顺序选择第一个可见库，保证卡片有稳定、可访问的详情落点。
    """
    if visible_library_ids == set():
        return []

    # 先在数据库内按作品选出最近状态，避免连看一整季时把同一部剧重复铺满。
    ranked_states = (
        select(
            PlaybackState.id.label("state_id"),
            func.row_number()
            .over(
                partition_by=PlaybackState.media_item_id,
                order_by=(PlaybackState.last_played_at.desc(), PlaybackState.id.desc()),
            )
            .label("item_rank"),
        )
        .where(
            PlaybackState.member_id == member_id,
            PlaybackState.last_played_at.is_not(None),  # type: ignore[union-attr]
            PlaybackState.season_number >= 0,
            PlaybackState.episode_number >= 0,
        )
        .subquery()
    )

    # 同一集可能同时存在 1080p / 2160p 等多个版本；提醒统计按季集单元聚合，
    # 并使用该单元**第一次**进入本库的时间，避免播放后洗版被误报成“新入库 1 集”。
    # available 用全部版本判断当前是否至少还有一份在位，已全部缺失的集不提醒。
    library_episode_units = (
        select(
            LibraryFile.library_id.label("library_id"),
            LibraryFile.media_item_id.label("media_item_id"),
            LibraryFile.season_number.label("season_number"),
            LibraryFile.episode_number.label("episode_number"),
            func.min(LibraryFile.created_at).label("first_added_at"),
            func.max(
                case((LibraryFile.in_place(), 1), else_=0)  # type: ignore[union-attr]
            ).label("available"),
        )
        .where(LibraryFile.media_item_id.is_not(None))  # type: ignore[union-attr]
        .group_by(
            LibraryFile.library_id,
            LibraryFile.media_item_id,
            LibraryFile.season_number,
            LibraryFile.episode_number,
        )
        .subquery()
    )
    new_episode_count = (
        select(func.count())
        .select_from(library_episode_units)
        .where(
            library_episode_units.c.library_id == Library.id,
            library_episode_units.c.media_item_id == PlaybackState.media_item_id,
            library_episode_units.c.available == 1,
            library_episode_units.c.first_added_at > PlaybackState.last_played_at,
        )
        .correlate(Library, PlaybackState)
        .scalar_subquery()
        .label("new_episode_count")
    )

    statement = (
        select(
            PlaybackState,
            MediaItem,
            Library.id,
            MediaMetadata.poster_file,
            MediaMetadata.backdrop_file,
            MediaMetadata.runtime_minutes,
            MediaEpisode.name,
            MediaEpisode.runtime_minutes,
            MediaEpisode.still_file,
            MediaEpisode.still_path,
            func.max(LibraryFile.duration_seconds).label("file_duration_seconds"),
            new_episode_count,
        )
        .join(ranked_states, ranked_states.c.state_id == PlaybackState.id)
        .join(MediaItem, MediaItem.id == PlaybackState.media_item_id)
        .join(
            LibraryFile,
            and_(
                LibraryFile.media_item_id == PlaybackState.media_item_id,
                LibraryFile.season_number == PlaybackState.season_number,
                LibraryFile.episode_number == PlaybackState.episode_number,
                LibraryFile.in_place(),
            ),
        )
        .join(Library, Library.id == LibraryFile.library_id)
        .outerjoin(MediaMetadata, MediaMetadata.media_item_id == MediaItem.id)
        .outerjoin(
            MediaEpisode,
            and_(
                MediaEpisode.media_item_id == PlaybackState.media_item_id,
                MediaEpisode.season_number == PlaybackState.season_number,
                MediaEpisode.episode_number == PlaybackState.episode_number,
            ),
        )
        .where(ranked_states.c.item_rank == 1)
        .group_by(
            PlaybackState.id,
            MediaItem.id,
            Library.id,
            MediaMetadata.id,
            MediaEpisode.id,
        )
        .order_by(
            PlaybackState.last_played_at.desc(),
            MediaItem.id.asc(),
            Library.sort_order.asc(),
            Library.id.asc(),
        )
    )
    if visible_library_ids is not None:
        statement = statement.where(Library.id.in_(visible_library_ids))  # type: ignore[attr-defined]

    rows = (await session.execute(statement)).all()
    image_base = get_settings().tmdb_image_base_url.rstrip("/")
    result: list[RecentWatchItemView] = []
    seen_items: set[int] = set()
    for (
        state,
        item,
        library_id,
        poster_file,
        backdrop_file,
        item_runtime_minutes,
        episode_name,
        episode_runtime_minutes,
        episode_still_file,
        episode_still_path,
        file_duration_seconds,
        added_episode_count,
    ) in rows:
        if item.id is None or item.id in seen_items or state.last_played_at is None:
            continue
        seen_items.add(item.id)
        if poster_file:
            poster_url = f"/images/assets/{poster_file}?v={asset_version(poster_file)}"
        else:
            poster_url = f"{image_base}/w500{item.poster_path}" if item.poster_path else None
        if backdrop_file:
            backdrop_url = f"/images/assets/{backdrop_file}?v={asset_version(backdrop_file)}"
        else:
            backdrop_url = f"{image_base}/w780{item.backdrop_path}" if item.backdrop_path else None
        episode_still_url = None
        if item.kind == "tv":
            if episode_still_file:
                episode_still_url = (
                    f"/images/assets/{episode_still_file}?v={asset_version(episode_still_file)}"
                )
            elif episode_still_path:
                episode_still_url = f"{image_base}/w500{episode_still_path}"
        duration_ms = _runtime_ms(
            file_duration_seconds,
            episode_runtime_minutes if item.kind == "tv" else None,
            item_runtime_minutes,
        )
        result.append(
            RecentWatchItemView(
                media_item_id=item.id,
                library_id=library_id,
                kind=MediaKind(item.kind),
                title=item.title,
                year=item.year,
                poster_url=poster_url,
                backdrop_url=backdrop_url,
                episode_still_url=episode_still_url,
                season_number=state.season_number,
                episode_number=state.episode_number,
                episode_title=episode_name or None,
                new_episode_count=int(added_episode_count or 0) if item.kind == "tv" else 0,
                position_ms=state.position_ms,
                duration_ms=duration_ms,
                progress_percent=_progress_percent(state.position_ms, duration_ms),
                played=state.played,
                play_count=state.play_count,
                last_played_at=state.last_played_at,
            )
        )
        if len(result) >= limit:
            break
    return result
