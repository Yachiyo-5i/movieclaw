"""播放记录的 Web 业务接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import require_login
from movieclaw_api.schemas.playback import RecentWatchView
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.library.access import visible_library_ids
from movieclaw_api.services.playback_recent import recent_watch_items
from movieclaw_db.engine import get_session

router = APIRouter(prefix="/playback", tags=["playback"])


@router.get(
    "/recent",
    response_model=ApiResponse[RecentWatchView],
    summary="最近观看",
    operation_id="playback.recent",
    openapi_extra={"x-cli-hidden": True},
)
async def list_recent_watch(
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[RecentWatchView]:
    """列出当前账号在可见媒体库中的最近观看作品。"""
    visible_ids = await visible_library_ids(session, principal)
    member_id = principal.member_id if principal.member_id is not None else 0
    items = await recent_watch_items(
        session,
        member_id=member_id,
        visible_library_ids=visible_ids,
        limit=limit,
    )
    return ok(RecentWatchView(items=items))
