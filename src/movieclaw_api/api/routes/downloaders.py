from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.downloader import (
    DownloaderPayload,
    DownloaderStatusUpdate,
    DownloaderView,
    DownloadSubmitPayload,
    DownloadSubmitView,
    DownloadTaskDeleteView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.downloader_config import (
    DownloaderConfigService,
    verify_downloader,
)
from movieclaw_api.services.library.config import LibraryConfigService
from movieclaw_api.services.torrent_submit import (
    delete_torrent,
    submit_torrent,
    translate_save_path,
)
from movieclaw_db.engine import get_session

router = APIRouter(prefix="/downloaders", tags=["downloaders"])


def _mappings_to_rows(payload: DownloaderPayload) -> list[dict[str, str]] | None:
    """把请求体里的路径映射转成落库的字典列表（校验已在 schema 完成）。"""
    if payload.path_mappings is None:
        return None
    return [m.model_dump() for m in payload.path_mappings]


@router.post(
    "/submit",
    response_model=ApiResponse[DownloadSubmitView],
    summary="把一条搜索结果种子提交到下载器（缺省默认下载器，可指定分流）",
    operation_id="dl.submit",
)
async def submit_download(
    payload: DownloadSubmitPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloadSubmitView]:
    """手动下载：带站点登录态取回 .torrent → 提交给默认下载器。

    保存目录取值：调用方手选的 save_path 最优先；否则与订阅同一决策——
    选了库且库有监听导入规则 → 规则源目录（完成后监听导入接管、规范命名
    进库）；选库无规则 → 库推导路径（主根/标题 (年份)，直接下载进库原地
    收纳——扫描的完整性检测保证半成品不入账）；未选库 → 下载器默认目录。
    提交幂等：种子已在下载器中不视为错误，data.already_exists=true。
    """
    from movieclaw_api.services.library.routing import resolve_save_path

    library = None
    derived_path = None
    entry_level = False  # 条目级目录才允许锚下载线索
    if payload.save_path is not None:
        # 用户在下载弹窗里手选了目录：完全尊重（映射守门仍在 submit_torrent）；
        # 手选目录不是条目级，不锚下载线索
        derived_path = payload.save_path
    elif payload.library_id is not None:
        # 三级兜底与订阅投递同一实现（library_routing.resolve_save_path）
        library = await LibraryConfigService(session).get(payload.library_id)
        decision = await resolve_save_path(
            session, library, kind=library.kind, title=payload.title, year=payload.year
        )
        derived_path = decision.path
        entry_level = decision.entry_level

    result, row = await submit_torrent(
        session,
        site_id=payload.site_id,
        download_url=payload.download_url,
        tags=["movieclaw-manual"],
        save_path=derived_path,
        # 线索只锚**条目级**目录；锚到库主根/监听目录会波及目录下所有文件
        subtitle=payload.subtitle if entry_level else None,
        downloader_id=payload.downloader_id,
    )
    assert row.id is not None  # 落库记录必有主键
    view = DownloadSubmitView(
        info_hash=result.info_hash,
        name=result.name,
        already_exists=result.already_exists,
        downloader_id=row.id,
        downloader_name=row.name,
        # 回显下载器视角的实际保存目录（有路径映射时与本地视角不同），
        # 跨容器部署配错映射能在提交结果里第一时间看出来
        save_path=translate_save_path(derived_path or row.save_path, row.path_mappings),
    )
    if result.already_exists:
        message = "该种子已在下载器中，未重复添加"
    elif library is not None:
        message = f"已提交到「{row.name}」，入库到「{library.name}」"
    else:
        message = f"已提交到「{row.name}」"
    return ok(view, message=message)


@router.delete(
    "/{downloader_id}/torrents/{info_hash}",
    response_model=ApiResponse[DownloadTaskDeleteView],
    summary="删除 MovieClaw 创建的 qBittorrent 下载任务（保留已下载数据）",
    operation_id="dl.torrent.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_download_task(
    downloader_id: int,
    info_hash: str = Path(
        pattern="^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$",
        description="要删除的 qBittorrent 下载任务 infohash（v1 为 40 位，v2 为 64 位）",
    ),
    delete_files: str | None = Query(default=None, include_in_schema=False),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloadTaskDeleteView]:
    """删除单个 MovieClaw 创建的 qBittorrent 任务，磁盘数据保持不动。"""
    if delete_files is not None:
        raise BadRequestException(
            "删除磁盘数据必须调用「删除任务及数据」接口，不能通过 delete_files 参数请求"
        )
    normalized_hash = info_hash.lower()
    row = await delete_torrent(
        session,
        downloader_id=downloader_id,
        info_hash=normalized_hash,
        delete_files=False,
    )
    return ok(
        DownloadTaskDeleteView(info_hash=normalized_hash, delete_files=False),
        message=f"已确认下载器「{row.name}」中任务已删除，已保留已下载数据",
    )


@router.delete(
    "/{downloader_id}/torrents/{info_hash}/files",
    response_model=ApiResponse[DownloadTaskDeleteView],
    summary="删除 MovieClaw 创建的 qBittorrent 下载任务及已下载数据",
    operation_id="dl.torrent.files.delete",
    openapi_extra={"x-cli-dangerous": "destructive"},
)
async def delete_download_task_and_files(
    downloader_id: int,
    info_hash: str = Path(
        pattern="^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$",
        description="要删除的 qBittorrent 下载任务 infohash（v1 为 40 位，v2 为 64 位）",
    ),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloadTaskDeleteView]:
    """删除单个 MovieClaw 创建的 qBittorrent 任务及其落盘数据。

    此接口仅供明确了解影响面的 Web 或本机 CLI 使用；产品内 Agent 被工具层
    硬性禁止调用，避免模型自行生成确认参数而误删磁盘文件。
    """
    normalized_hash = info_hash.lower()
    row = await delete_torrent(
        session,
        downloader_id=downloader_id,
        info_hash=normalized_hash,
        delete_files=True,
    )
    return ok(
        DownloadTaskDeleteView(info_hash=normalized_hash, delete_files=True),
        message=f"已确认下载器「{row.name}」中任务及已下载数据均已删除",
    )


@router.get(
    "",
    response_model=ApiResponse[list[DownloaderView]],
    summary="列出已配置的下载器及连接状态",
    operation_id="dl.list",
)
async def list_downloaders(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[DownloaderView]]:
    service = DownloaderConfigService(session)
    rows = await service.list_all()
    return ok([DownloaderView.from_model(r) for r in rows])


@router.get(
    "/{downloader_id}",
    response_model=ApiResponse[DownloaderView],
    summary="获取单个下载器详情",
    operation_id="dl.show",
)
async def get_downloader(
    downloader_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    service = DownloaderConfigService(session)
    return ok(DownloaderView.from_model(await service.get(downloader_id)))


@router.post(
    "",
    response_model=ApiResponse[DownloaderView],
    summary="添加一个下载器（保存后异步测试连接）",
    operation_id="dl.add",
)
async def create_downloader_config(
    payload: DownloaderPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    """保存下载器连接信息（状态置 pending），并在后台异步测试连通性。

    接口立即返回，调用方可轮询下载器详情（CLI：mclaw dl show <id>）观察 status 变化：
    pending → verifying → active（可用）/ failed（见 last_error）。
    """
    service = DownloaderConfigService(session)
    row = await service.create(
        name=payload.name,
        client_type=payload.client_type,
        url=payload.url,
        username=payload.username,
        password=payload.password,
        save_path=payload.save_path,
        path_mappings=_mappings_to_rows(payload),
        enabled=payload.enabled,
    )
    assert row.id is not None  # 落库后必有主键
    row = await service.start_verification(row.id)
    background_tasks.add_task(verify_downloader, row.id)
    return ok(DownloaderView.from_model(row), message="已保存，正在测试连接")


@router.put(
    "/{downloader_id}",
    response_model=ApiResponse[DownloaderView],
    summary="更新下载器配置（更新后重新测试连接）",
    operation_id="dl.update",
)
async def update_downloader_config(
    downloader_id: int,
    payload: DownloaderPayload,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    service = DownloaderConfigService(session)
    await service.update(
        downloader_id,
        name=payload.name,
        client_type=payload.client_type,
        url=payload.url,
        username=payload.username,
        password=payload.password,
        save_path=payload.save_path,
        path_mappings=_mappings_to_rows(payload),
        enabled=payload.enabled,
    )
    row = await service.start_verification(downloader_id)
    background_tasks.add_task(verify_downloader, downloader_id)
    return ok(DownloaderView.from_model(row), message="已更新，正在测试连接")


@router.patch(
    "/{downloader_id}/status",
    response_model=ApiResponse[DownloaderView],
    summary="启用 / 停用下载器",
    operation_id="dl.status.set",
)
async def set_downloader_status(
    downloader_id: int,
    payload: DownloaderStatusUpdate,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    service = DownloaderConfigService(session)
    row = await service.set_enabled(downloader_id, payload.enabled)
    return ok(DownloaderView.from_model(row))


@router.post(
    "/{downloader_id}/default",
    response_model=ApiResponse[DownloaderView],
    summary="设为默认下载器",
    operation_id="dl.default.set",
)
async def set_default_downloader(
    downloader_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    """把该下载器设为默认（一键下载不选目标时投给它），同时取消其他台的默认。

    注意：其他记录的 is_default 也会随之变化，调用方应重新读取列表。"""
    service = DownloaderConfigService(session)
    row = await service.set_default(downloader_id)
    return ok(DownloaderView.from_model(row), message="已设为默认下载器")


@router.post(
    "/{downloader_id}/verify",
    response_model=ApiResponse[DownloaderView],
    summary="手动重新测试连接",
    operation_id="dl.verify",
)
async def reverify_downloader(
    downloader_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[DownloaderView]:
    """手动触发一次连接测试（如下载器重启后想确认恢复）。

    行为：不存在 → 404；已在测试中 → 409；否则同步占位为 VERIFYING
    并在后台重新测试。"""
    service = DownloaderConfigService(session)
    row = await service.start_verification(downloader_id)
    background_tasks.add_task(verify_downloader, downloader_id)
    return ok(DownloaderView.from_model(row), message="已重新发起连接测试")


@router.delete(
    "/{downloader_id}",
    response_model=ApiResponse[dict],
    summary="删除下载器配置",
    operation_id="dl.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_downloader_config(
    downloader_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    service = DownloaderConfigService(session)
    await service.delete(downloader_id)
    return ok({}, message="已删除")
