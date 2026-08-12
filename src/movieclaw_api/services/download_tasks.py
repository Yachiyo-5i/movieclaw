"""任务中心的下载器实时聚合视图。

下载进度只存在于 qBittorrent / Transmission，本服务不复制第二份状态；每次
请求并行读取所有可用下载器的一次列表快照，再按 infohash 关联订阅工单与手动
下载意图。某台下载器不可达时只把该来源标为异常，其余来源仍正常返回。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.network_egress import effective_tmdb_image_base_url
from movieclaw_db.models import (
    ConfigStatus,
    DownloaderClient,
    ManualDownloadIntent,
    MediaItem,
    Subscription,
    WantedItem,
    WantedStatus,
)
from movieclaw_db.repositories.downloader_repo import DownloaderRepository
from movieclaw_downloader import DownloaderConfig, TorrentBrief, create_downloader

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
            "media_item_id": media.id,
            "media_title": media.title,
            "media_kind": media.kind,
            "poster_url": _poster_url(media),
        }
        for intent, media in manual_rows
    }
    return dict(subscriptions), manual


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


async def download_task_snapshot(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    """并行读取下载器，返回活跃下载与仍在业务管线内的完成/缺失任务。"""
    subscriptions, manual = await _relations(session)
    repository = DownloaderRepository(session)
    downloaders = await repository.list_all()

    async def read_source(row: DownloaderClient) -> tuple[dict[str, Any], list[TorrentBrief]]:
        assert row.id is not None
        base = {
            "id": row.id,
            "name": row.name,
            "client_type": row.client_type,
            "task_count": 0,
        }
        if not row.enabled:
            return {**base, "status": "disabled", "message": "下载器已停用"}, []
        if row.status != ConfigStatus.ACTIVE:
            message = row.last_error or "下载器尚未通过连接验证"
            return {**base, "status": "unavailable", "message": message}, []

        adapter = None
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
        except Exception:  # noqa: BLE001 -- 单台失败必须降级，不能拖垮任务中心
            logger.exception("读取下载器「%s」的任务列表失败", row.name)
            return {
                **base,
                "status": "error",
                "message": "读取任务失败，请检查下载器连接",
            }, []
        finally:
            if adapter is not None:
                try:
                    await adapter.close()
                except Exception:  # noqa: BLE001 -- 关闭失败不能覆盖本次来源快照
                    logger.warning("关闭下载器「%s」连接失败", row.name, exc_info=True)
        return {**base, "status": "active", "message": None}, torrents

    source_results = await asyncio.gather(*(read_source(row) for row in downloaders))
    items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for row, (source_view, torrents) in zip(downloaders, source_results, strict=True):
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

    # 订阅/手动意图已经投递、但所有可用下载器都查不到时，仍然是需要关注的
    # 任务，不能因为外部事实源缺行就在全局视图里消失。
    for info_hash in sorted((set(subscriptions) | set(manual)) - seen_hashes):
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
