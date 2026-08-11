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


def _display_preferences_guid(raw: str) -> str:
    """DisplayPreferencesDto.Id：对齐真 Jellyfin 的派生规则（10.10 源码）。

    DisplayPreferencesController 先 Guid.TryParse，失败则取
    ``GetMD5``（UTF-16LE 编码取 MD5，再按 .NET Guid(byte[]) 的
    小端字节序构造），输出为带连字符的 "D" 格式。
    金样：'usersettings' → 3ce5b65d-e116-d731-65d1-efc4a30ec35c。
    """
    import hashlib

    from movieclaw_jellyfin.ids import normalize_guid

    normalized = normalize_guid(raw)
    if normalized is None:
        digest = hashlib.md5(raw.encode("utf-16-le")).digest()
        # .NET Guid(byte[])：前 4/2/2 字节按小端翻转，后 8 字节原样
        normalized = (digest[3::-1] + digest[5:3:-1] + digest[7:5:-1] + digest[8:]).hex()
    return (
        f"{normalized[:8]}-{normalized[8:12]}-{normalized[12:16]}"
        f"-{normalized[16:20]}-{normalized[20:]}"
    )


@router.get(
    "/DisplayPreferences/{display_preferences_id}",
    dependencies=[Depends(require_device)],
)
async def display_preferences(
    display_preferences_id: str, client: str = "emby"
) -> JSONResponse:
    """默认 DisplayPreferencesDto（issue #124）。

    Infuse 添加媒体库最后一步会请求 /DisplayPreferences/usersettings；
    不落库、恒返回默认值——展示偏好由客户端本地维护即可。字段、类型与
    默认值对齐真 Jellyfin 10.10 新用户首次请求的响应（实体默认值 +
    DisplayPreferencesDto 构造默认；可空字段 ViewType/IndexBy 省略）。
    """
    return JSONResponse(
        {
            "Id": _display_preferences_guid(display_preferences_id),
            "SortBy": "SortName",
            "RememberIndexing": False,
            "PrimaryImageHeight": 250,
            "PrimaryImageWidth": 250,
            # 真 Jellyfin 新建 DisplayPreferences 时的 CustomPrefs 快照
            # （无 HomeSections → 无 homesectionN 键；布尔是 C# ToString 形态）
            "CustomPrefs": {
                "chromecastVersion": "stable",
                "skipForwardLength": "30000",
                "skipBackLength": "10000",
                "enableNextVideoInfoOverlay": "False",
                "tvhome": None,
                "dashboardTheme": None,
            },
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
