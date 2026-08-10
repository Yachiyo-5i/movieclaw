"""界面偏好接口：全站页面样式设定的读写。

设定按页面分组（结构见 settings.schemas 的 ``UiPreferencesSetting``），
本路由做整体读写透传——纯用户偏好、无敏感字段、无业务校验（结构校验由
Pydantic 完成，未知字段按前向兼容忽略），因此请求/响应体直接复用配置域
模型，不另抄一份 schema。

存储按主体分流（docs/design/member-management.md P2）：
- 超管 → ``ui.preferences`` 全局配置域（升级前的既有数据原地生效）；
- 成员 → ``member.ui_prefs`` JSON 列，各存各的；NULL 回退默认值——
  成员拖外观滑杆不再覆盖全家的界面设定。

前端在应用启动时 GET 一次、Context 全站共享；每次改动 PUT 整体覆盖。
未来新增页面的样式设定：配置域模型加字段 → 前端类型同步 → 无需动本文件。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.api.deps import require_login
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.auth import Principal
from movieclaw_api.settings.schemas import (
    UiPreferencesSetting,
    get_ui_preferences,
    save_ui_preferences,
)
from movieclaw_db.engine import get_session
from movieclaw_db.repositories.member_repo import MemberRepository

router = APIRouter(prefix="/ui", tags=["ui"])


@router.get(
    "/preferences",
    response_model=ApiResponse[UiPreferencesSetting],
    summary="读取界面偏好（按页面分组的样式设定）",
    operation_id="ui.prefs.show",
)
async def get_preferences(
    principal: Principal = Depends(require_login),
) -> ApiResponse[UiPreferencesSetting]:
    """返回当前主体的界面样式偏好；从未配置过的部分返回默认值。"""
    if principal.kind == "member" and principal.member is not None:
        raw = principal.member.ui_prefs
        # 未知字段前向兼容忽略（与配置域读取同语义）
        return ok(UiPreferencesSetting.model_validate(raw) if raw else UiPreferencesSetting())
    return ok(await get_ui_preferences())


@router.put(
    "/preferences",
    response_model=ApiResponse[UiPreferencesSetting],
    summary="保存界面偏好（整体覆盖）",
    operation_id="ui.prefs.update",
)
async def update_preferences(
    payload: UiPreferencesSetting,
    principal: Principal = Depends(require_login),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[UiPreferencesSetting]:
    """整体覆盖式保存界面偏好，返回保存后的值。成员写自己的列，不碰全局。"""
    if principal.kind == "member" and principal.member_id is not None:
        repo = MemberRepository(session)
        member = await repo.get(principal.member_id)
        if member is not None:
            member.ui_prefs = payload.model_dump(mode="json")
            await repo.save(member)
        return ok(payload, message="界面设置已保存")
    return ok(await save_ui_preferences(payload), message="界面设置已保存")
