"""认证与用户接口（设计文档 4.2/4.3）。"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from movieclaw_api.services import auth as auth_service
from movieclaw_api.settings.schemas import get_jellyfin_compat
from movieclaw_db.engine import get_database
from movieclaw_db.models import JellyfinDevice
from movieclaw_db.models.base import utcnow
from movieclaw_db.models.member import Member
from movieclaw_jellyfin.errors import (
    JellyfinError,
    bad_request_text,
    not_found_message,
)
from movieclaw_jellyfin.identity import session_info_dto, user_dto
from movieclaw_jellyfin.ids import normalize_guid, user_guid
from movieclaw_jellyfin.security import read_authorization, require_device

router = APIRouter()


@router.post("/Users/AuthenticateByName")
async def authenticate_by_name(request: Request) -> JSONResponse:
    auth = read_authorization(request)
    # 四键缺任一 → 400 text/plain（对齐 SessionManager 的 ArgumentException 链）
    if not auth.has_identity:
        raise bad_request_text()

    try:
        body = await request.json()
    except Exception:
        raise bad_request_text() from None
    if not isinstance(body, dict):
        raise bad_request_text()
    # 请求体键名大小写不敏感（MVC JsonSerializerDefaults.Web 行为）
    lowered = {str(k).lower(): v for k, v in body.items()}
    username = lowered.get("username") or ""
    password = lowered.get("pw") or ""
    if not username:
        raise bad_request_text()

    try:
        identity = await auth_service.authenticate(username, str(password))
    except Exception:
        # 密码错/账号不存在 → 401 text/plain "Error processing request."
        raise JellyfinError(401, text="Error processing request.") from None
    if isinstance(identity, Member):
        # 成员经 Jellyfin 登录属于 P1（设备须绑定 member_id、Policy 按成员
        # 投影）；在那之前放行会让成员拿到超管的 Jellyfin 身份，必须拒绝。
        raise JellyfinError(401, text="Error processing request.")

    setting = await get_jellyfin_compat()
    token = secrets.token_hex(16)
    async with get_database().session() as session:
        # 同 device_id 重登录：覆盖并换发 token（旧 token 即刻失效，防凭据累积）
        device = (
            await session.execute(
                select(JellyfinDevice).where(JellyfinDevice.device_id == auth.device_id)
            )
        ).scalar_one_or_none()
        if device is None:
            device = JellyfinDevice(token=token, device_id=auth.device_id)
            session.add(device)
        else:
            device.token = token
        device.client = auth.client
        device.device_name = auth.device
        device.version = auth.version
        device.last_seen_at = utcnow()
        device.updated_at = utcnow()
        await session.commit()

    account = await auth_service.get_admin_account()
    return JSONResponse(
        {
            "User": await user_dto(setting.server_id),
            "SessionInfo": session_info_dto(
                setting.server_id,
                secrets.token_hex(16),
                client=auth.client,
                device_id=auth.device_id,
                device_name=auth.device,
                version=auth.version,
                user_name=account.username,
            ),
            "AccessToken": token,
            "ServerId": setting.server_id,
        }
    )


@router.get("/Users/Public")
async def users_public() -> JSONResponse:
    setting = await get_jellyfin_compat()
    return JSONResponse([await user_dto(setting.server_id)])


@router.get("/Users/Me", dependencies=[Depends(require_device)])
async def users_me() -> JSONResponse:
    setting = await get_jellyfin_compat()
    return JSONResponse(await user_dto(setting.server_id))


@router.get("/Users/{user_id}", dependencies=[Depends(require_device)])
async def users_by_id(user_id: str) -> JSONResponse:
    normalized = normalize_guid(user_id)
    if normalized is None:
        # 路由段解析失败是参数错误（400），绝不能 401——会触发客户端登录循环
        raise bad_request_text()
    if normalized != user_guid():
        raise not_found_message("User not found")
    setting = await get_jellyfin_compat()
    return JSONResponse(await user_dto(setting.server_id))
