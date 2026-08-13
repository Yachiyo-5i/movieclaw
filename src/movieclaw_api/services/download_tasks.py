"""任务中心的下载器实时聚合视图。

下载进度只存在于 qBittorrent / Transmission，本服务不复制第二份状态；每次
请求并行读取所有可用下载器的一次列表快照，再按 infohash 关联订阅工单与手动
下载意图。某台下载器不可达时只把该来源标为异常，其余来源仍正常返回。
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import NotFoundException, UpstreamServiceException
from movieclaw_api.services.network_egress import effective_tmdb_image_base_url
from movieclaw_db.models import (
    ConfigStatus,
    DownloaderClient,
    LibraryFile,
    ManualDownloadIntent,
    MediaItem,
    Subscription,
    WantedItem,
    WantedStatus,
)
from movieclaw_db.repositories.downloader_repo import DownloaderRepository
from movieclaw_downloader import (
    DownloaderConfig,
    DownloaderException,
    TorrentBrief,
    TorrentStatus,
    create_downloader,
)

logger = logging.getLogger("movieclaw_api.download_tasks")

_IN_FLIGHT = (WantedStatus.GRABBED, WantedStatus.DOWNLOADED)


def _poster_url(item: MediaItem) -> str | None:
    """把已识别条目的海报收口为可直接展示的 TMDB 地址。"""
    if not item.poster_path:
        return None
    base = effective_tmdb_image_base_url().rstrip("/")
    return f"{base}/w342{item.poster_path}"


async def _relations(
    session: AsyncSession,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """一次查询装好订阅/手动意图索引，避免按下载任务逐条查库。"""
    subscription_rows = (
        await session.execute(
            select(WantedItem, Subscription, MediaItem)
            .join(Subscription, Subscription.id == WantedItem.subscription_id)
            .join(MediaItem, MediaItem.id == WantedItem.media_item_id)
            .where(
                WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                WantedItem.info_hash.is_not(None),  # type: ignore[union-attr]
            )
        )
    ).all()
    grouped_units: dict[tuple[str, int], list[dict[str, int]]] = defaultdict(list)
    subscription_meta: dict[tuple[str, int], dict[str, Any]] = {}
    for wanted, subscription, media in subscription_rows:
        assert wanted.info_hash is not None and subscription.id is not None
        info_hash = wanted.info_hash.lower()
        key = (info_hash, subscription.id)
        grouped_units[key].append(
            {
                "season_number": wanted.season_number,
                "episode_number": wanted.episode_number,
            }
        )
        subscription_meta[key] = {
            "id": subscription.id,
            "media_item_id": media.id,
            "media_title": media.title,
            "media_kind": media.kind,
            "poster_url": _poster_url(media),
        }

    subscriptions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, meta in subscription_meta.items():
        info_hash, _subscription_id = key
        subscriptions[info_hash].append(
            {
                **meta,
                "units": sorted(
                    grouped_units[key],
                    key=lambda unit: (unit["season_number"], unit["episode_number"]),
                ),
            }
        )

    manual_rows = (
        await session.execute(
            select(ManualDownloadIntent, MediaItem).join(
                MediaItem, MediaItem.id == ManualDownloadIntent.media_item_id
            )
        )
    ).all()
    manual = {
        intent.info_hash.lower(): {
            "intent_id": intent.id,
            "intent_created_at": intent.created_at,
            "library_id": intent.library_id,
            "media_item_id": media.id,
            "media_title": media.title,
            "media_kind": media.kind,
            "poster_url": _poster_url(media),
        }
        for intent, media in manual_rows
    }
    return dict(subscriptions), manual


def _torrent_video_files(
    status: TorrentStatus, downloader: DownloaderClient
) -> list[tuple[str, int]]:
    """把下载器文件清单还原成 MovieClaw 视角的绝对视频路径。

    历史手动下载锚没有保存投递模式和目录，只能以下载器仍持有的事实反查。
    路径先按下载器映射翻译回来；相对文件名拒绝绝对路径与 ``..``，避免异常
    上游数据越出保存目录。这里只计算字符串用于台账对账，不触碰磁盘。
    """
    from movieclaw_api.services.library.layout import VIDEO_EXTS
    from movieclaw_api.services.torrent_submit import translate_to_local

    local_base = translate_to_local(status.save_path, downloader.path_mappings)
    if not local_base:
        return []
    base = local_base.replace("\\", "/")
    files: list[tuple[str, int]] = []
    for torrent_file in status.files:
        relative = PurePosixPath(torrent_file.path.replace("\\", "/"))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            continue
        lower_name = relative.name.lower()
        if relative.suffix.lower() not in VIDEO_EXTS or "sample" in lower_name:
            continue
        files.append(
            (
                posixpath.normpath(posixpath.join(base, *relative.parts)),
                torrent_file.size_bytes,
            )
        )
    return files


def _inventory_covers_torrent(
    *,
    status: TorrentStatus,
    downloader: DownloaderClient,
    manual: dict[str, Any],
    inventory: list[LibraryFile],
) -> bool:
    """库存是否逐文件覆盖一个已完成的手动种子。

    原地下载优先按绝对路径精确命中；监听导入可能改名，历史版本又没有在
    ``library_file`` 写 torrent_id，因此只对身份锚创建后新增的同库、同媒体
    文件开放“尺寸相同”补偿。每个库存行最多消费一次，季包只入了一部分时
    不会提前收尾；仅仅拥有该剧的旧集也不会误清新任务。
    """
    expected = _torrent_video_files(status, downloader)
    if not status.completed or not expected:
        return False

    relevant = [
        row
        for row in inventory
        if row.library_id == manual["library_id"]
        and row.media_item_id == manual["media_item_id"]
        and row.missing_since is None
    ]
    unused = set(range(len(relevant)))
    for expected_path, expected_size in expected:
        matched = next(
            (
                index
                for index in unused
                if posixpath.normpath(relevant[index].file_path.replace("\\", "/"))
                == expected_path
            ),
            None,
        )
        if matched is None and expected_size > 0:
            matched = next(
                (
                    index
                    for index in unused
                    if relevant[index].size_bytes == expected_size
                    and relevant[index].created_at >= manual["intent_created_at"]
                ),
                None,
            )
        if matched is None:
            return False
        unused.remove(matched)
    return True


async def _reconcile_completed_manual_intents(
    session: AsyncSession,
    manual: dict[str, dict[str, Any]],
    details: dict[str, tuple[DownloaderClient, TorrentStatus]],
) -> set[str]:
    """用下载器文件清单与库存台账回收已完成的手动下载身份锚。

    这是对旧版本残留的读时修复：任务中心本来就要联合下载器与库存事实，
    第一次确认全部视频已经入账后删除瞬态身份锚；证据不足时保持原样等待。
    """
    candidates = {info_hash: manual[info_hash] for info_hash in details if info_hash in manual}
    if not candidates:
        return set()
    media_ids = {row["media_item_id"] for row in candidates.values()}
    library_ids = {row["library_id"] for row in candidates.values()}
    inventory = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.media_item_id.in_(media_ids),  # type: ignore[union-attr]
                    LibraryFile.library_id.in_(library_ids),  # type: ignore[union-attr]
                    LibraryFile.missing_since.is_(None),  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )

    consumed: set[str] = set()
    for info_hash, meta in candidates.items():
        downloader, status = details[info_hash]
        if not _inventory_covers_torrent(
            status=status,
            downloader=downloader,
            manual=meta,
            inventory=inventory,
        ):
            continue
        intent = await session.get(ManualDownloadIntent, meta["intent_id"])
        if intent is None:
            continue
        await session.delete(intent)
        consumed.add(info_hash)
        logger.info(
            "已按下载文件与库存台账对账，清理手动下载残留身份锚：hash=%s media_item=%s",
            info_hash,
            meta["media_item_id"],
        )
    if consumed:
        await session.commit()
    return consumed


def _task_dict(
    *,
    task_id: str,
    info_hash: str,
    torrent: TorrentBrief | None,
    downloader: DownloaderClient | None,
    subscriptions: list[dict[str, Any]],
    manual: dict[str, Any] | None,
) -> dict[str, Any]:
    source = "subscription" if subscriptions else "manual" if manual else "external"
    media = subscriptions[0] if subscriptions else manual
    completed = bool(torrent and torrent.completed)
    state = "missing" if torrent is None else "completed" if completed else torrent.state
    return {
        "id": task_id,
        "info_hash": info_hash,
        "name": torrent.name if torrent is not None else (media or {}).get("media_title"),
        "downloader_id": downloader.id if downloader is not None else None,
        "downloader_name": downloader.name if downloader is not None else None,
        "downloader_type": downloader.client_type if downloader is not None else None,
        "progress": torrent.progress if torrent is not None else None,
        "size_bytes": torrent.size_bytes if torrent is not None else None,
        "dlspeed_bytes": torrent.dlspeed_bytes if torrent is not None else None,
        "eta_seconds": torrent.eta_seconds if torrent is not None else None,
        "state": state,
        "source": source,
        "media_item_id": (media or {}).get("media_item_id"),
        "media_title": (media or {}).get("media_title"),
        "media_kind": (media or {}).get("media_kind"),
        "poster_url": (media or {}).get("poster_url"),
        "subscriptions": subscriptions,
    }


async def delete_download_task(
    session: AsyncSession,
    *,
    downloader_id: int,
    info_hash: str,
    delete_files: bool = False,
) -> None:
    """从指定下载器删除一个种子任务。

    下载器 ID 与 infohash 共同定位，避免多下载器场景误删同名任务。默认只
    移除任务；用户显式选择时才同时删除数据文件。底层删除保持幂等，因此
    用户确认期间任务恰好自行消失也返回成功。
    """
    repository = DownloaderRepository(session)
    row = await repository.get(downloader_id)
    if row is None:
        raise NotFoundException(f"下载器不存在：id={downloader_id}")

    adapter = create_downloader(
        DownloaderConfig(
            type=row.client_type.value,
            url=row.url,
            username=row.username,
            password=repository.decrypted_password(row),
        )
    )
    normalized_hash = info_hash.lower()
    try:
        await adapter.delete_torrent(normalized_hash, delete_files=delete_files)
    except DownloaderException as exc:
        logger.warning(
            "从下载器「%s」删除任务失败：hash=%s error=%s",
            row.name,
            normalized_hash,
            exc.message,
        )
        raise UpstreamServiceException(exc.message) from exc
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001 -- 关闭失败不能覆盖已经完成的删除结果
            logger.warning("关闭下载器「%s」连接失败", row.name, exc_info=True)

    logger.info(
        "已从下载器「%s」删除任务%s：hash=%s",
        row.name,
        "并删除数据文件" if delete_files else "并保留数据文件",
        normalized_hash,
    )
    # 手动意图不是救援工单。用户明确删除下载器任务后，它已不可能再靠该
    # hash 驱动监听认领，必须同步回收；否则虽然界面不再显示，数据库仍要等
    # 90 天兜底窗口才清掉孤儿锚。
    intent = (
        await session.execute(
            select(ManualDownloadIntent).where(
                ManualDownloadIntent.info_hash == normalized_hash
            )
        )
    ).scalar_one_or_none()
    if intent is not None:
        await session.delete(intent)
        await session.commit()
        logger.info("已清理被删除手动任务的身份锚：hash=%s", normalized_hash)


async def download_task_snapshot(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    """并行读取下载器，返回活跃下载与仍在业务管线内的完成/缺失任务。"""
    subscriptions, manual = await _relations(session)
    repository = DownloaderRepository(session)
    downloaders = await repository.list_all()

    async def read_source(
        row: DownloaderClient,
    ) -> tuple[
        dict[str, Any],
        list[TorrentBrief],
        dict[str, tuple[DownloaderClient, TorrentStatus]],
    ]:
        assert row.id is not None
        base = {
            "id": row.id,
            "name": row.name,
            "client_type": row.client_type,
            "task_count": 0,
        }
        if not row.enabled:
            return {**base, "status": "disabled", "message": "下载器已停用"}, [], {}
        if row.status != ConfigStatus.ACTIVE:
            message = row.last_error or "下载器尚未通过连接验证"
            return {**base, "status": "unavailable", "message": message}, [], {}

        adapter = None
        completed_manual: dict[str, tuple[DownloaderClient, TorrentStatus]] = {}
        try:
            # 解密凭据和构造适配器也属于单台来源边界。某条配置损坏时应在
            # 来源区给出可读错误，而不是让其它下载器的健康任务一起消失。
            adapter = create_downloader(
                DownloaderConfig(
                    type=row.client_type.value,
                    url=row.url,
                    username=row.username,
                    password=repository.decrypted_password(row),
                )
            )
            torrents = await adapter.list_torrents()
            # 轻量列表没有保存目录/文件清单。只为“已完成但仍挂手动身份锚”的
            # 少量任务补查详情，用于兼容清理旧版本留下的残留；单条失败不影响
            # 该下载器其余任务的实时列表。
            for torrent in torrents:
                info_hash = torrent.info_hash.lower()
                if not torrent.completed or info_hash not in manual:
                    continue
                try:
                    detail = await adapter.get_torrent(info_hash)
                except Exception as exc:  # noqa: BLE001 -- 对账增强失败时保守保留任务
                    logger.warning(
                        "读取已完成手动种子的文件清单失败，暂不清理身份锚："
                        "downloader=%s hash=%s error=%s",
                        row.name,
                        info_hash,
                        exc,
                    )
                    continue
                if detail is not None:
                    completed_manual[info_hash] = (row, detail)
        except Exception:  # noqa: BLE001 -- 单台失败必须降级，不能拖垮任务中心
            logger.exception("读取下载器「%s」的任务列表失败", row.name)
            return {
                **base,
                "status": "error",
                "message": "读取任务失败，请检查下载器连接",
            }, [], {}
        finally:
            if adapter is not None:
                try:
                    await adapter.close()
                except Exception:  # noqa: BLE001 -- 关闭失败不能覆盖本次来源快照
                    logger.warning("关闭下载器「%s」连接失败", row.name, exc_info=True)
        return {**base, "status": "active", "message": None}, torrents, completed_manual

    source_results = await asyncio.gather(*(read_source(row) for row in downloaders))
    completed_details = {
        info_hash: detail
        for _source, _torrents, details in source_results
        for info_hash, detail in details.items()
    }
    consumed_manual = await _reconcile_completed_manual_intents(
        session, manual, completed_details
    )
    for info_hash in consumed_manual:
        manual.pop(info_hash, None)
    items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for row, (source_view, torrents, _details) in zip(
        downloaders, source_results, strict=True
    ):
        visible_count = 0
        for index, torrent in enumerate(torrents):
            info_hash = torrent.info_hash.lower()
            linked_subscriptions = subscriptions.get(info_hash, [])
            linked_manual = manual.get(info_hash)
            # 做种历史可能有上千条：任务中心只看未完成任务，以及仍等待
            # MovieClaw 入库收尾的订阅/手动任务。
            if torrent.completed and not linked_subscriptions and not linked_manual:
                continue
            seen_hashes.add(info_hash)
            visible_count += 1
            items.append(
                _task_dict(
                    task_id=f"{row.id}:{info_hash or index}",
                    info_hash=info_hash,
                    torrent=torrent,
                    downloader=row,
                    subscriptions=linked_subscriptions,
                    manual=linked_manual,
                )
            )
        sources.append({**source_view, "task_count": visible_count})

    # 订阅已投递、但所有可用下载器都查不到时仍需提示：巡检稍后会把工单
    # 退回重新找源。手动下载意图只是入库身份锚，不是需要救援的工单；任务
    # 被明确删除后不应凭一条孤儿锚继续在任务中心显示“缺失”。
    for info_hash in sorted(set(subscriptions) - seen_hashes):
        linked_subscriptions = subscriptions.get(info_hash, [])
        linked_manual = manual.get(info_hash)
        items.append(
            _task_dict(
                task_id=f"missing:{info_hash}",
                info_hash=info_hash,
                torrent=None,
                downloader=None,
                subscriptions=linked_subscriptions,
                manual=linked_manual,
            )
        )

    state_order = {
        "error": 0,
        "missing": 0,
        "stalled": 1,
        "paused": 2,
        "downloading": 3,
        "unknown": 4,
        "completed": 5,
    }
    items.sort(key=lambda item: (state_order.get(item["state"], 9), item["name"] or ""))
    return {"items": items, "sources": sources}
