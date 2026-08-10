from __future__ import annotations

import hmac

from fastapi import Cookie, Depends, Header

from movieclaw_api.exceptions import ForbiddenException, UnauthorizedException
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.auth import Principal
from movieclaw_api.settings.schemas import get_sync_setting


def _extract_bearer(authorization: str | None) -> str | None:
    """从 Authorization 头中取出 Bearer 令牌；格式不符返回 None。"""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


async def require_sync_token(authorization: str | None = Header(default=None)) -> None:
    """插件侧接口的鉴权依赖：校验请求头里的同步令牌。

    校验流程：
    1. 后端从未生成令牌（同步未启用）→ 401，提示先去后台生成令牌。
    2. 请求未带 Bearer 令牌或与后端不一致 → 401，提示令牌无效/已重置。

    比较使用 ``hmac.compare_digest`` 做常量时间比较，避免时序侧信道。
    错误信息为清晰中文，方便非开发者按提示操作。
    """
    setting = await get_sync_setting()
    if not setting.token:
        raise UnauthorizedException("后端未启用同步，请先在后台生成令牌")

    provided = _extract_bearer(authorization)
    if not provided or not hmac.compare_digest(provided, setting.token):
        raise UnauthorizedException("令牌无效或已重置，请重新填写")


async def require_login(
    session_token: str | None = Cookie(default=None, alias=auth_service.SESSION_COOKIE_NAME),
    authorization: str | None = Header(default=None),
) -> Principal:
    """业务接口的登录鉴权依赖：会话 Cookie **或** Bearer 令牌，返回请求主体。

    两条通道（docs/design/cli.md §8.1）：
    - Web 端：会话 Cookie（超管或成员，由令牌负载区分）；
    - CLI / 产品内 Agent：``Authorization: Bearer <令牌>``——PAT 长期令牌
      或 Agent 短时效签名令牌，同一验签入口。

    全站默认拒绝的执行点——除公开白名单与插件侧接口外，所有路由都必须挂
    本依赖（api/router.py 按组挂载，tests 里有守护测试兜底防漏挂）。
    未登录 / 会话过期 / 令牌无效统一 401。授权（管理员/能力开关）不在
    这里判——挂 ``require_admin`` 或在服务层消费 Principal。
    """
    if session_token:
        return await auth_service.verify_session_token(session_token)
    if bearer := _extract_bearer(authorization):
        return await auth_service.verify_bearer_token(bearer)
    raise UnauthorizedException("未登录，请先登录")


async def require_admin(principal: Principal = Depends(require_login)) -> Principal:
    """管理员鉴权依赖：在登录之上断言管理员身份，成员访问一律 403。

    与守护测试的契约（tests/api/test_member_auth.py）：不在成员白名单里的
    路由必须由本依赖（或服务层等价判定）挡住成员——新增管理路由挂本依赖
    即自动满足契约。
    """
    if not principal.is_admin:
        raise ForbiddenException("该操作需要管理员权限")
    return principal


async def require_search_capability(
    principal: Principal = Depends(require_login),
) -> Principal:
    """站点搜索能力依赖：管理员直通；成员须开启 ``allow_search`` 开关。

    搜索消耗站点配额、暴露站点存在，因此默认对成员关闭，由管理员在成员
    管理页逐人开启（docs/design/member-management.md §2.2）。
    """
    if principal.is_admin:
        return principal
    if principal.member is None or not principal.member.allow_search:
        raise ForbiddenException("管理员未对你开放站点搜索，请联系管理员开启")
    return principal


async def require_subscribe_capability(
    principal: Principal = Depends(require_login),
) -> Principal:
    """订阅能力依赖：管理员直通；成员须开启 ``allow_subscribe`` 开关。"""
    if principal.is_admin:
        return principal
    if principal.member is None or not principal.member.allow_subscribe:
        raise ForbiddenException("管理员未对你开放订阅功能，请联系管理员开启")
    return principal
