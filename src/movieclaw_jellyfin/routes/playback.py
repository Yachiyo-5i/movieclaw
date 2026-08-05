"""播放链路（设计文档 §6）：PlaybackInfo 与取流。

- PlaybackInfo：不解析 DeviceProfile，恒返回未经设备适配的 MediaSources
  （等价于"无转码权限的 Jellyfin"，协议合法）；
- /Videos/{id}/stream：本地文件走 FileResponse（原生 Range/206/HEAD）；
  strm 条目读内容后 302 到云端直链，不代理（零网盘流量）。
  鉴权：真 Jellyfin 此接口匿名，我们要求 token（偏离③，公网暴露考量）。
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from movieclaw_db.engine import get_database
from movieclaw_db.models import LibraryFile
from movieclaw_jellyfin.catalog import media_source_dto
from movieclaw_jellyfin.errors import bad_request_text, not_found
from movieclaw_jellyfin.ids import EntityKind, decode_guid, media_source_guid
from movieclaw_jellyfin.security import require_device
from movieclaw_playback.streaming import container_mime_type, is_strm, resolve_strm_url

router = APIRouter(dependencies=[Depends(require_device)])


async def _files_for_ref(ref) -> list[LibraryFile]:
    """按条目/单元 GUID 取在位文件行（多版本多行，稳定排序）。"""
    async with get_database().session() as session:
        q = select(LibraryFile).where(
            LibraryFile.media_item_id == ref.entity_id,
            LibraryFile.missing_since.is_(None),
        )
        if ref.kind == EntityKind.EPISODE:
            q = q.where(
                LibraryFile.season_number == ref.season,
                LibraryFile.episode_number == ref.episode,
            )
        elif ref.kind == EntityKind.ITEM:
            q = q.where(LibraryFile.season_number == 0, LibraryFile.episode_number == 0)
        rows = list((await session.execute(q)).scalars())
    rows.sort(key=lambda f: f.id)
    return rows


def _select_source(
    files: list[LibraryFile], media_source_id: str | None, item_guid_raw: str
) -> list[LibraryFile]:
    """mediaSourceId 筛选：缺省全部；等于 itemId 时回落第一个（设计文档 6.2）。"""
    if not media_source_id:
        return files
    normalized = (media_source_id or "").lower().replace("-", "")
    for f in files:
        if media_source_guid(f.id) == normalized:
            return [f]
    item_norm = item_guid_raw.lower().replace("-", "")
    if normalized == item_norm and files:
        return [files[0]]
    return []


@router.get("/Items/{item_id}/PlaybackInfo")
@router.post("/Items/{item_id}/PlaybackInfo")
async def playback_info(request: Request, item_id: str) -> JSONResponse:
    ref = decode_guid(item_id)
    if ref is None or ref.kind not in (EntityKind.ITEM, EntityKind.EPISODE):
        raise not_found()

    # query 优先于 body；DeviceProfile 与 LiveStreamId 一律忽略（后者会短路
    # 源解析，绝不能当 mediaSourceId 用）
    media_source_id = request.query_params.get("mediaSourceId")
    if media_source_id is None and request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            lowered = {str(k).lower(): v for k, v in body.items()}
            raw = lowered.get("mediasourceid")
            media_source_id = str(raw) if raw else None

    files = await _files_for_ref(ref)
    selected = _select_source(files, media_source_id, item_id)
    if not selected:
        return JSONResponse(
            {"MediaSources": [], "ErrorCode": "NoCompatibleStream"}
        )
    # 播放协商是唯一现读 strm 的场景：直链多带时效签名，须现读现用；
    # 解析失败的版本剔除，全部失败按"无可播源"应答
    sources = [s for f in selected if (s := media_source_dto(f, resolve_strm=True))]
    if not sources:
        return JSONResponse({"MediaSources": [], "ErrorCode": "NoCompatibleStream"})
    return JSONResponse(
        {
            "MediaSources": sources,
            "PlaySessionId": secrets.token_hex(16),
        }
    )


@router.get("/Videos/{item_id}/stream")
@router.head("/Videos/{item_id}/stream")
@router.get("/Videos/{item_id}/stream.{container}")
@router.head("/Videos/{item_id}/stream.{container}")
async def video_stream(
    request: Request, item_id: str, container: str | None = None
) -> Response:
    ref = decode_guid(item_id)
    if ref is None or ref.kind not in (EntityKind.ITEM, EntityKind.EPISODE):
        raise not_found()
    static = (request.query_params.get("static") or "").lower() == "true"
    if not static:
        # 无 static=true 本应转码；我们不转码（偏离⑨）
        raise bad_request_text()

    files = await _files_for_ref(ref)
    selected = _select_source(files, request.query_params.get("mediaSourceId"), item_id)
    if not selected:
        raise not_found()
    f = selected[0]

    if is_strm(f.file_path):
        url = resolve_strm_url(f.file_path)
        if url is None:
            raise not_found()
        # 302 直链：HEAD 同样 302、不塞 body；重定向目标自己支持 Range
        return RedirectResponse(url, status_code=302)

    path = Path(f.file_path)
    if not path.is_file():
        raise not_found()
    media_type = container_mime_type(container or f.container or path.suffix)
    # FileResponse 原生处理 Range/206/If-Range/HEAD（starlette >= 0.36）
    return FileResponse(path, media_type=media_type)
