"""通用图片代理接口（带本地磁盘缓存）与刮削图片资产直出。"""

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.services.image_cache import get_image_cache
from movieclaw_api.services.image_variants import (
    ImageVariant,
    get_image_variant_service,
    local_source_version,
)

router = APIRouter(prefix="/images", tags=["images"])


@router.get(
    "/proxy",
    response_class=FileResponse,
    summary="代理并缓存远程图片",
    operation_id="images.proxy",
    openapi_extra={"x-cli-hidden": True},
)
async def proxy_image(
    url: str = Query(min_length=1, max_length=2048),
    variant: ImageVariant | None = Query(default=None),
) -> FileResponse:
    """前端所有远程图片的统一入口：命中读本地缓存，未命中回源抓取后落盘。

    域名安全（SSRF 防护）、类型和体积校验在 ImageProxy 服务层完成。
    图床 URL 对应的内容事实上不可变，浏览器侧直接给一年 immutable 缓存。
    """
    cached = await get_image_cache().get_or_fetch(url)
    if variant is not None:
        cached = await get_image_variant_service().get_or_create(
            cached.path,
            source_key=f"remote:{url}",
            source_version=cached.version,
            variant=variant,
        )
    return FileResponse(
        cached.path,
        media_type=cached.content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get(
    "/assets/{path:path}",
    response_class=FileResponse,
    summary="刮削图片资产直出（data/metadata/images 下的本地文件）",
    operation_id="images.asset",
    openapi_extra={"x-cli-hidden": True},
)
async def get_metadata_asset(
    path: str,
    variant: ImageVariant | None = Query(default=None),
    v: str | None = Query(default=None, max_length=32),
) -> FileResponse:
    """海报/剧照等刮削资产的服务通道（docs/design/metadata.md 6.1）。

    路径限定在资产根目录内（防目录穿越）。force 刷新会原地覆盖同名文件，
    故不给 immutable，一天后重新校验即可。
    """
    from movieclaw_api.services.media_scrape import assets_root

    root = assets_root().resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise NotFoundException("图片资产不存在")
    if variant is not None:
        cached = await get_image_variant_service().get_or_create(
            target,
            source_key=f"asset:{path}",
            source_version=local_source_version(target),
            variant=variant,
        )
        # 业务 URL 携带的 v 与当前文件版本一致时才可永久缓存；手写的错误 v
        # 仍给一天缓存，避免同一 URL 在换图后长期停留旧派生图。
        immutable = v == str(int(target.stat().st_mtime))
        cache_control = (
            "public, max-age=31536000, immutable"
            if immutable
            else "public, max-age=86400"
        )
        return FileResponse(
            cached.path,
            media_type=cached.content_type,
            headers={"Cache-Control": cache_control},
        )
    return FileResponse(
        target,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
