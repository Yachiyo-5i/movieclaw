"""播放进度回报与已看/收藏标记（设计文档 §7）。

落库语义（v1.1 按源码修正版）：
- play_count+1 与 last_played_at 在 /Sessions/Playing（开始）时更新；
- Progress 与 Stopped 跑同一套阈值三分支（movieclaw_playback.progress）；
- Failed=true 的 Stopped 完全跳过落库；
- UserPlayedItems：datePlayed 才 +1，否则 max(count,1)；DELETE 全清零；
- 作用于 Series/Season GUID 时级联全部有文件的子单元。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from movieclaw_api.services.webhook import emit_events
from movieclaw_db.engine import get_database
from movieclaw_db.models import LibraryFile, MediaEpisode, MediaItem, MediaMetadata
from movieclaw_jellyfin.catalog import (
    TICKS_PER_MS,
    _folder_user_data,
    _leaf_user_data,
    load_bundles,
)
from movieclaw_jellyfin.errors import bad_request_text, not_found
from movieclaw_jellyfin.ids import EntityKind, EntityRef, decode_guid
from movieclaw_jellyfin.security import RequestIdentity, require_device
from movieclaw_playback import state as playback_state
from movieclaw_playback.events import (
    ClientInfo,
    build_favorite_event,
    build_marked_events,
    build_playback_event,
)
from movieclaw_playback.streaming import stop_device_streams

router = APIRouter(dependencies=[Depends(require_device)])


def _client_info(identity: RequestIdentity) -> ClientInfo:
    """协议身份 → 协议无关的客户端信息（webhook 事件的 client 字段）。"""
    device = identity.device
    return ClientInfo(
        name=device.client or "",
        device_name=device.device_name or "",
        device_id=device.device_id or "",
        version=device.version or "",
    )


async def _read_body(request: Request) -> dict[str, Any]:
    """宽容读取 JSON body：键名小写化、未知字段忽略、坏 JSON 当空。"""
    try:
        body = await request.json()
    except Exception:
        return {}
    if not isinstance(body, dict):
        return {}
    return {str(k).lower(): v for k, v in body.items()}


def _position_ms(body: dict[str, Any], query_ticks: str | None = None) -> int | None:
    """None = 客户端没报位置（领域层按"播到结尾"处理）；0 = 拖回开头。"""
    raw = body.get("positionticks")
    if raw is None and query_ticks is not None:
        raw = query_ticks
    if raw is None:
        return None
    try:
        ticks = int(raw)
    except (TypeError, ValueError):
        return None
    return max(0, ticks // TICKS_PER_MS)


async def _resolve_units(ref: EntityRef) -> list[playback_state.Unit]:
    """GUID → 受影响的 (item, season, episode) 单元列表（文件夹级联）。"""
    async with get_database().session() as session:
        q = select(LibraryFile.season_number, LibraryFile.episode_number).where(
            LibraryFile.media_item_id == ref.entity_id,
            LibraryFile.missing_since.is_(None),
        )
        rows = list((await session.execute(q)).all())
    units = sorted({(ref.entity_id, s, e) for s, e in rows})
    if ref.kind == EntityKind.EPISODE:
        return [(ref.entity_id, ref.season, ref.episode)]
    if ref.kind == EntityKind.SEASON:
        return [u for u in units if u[1] == ref.season]
    if ref.kind == EntityKind.ITEM:
        # 电影 = (0,0) 单元；剧 = 全部集
        return units or [(ref.entity_id, 0, 0)]
    return []


async def _unit_runtime_ms(unit: playback_state.Unit) -> int | None:
    item_id, season, episode = unit
    async with get_database().session() as session:
        files = list(
            (
                await session.execute(
                    select(LibraryFile).where(
                        LibraryFile.media_item_id == item_id,
                        LibraryFile.season_number == season,
                        LibraryFile.episode_number == episode,
                        LibraryFile.missing_since.is_(None),
                    )
                )
            ).scalars()
        )
        for f in files:
            if f.duration_seconds:
                return f.duration_seconds * 1000
        ep = (
            await session.execute(
                select(MediaEpisode).where(
                    MediaEpisode.media_item_id == item_id,
                    MediaEpisode.season_number == season,
                    MediaEpisode.episode_number == episode,
                )
            )
        ).scalar_one_or_none()
        if ep and ep.runtime_minutes:
            return ep.runtime_minutes * 60_000
        meta = (
            await session.execute(
                select(MediaMetadata).where(MediaMetadata.media_item_id == item_id)
            )
        ).scalar_one_or_none()
        if meta and meta.runtime_minutes:
            return meta.runtime_minutes * 60_000
    return None


def _leaf_unit(ref: EntityRef) -> playback_state.Unit:
    if ref.kind == EntityKind.EPISODE:
        return (ref.entity_id, ref.season, ref.episode)
    return (ref.entity_id, 0, 0)


async def _favorite_unit(ref: EntityRef) -> playback_state.Unit:
    """收藏的落点单元：叶子用真实单元；Season/Series 用哨兵（-1）——
    与 catalog._folder_user_data 的读取侧约定一致，绝不污染 S00E00。"""
    if ref.kind == EntityKind.EPISODE:
        return (ref.entity_id, ref.season, ref.episode)
    if ref.kind == EntityKind.SEASON:
        return (ref.entity_id, ref.season, -1)
    async with get_database().session() as session:
        item = await session.get(MediaItem, ref.entity_id)
    if item is not None and item.kind == "tv":
        return (ref.entity_id, -1, -1)
    return (ref.entity_id, 0, 0)


def _decode_item_ref(raw: Any) -> EntityRef | None:
    if not raw:
        return None
    ref = decode_guid(str(raw))
    if ref is None or ref.kind not in (
        EntityKind.ITEM,
        EntityKind.SEASON,
        EntityKind.EPISODE,
    ):
        return None
    return ref


# ---------------------------------------------------------------------------
# Sessions/Playing*
# ---------------------------------------------------------------------------


async def _record_start(ref: EntityRef, client: ClientInfo) -> None:
    """开始播放落库 + commit 后装配/投递 ``playback.started`` 事件。"""
    unit = _leaf_unit(ref)
    async with get_database().session() as session:
        row = await playback_state.record_playback_start(session, unit)
        await session.commit()
        event = await build_playback_event(
            session, "playback.started", unit, row, client=client
        )
    if event is not None:
        emit_events([event])


@router.post("/Sessions/Playing", status_code=204)
async def playing_start(
    request: Request, identity: RequestIdentity = Depends(require_device)
) -> Response:
    body = await _read_body(request)
    ref = _decode_item_ref(body.get("itemid"))
    if ref is not None:
        await _record_start(ref, _client_info(identity))
    return Response(status_code=204)


#: playback.progress 事件的节流：每单元最多 30 秒一条（避免播放期间刷屏，
#: 设计文档 §1.1）。键是 (item, season, episode)，量级 = 库内在播单元数
_PROGRESS_EMIT_INTERVAL = 30.0
_progress_last_emit: dict[playback_state.Unit, float] = {}


def _progress_throttled(unit: playback_state.Unit) -> bool:
    """True = 本次进度不发事件；未被节流时顺带记下本次时间。"""
    now = time.monotonic()
    last = _progress_last_emit.get(unit)
    if last is not None and now - last < _PROGRESS_EMIT_INTERVAL:
        return True
    _progress_last_emit[unit] = now
    return False


async def _apply_progress(
    ref: EntityRef,
    position_ms: int | None,
    *,
    stopped: bool = False,
    client: ClientInfo | None = None,
) -> None:
    """进度落库；commit 后按语义装配 webhook 事件：
    Stopped 上报发 ``playback.stopped``；played 本次翻转为 True 追加
    ``playback.completed``（Progress 与 Stopped 都可能触发翻转）；
    普通进度上报按单元节流发 ``playback.progress``。"""
    unit = _leaf_unit(ref)
    runtime_ms = await _unit_runtime_ms(unit)
    async with get_database().session() as session:
        row, newly_played = await playback_state.record_playback_progress(
            session, unit, position_ms=position_ms, runtime_ms=runtime_ms
        )
        await session.commit()
        emit_progress = (
            not stopped and not newly_played and not _progress_throttled(unit)
        )
        events = []
        for name, hit in (
            ("playback.stopped", stopped),
            ("playback.completed", newly_played),
            ("playback.progress", emit_progress),
        ):
            if not hit:
                continue
            event = await build_playback_event(
                session, name, unit, row, duration_ms=runtime_ms, client=client
            )
            if event is not None:
                events.append(event)
    emit_events(events)


@router.post("/Sessions/Playing/Progress", status_code=204)
async def playing_progress(
    request: Request, identity: RequestIdentity = Depends(require_device)
) -> Response:
    body = await _read_body(request)
    ref = _decode_item_ref(body.get("itemid"))
    if ref is not None:
        position = _position_ms(body)
        # Progress 不带位置的心跳（如暂停事件）不落库；报 0 = 拖回开头要落
        if position is not None:
            await _apply_progress(ref, position, client=_client_info(identity))
    return Response(status_code=204)


@router.post("/Sessions/Playing/Stopped", status_code=204)
async def playing_stopped(
    request: Request, identity: RequestIdentity = Depends(require_device)
) -> Response:
    body = await _read_body(request)
    # VidHub 的 Stopped 不代表它已立即关闭此前发出的 Range 请求。先取消同设备
    # 的活跃流，避免机械盘继续为已退出的播放器预读；失败停止同样必须收口。
    stop_device_streams(identity.device.device_id)
    if body.get("failed") is True:
        # 播放失败的上报不落库（SessionManager.cs:1164-1167）
        return Response(status_code=204)
    ref = _decode_item_ref(body.get("itemid"))
    if ref is not None:
        await _apply_progress(
            ref, _position_ms(body), stopped=True, client=_client_info(identity)
        )
    return Response(status_code=204)


@router.post("/Sessions/Playing/Ping", status_code=204)
async def playing_ping(request: Request) -> Response:
    if "playSessionId" not in request.query_params:
        raise bad_request_text()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# legacy /PlayingItems（P2 兜底，参数走 query；停止是 DELETE）
# ---------------------------------------------------------------------------


@router.post("/PlayingItems/{item_id}", status_code=204)
@router.post("/Users/{user_id}/PlayingItems/{item_id}", status_code=204)
async def playing_start_legacy(
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> Response:
    ref = _decode_item_ref(item_id)
    if ref is not None:
        await _record_start(ref, _client_info(identity))
    return Response(status_code=204)


@router.post("/PlayingItems/{item_id}/Progress", status_code=204)
@router.post("/Users/{user_id}/PlayingItems/{item_id}/Progress", status_code=204)
async def playing_progress_legacy(
    request: Request,
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> Response:
    ref = _decode_item_ref(item_id)
    if ref is not None:
        position = _position_ms({}, request.query_params.get("positionTicks"))
        if position is not None:
            await _apply_progress(ref, position, client=_client_info(identity))
    return Response(status_code=204)


@router.delete("/PlayingItems/{item_id}", status_code=204)
@router.delete("/Users/{user_id}/PlayingItems/{item_id}", status_code=204)
async def playing_stopped_legacy(
    request: Request,
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> Response:
    ref = _decode_item_ref(item_id)
    stop_device_streams(identity.device.device_id)
    if ref is not None:
        await _apply_progress(
            ref,
            _position_ms({}, request.query_params.get("positionTicks")),
            stopped=True,
            client=_client_info(identity),
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 已看 / 收藏（200 + UserItemDataDto）
# ---------------------------------------------------------------------------


async def _user_data_response(ref: EntityRef, guid_raw: str) -> JSONResponse:
    """标记类接口的 200 响应体：与浏览接口同一套 UserData 公式，
    文件夹（Series/Season）给聚合形态（含 UnplayedItemCount），客户端
    据此原地刷新角标，不必重拉列表。"""
    guid = guid_raw.lower().replace("-", "")
    async with get_database().session() as session:
        bundles = await load_bundles(session, [ref.entity_id])
    bundle = bundles.get(ref.entity_id)
    if bundle is None:
        return JSONResponse(
            {
                "PlaybackPositionTicks": 0,
                "PlayCount": 0,
                "IsFavorite": False,
                "Played": False,
                "Key": guid,
                "ItemId": guid,
            }
        )
    if ref.kind == EntityKind.EPISODE:
        return JSONResponse(_leaf_user_data(bundle, ref.season, ref.episode, guid))
    if ref.kind == EntityKind.SEASON:
        return JSONResponse(_folder_user_data(bundle, guid, season=ref.season))
    if bundle.item.kind == "movie":
        return JSONResponse(_leaf_user_data(bundle, 0, 0, guid))
    return JSONResponse(_folder_user_data(bundle, guid))


def _parse_date_played(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


@router.post("/UserPlayedItems/{item_id}")
@router.post("/Users/{user_id}/PlayedItems/{item_id}")
async def mark_played(
    request: Request,
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> JSONResponse:
    ref = _decode_item_ref(item_id)
    if ref is None:
        raise not_found()
    units = await _resolve_units(ref)
    if not units:
        raise not_found()
    date_played = _parse_date_played(request.query_params.get("datePlayed"))
    async with get_database().session() as session:
        await playback_state.mark_played(session, units, date_played=date_played)
        await session.commit()
        events = await build_marked_events(
            session, "playback.marked_played", units, client=_client_info(identity)
        )
    emit_events(events)
    return await _user_data_response(ref, item_id)


@router.delete("/UserPlayedItems/{item_id}")
@router.delete("/Users/{user_id}/PlayedItems/{item_id}")
async def mark_unplayed(
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> JSONResponse:
    ref = _decode_item_ref(item_id)
    if ref is None:
        raise not_found()
    units = await _resolve_units(ref)
    if not units:
        raise not_found()
    async with get_database().session() as session:
        await playback_state.mark_unplayed(session, units)
        await session.commit()
        events = await build_marked_events(
            session, "playback.marked_unplayed", units, client=_client_info(identity)
        )
    emit_events(events)
    return await _user_data_response(ref, item_id)


async def _set_favorite(
    ref: EntityRef, *, favorite: bool, client: ClientInfo
) -> None:
    """收藏落库 + commit 后装配/投递 ``item.(un)favorited`` 事件。"""
    unit = await _favorite_unit(ref)
    async with get_database().session() as session:
        await playback_state.set_favorite(session, unit, favorite=favorite)
        await session.commit()
        event = await build_favorite_event(
            session, unit, favorite=favorite, client=client
        )
    if event is not None:
        emit_events([event])


@router.post("/UserFavoriteItems/{item_id}")
@router.post("/Users/{user_id}/FavoriteItems/{item_id}")
async def mark_favorite(
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> JSONResponse:
    ref = _decode_item_ref(item_id)
    if ref is None:
        raise not_found()
    await _set_favorite(ref, favorite=True, client=_client_info(identity))
    return await _user_data_response(ref, item_id)


@router.delete("/UserFavoriteItems/{item_id}")
@router.delete("/Users/{user_id}/FavoriteItems/{item_id}")
async def unmark_favorite(
    item_id: str,
    user_id: str | None = None,
    identity: RequestIdentity = Depends(require_device),
) -> JSONResponse:
    ref = _decode_item_ref(item_id)
    if ref is None:
        raise not_found()
    await _set_favorite(ref, favorite=False, client=_client_info(identity))
    return await _user_data_response(ref, item_id)
