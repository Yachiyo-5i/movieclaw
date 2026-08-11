"""启动期轻量接口与敷衍实现（设计文档 §2 P1）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from movieclaw_jellyfin.security import require_device

router = APIRouter()


@router.get("/Branding/Configuration")
async def branding_configuration() -> JSONResponse:
    return JSONResponse({"SplashscreenEnabled": False})


@router.get("/Branding/Css")
@router.get("/Branding/Css.css")
async def branding_css() -> PlainTextResponse:
    return PlainTextResponse("", media_type="text/css")


@router.get("/QuickConnect/Enabled")
async def quickconnect_enabled() -> JSONResponse:
    # 恒 false（有意偏离：真默认 true）——客户端据此隐藏 QuickConnect 入口
    return JSONResponse(False)


@router.get("/Plugins", dependencies=[Depends(require_device)])
async def plugins() -> JSONResponse:
    # Infuse 验证媒体库时会拉取插件清单（issue #124）；无插件体系，空数组即可
    return JSONResponse([])


@router.get(
    "/DisplayPreferences/{display_preferences_id}",
    dependencies=[Depends(require_device)],
)
async def display_preferences(
    display_preferences_id: str, client: str = "emby"
) -> JSONResponse:
    """默认 DisplayPreferencesDto（issue #124）。

    Infuse 添加媒体库最后一步会请求 /DisplayPreferences/usersettings；
    不落库、恒返回默认值——展示偏好由客户端本地维护即可。
    字段与类型对齐 Jellyfin 10.10 的 DisplayPreferencesDto（可空字段省略）。
    """
    return JSONResponse(
        {
            "Id": display_preferences_id,
            "SortBy": "SortName",
            "RememberIndexing": False,
            "PrimaryImageHeight": 250,
            "PrimaryImageWidth": 250,
            "CustomPrefs": {},
            "ScrollDirection": "Horizontal",
            "ShowBackdrop": True,
            "RememberSorting": False,
            "SortOrder": "Ascending",
            "ShowSidebar": False,
            "Client": client,
        }
    )


@router.post(
    "/DisplayPreferences/{display_preferences_id}",
    status_code=204,
    dependencies=[Depends(require_device)],
)
async def update_display_preferences(display_preferences_id: str) -> Response:
    # 客户端回写展示偏好：接受但不存储（与 GET 的恒默认值语义一致）
    return Response(status_code=204)


@router.get("/Sessions", dependencies=[Depends(require_device)])
async def sessions() -> JSONResponse:
    return JSONResponse([])


@router.post(
    "/Sessions/Capabilities", status_code=204, dependencies=[Depends(require_device)]
)
@router.post(
    "/Sessions/Capabilities/Full",
    status_code=204,
    dependencies=[Depends(require_device)],
)
async def sessions_capabilities() -> Response:
    # 204 且不存储 DeviceProfile：PlaybackInfo 因此永远无 profile 可回退，
    # 等价于"无转码权限的 Jellyfin"（设计文档 6.1）
    return Response(status_code=204)


@router.delete(
    "/Videos/ActiveEncodings", status_code=204, dependencies=[Depends(require_device)]
)
async def active_encodings() -> Response:
    # 部分客户端退出时无条件发一次转码清理；我们无转码，204 即可
    return Response(status_code=204)
