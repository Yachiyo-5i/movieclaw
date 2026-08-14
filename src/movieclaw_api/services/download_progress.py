"""投递救援巡检：照看订阅在途投递的种子，只救援、不搬运。

订阅止于投递（架构定稿）：投递记下 info_hash 后，下载完成的搬运由
监听导入（按 info_hash 认领身份）或库扫描（原地入账）完成，工单的
完成状态由库存对账关闭（wanted_fulfillment）。本任务只剩投递方
自己的责任——**照看投递结果的死活**：

- 种子在所有可用下载器中都查不到（被手动删除）→ 工单退回 wanted
  短冷却后重新找资源；
- 种子长期（STALLED_REQUEUE_DAYS）未完成 → 视为卡死，退回重新找
  资源（旧种子若之后完成，库存对账照样关闭工单，不冲突）；
- 种子已完成 → **落点核验**：把下载器上报的实际保存目录反向过路径
  映射翻译回 movieclaw 视角，本地看不到种子内容（超过宽限期）说明
  落进了 movieclaw 不可达的位置（映射缺失/卷未挂载/用户在下载器里
  移动了文件）——记一条中文告警活动（去重只记一次），不退回重找
  （数据真实存在，重找只会重复下载到同一个黑洞）；
- 种子已完成且落点可见 → **内容核验**：工单承诺的集数不在种子文件
  清单里（而清单明确认得出其他集数）→ 缺失部分退回重新找资源——
  全集/整季包的声明覆盖与物理内容不符时（真实案例：全集包被判定
  覆盖特别篇，实际一个 SP 文件都没有），工单不能挂在永远等不来的
  种子上，库存对账扫多少轮都关不掉它；
- 其余情况（下载中/已完成且落点可见待入库）不做任何事。

失败语义沿用：每组独立处理，单组失败不拖垮整轮，中文活动可回放。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.services.subscription import units_text
from movieclaw_api.services.system_notice import resolve_notices, upsert_notice
from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    ActivityType,
    MediaItem,
    Subscription,
    SubscriptionActivity,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.models.downloader_client import DownloaderClient
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_db.models.system_notice import NoticeSeverity
from movieclaw_db.repositories import SubscriptionRepository
from movieclaw_db.repositories.downloader_repo import DownloaderRepository
from movieclaw_downloader import DownloaderConfig, TorrentStatus, create_downloader
from movieclaw_media.models import MediaKind
from movieclaw_scheduler.registry import register_task

logger = logging.getLogger("movieclaw_api.download_progress")

# 巡检节奏：救援不追求秒级——5 分钟内发现"种子被删"足够灵敏
PROGRESS_TICK_SECONDS = 300

# 种子被手动删除后工单退回 wanted 的冷却（给用户留出"删错了重新添加"的窗口）
_MISSING_RETRY_MINUTES = 30

# 卡死判定：投递后超过该天数仍未下载完成，退回重新找资源（大体积慢速种子
# 也少有超过一周的；判错的代价只是多找一个候选，旧种子完成后照样入库）
STALLED_REQUEUE_DAYS = 7

_tick_lock = asyncio.Lock()

# 在途状态：GRABBED 为主；DOWNLOADED 是旧版管线的遗留中间态（新架构不再
# 写入），存量行按同样语义照看直至库存对账关闭
_IN_FLIGHT = (WantedStatus.GRABBED, WantedStatus.DOWNLOADED)


@register_task(
    "check_download_progress",
    title="投递救援巡检",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=PROGRESS_TICK_SECONDS,
    description=(
        "照看订阅在途投递的种子：被手动删除或长期卡死的工单退回重新找资源；"
        "已完成的核验落点与内容——movieclaw 看不到文件时在时间线告警，"
        "种子内容缺少承诺的集数时把缺失部分退回重新找资源。"
        "下载完成后的入库由监听导入/库扫描完成，工单由库存对账关闭。"
    ),
)
async def check_download_progress() -> None:
    async with _tick_lock:
        db = get_database()
        async with db.session() as session:
            groups = await _pipeline_groups(session)
            if not groups:
                return
            downloaders = await _usable_downloaders(session)
        if not downloaders:
            logger.warning("有 %d 个在途种子等待照看，但没有可用的下载器", len(groups))
            return
        for subscription_id, info_hash in groups:
            try:
                await _rescue_group(subscription_id, info_hash, downloaders)
            except Exception:  # noqa: BLE001 -- 单组失败不拖垮整轮
                logger.exception("种子 %s（订阅 #%s）的救援巡检失败", info_hash, subscription_id)


async def _pipeline_groups(
    session: AsyncSession,
) -> dict[tuple[int, str], list[WantedItem]]:
    """在途工单，按（订阅, 种子）分组。"""
    result = await session.execute(
        select(WantedItem).where(
            WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
            WantedItem.info_hash.is_not(None),  # type: ignore[union-attr]
        )
    )
    groups: dict[tuple[int, str], list[WantedItem]] = {}
    for row in result.scalars().all():
        assert row.info_hash is not None
        groups.setdefault((row.subscription_id, row.info_hash), []).append(row)
    return groups


async def _usable_downloaders(
    session: AsyncSession,
) -> list[tuple[DownloaderClient, DownloaderConfig]]:
    """全部可用（启用 + 连接验证通过）的下载器及其连接配置。"""
    repo = DownloaderRepository(session)
    rows = await repo.list_all()
    usable = []
    for row in rows:
        if not row.enabled or row.status != ConfigStatus.ACTIVE:
            continue
        usable.append(
            (
                row,
                DownloaderConfig(
                    type=row.client_type.value,
                    url=row.url,
                    username=row.username,
                    password=repo.decrypted_password(row),
                ),
            )
        )
    return usable


async def _query_torrent(
    info_hash: str,
    downloaders: list[tuple[DownloaderClient, DownloaderConfig]],
    *,
    include_files: bool = True,
) -> tuple[DownloaderClient, TorrentStatus] | None:
    """在全部可用下载器中查找种子（先到先得；单台故障不影响其余）。

    连同命中的下载器记录一起返回——落点核验需要它的路径映射做反向翻译。
    进度快照类的高频轮询传 ``include_files=False``，省掉文件清单的获取开销。
    """
    for row, config in downloaders:
        adapter = create_downloader(config)
        try:
            status = await adapter.get_torrent(info_hash, include_files=include_files)
        except Exception as exc:  # noqa: BLE001 -- 单台不可达降级继续
            logger.warning("查询下载器「%s」失败：%s", row.name, exc)
            continue
        finally:
            await adapter.close()
        if status is not None:
            return row, status
    return None


async def _rescue_group(
    subscription_id: int,
    info_hash: str,
    downloaders: list[tuple[DownloaderClient, DownloaderConfig]],
) -> None:
    found = await _query_torrent(info_hash, downloaders)
    downloader_row, status = found if found is not None else (None, None)
    db = get_database()
    async with db.session() as session:
        # 组内工单在会话内重取（库存对账可能刚关闭了其中一部分）
        rows = list(
            (
                await session.execute(
                    select(WantedItem).where(
                        WantedItem.subscription_id == subscription_id,
                        WantedItem.info_hash == info_hash,
                        WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return
        subscription = await session.get(Subscription, subscription_id)
        if subscription is None:
            return
        item = await session.get(MediaItem, subscription.media_item_id)
        assert item is not None  # 外键保证
        repo = SubscriptionRepository(session)

        if status is None:
            await _requeue(
                session,
                repo,
                item,
                rows,
                info_hash,
                message=(
                    f"投递的种子已不在下载器中（可能被手动删除），"
                    f"{_MISSING_RETRY_MINUTES} 分钟后重新寻找资源"
                ),
                reason="torrent_missing",
            )
            return

        if not status.completed and _stalled(rows):
            await _requeue(
                session,
                repo,
                item,
                rows,
                info_hash,
                message=(
                    f"「{status.name}」投递超过 {STALLED_REQUEUE_DAYS} 天仍未下载完成，"
                    "退回重新寻找资源（原种子保留在下载器中，完成后仍会自动入库）"
                ),
                reason="stalled",
            )
            return

        if status.completed:
            # 已完成：先核验落点（movieclaw 侧看不到内容 → 告警活动），落点
            # 可见再核验内容（承诺的集数不在文件清单里 → 缺失部分退回重找）。
            # 搬运仍归监听导入/库扫描，清单里存在的集数仍归库存对账关单
            assert downloader_row is not None  # found 非 None 时二者同源
            landed = await _verify_landing(session, repo, rows, info_hash, downloader_row, status)
            if landed:
                await _verify_content(session, repo, item, rows, info_hash, status)
            return

        logger.debug("《%s》的种子 %s：下载中", item.title, info_hash)


# 落点核验宽限期：完成后给下载器归位文件/网络盘可见性留出的窗口
_LANDING_GRACE_MINUTES = 10


async def _verify_landing(
    session: AsyncSession,
    repo: SubscriptionRepository,
    rows: list[WantedItem],
    info_hash: str,
    downloader: DownloaderClient,
    status,
) -> bool:
    """核验已完成种子的落点：movieclaw 侧看不到内容则记告警活动（去重）。

    判定：下载器上报的实际保存目录反向过路径映射翻译回 movieclaw 视角，
    在本地 stat 种子内容根（首个文件的顶层段，或任务名）。看不到就说明
    文件落在 movieclaw 不可达的位置——映射缺失/卷未挂载/被人工移动，
    监听导入和库扫描永远等不到它，必须把问题亮到时间线上。
    不退回重找：数据真实存在，重找只会重复下载到同一个黑洞。

    返回落点是否可见——只有确认可见（True）才有资格进入后续的内容核验；
    宽限期内或证据不全一律返回 False（本轮不判，不代表落点有问题）。
    """
    from movieclaw_api.services.torrent_submit import translate_to_local

    # 宽限期内不判：刚完成的种子可能还在归位（qB 临时目录搬移等）
    threshold = utcnow() - timedelta(minutes=_LANDING_GRACE_MINUTES)
    if any((w.grabbed_at or w.updated_at) > threshold for w in rows):
        return False

    local_dir = translate_to_local(status.save_path, downloader.path_mappings)
    root = status.files[0].path.split("/")[0] if status.files else status.name
    if not local_dir or not root:
        return False
    if (Path(local_dir) / root).exists():
        # 落点可见，等监听导入/库扫描接管即可；此前若报过"看不到"（映射
        # 刚修好/卷刚挂上），问题已消失，红灯就地熄灭
        await resolve_notices(
            session,
            dedupe_key=f"subscription.landing:{rows[0].subscription_id}:{info_hash}",
        )
        return True

    message = (
        f"「{status.name}」已下载完成，但 movieclaw 在 {local_dir} 看不到它——"
        f"下载器「{downloader.name}」可能无法访问该路径（路径映射缺失或卷未挂载），"
        "或文件已在下载器中被移动。请检查「设置 → 下载器」的路径映射，"
        "或改用监听导入规则后把文件移入监听目录"
    )
    # 全局红灯（幂等 upsert，问题在就常亮）：时间线活动埋在单个订阅详情页里，
    # 这类"不修就永远卡着"的错误必须在任何页面都能被感受到
    await upsert_notice(
        session,
        dedupe_key=f"subscription.landing:{rows[0].subscription_id}:{info_hash}",
        severity=NoticeSeverity.ERROR,
        source="subscription",
        title=f"「{status.name}」下载完成但无法入库",
        message=message,
        payload={"subscription_id": rows[0].subscription_id, "info_hash": info_hash},
    )

    # 去重：同一（订阅, 种子）只告警一次，避免每 5 分钟刷屏
    existing = (
        (
            await session.execute(
                select(SubscriptionActivity).where(
                    SubscriptionActivity.subscription_id == rows[0].subscription_id,
                    SubscriptionActivity.type == ActivityType.IMPORT_FAILED,
                )
            )
        )
        .scalars()
        .all()
    )
    for activity in existing:
        payload = activity.payload or {}
        if payload.get("info_hash") == info_hash and payload.get("reason") == "path_unreachable":
            return False

    await repo.add_activity(
        SubscriptionActivity(
            subscription_id=rows[0].subscription_id,
            wanted_item_id=rows[0].id,
            type=ActivityType.IMPORT_FAILED,
            message=message,
            payload={
                "info_hash": info_hash,
                "reason": "path_unreachable",
                "downloader_save_path": status.save_path,
                "local_dir": local_dir,
            },
        )
    )
    logger.warning(
        "落点核验失败：《种子 %s》完成于 %s（下载器视角 %s），movieclaw 侧不可见",
        info_hash,
        local_dir,
        status.save_path,
    )
    return False


# 种子内文件的季集声明：场景命名 SxxEyy。第二组抓整段集号串——连写
# （E05E06）与区间简写（E05-E08）都收进来，展开规则见 _observed_units。
# 裸数字段必须有分隔符引导（防止把 E05.1080p 的技术串吞进集号）
_FILE_UNIT_RE = re.compile(r"[Ss](\d{1,2})((?:[Ee]\d{1,4})(?:(?:\s*[-~&+]\s*[Ee]?|[Ee])\d{1,4})*)")
_FILE_EP_RE = re.compile(r"\d{1,4}")


def _observed_units(files) -> set[tuple[int, int]]:
    """种子文件清单里能认出的 (季, 集) 集合。

    只认场景命名的 SxxEyy 高置信声明；恰好两个递增集号视为区间展开
    （E05-E08 → 5..8，连写 E05E06 展开结果与列举一致），其余按列举。
    季集识别的完整口径在库扫描侧（NER 模型），这里刻意不复用——救援
    巡检要在无模型环境同样工作，而轻量正则的漏认只会让判定更保守
    （认不出 → 不动），不会造成误退。
    """
    observed: set[tuple[int, int]] = set()
    for file in files:
        for match in _FILE_UNIT_RE.finditer(file.path):
            season = int(match.group(1))
            numbers = [int(n) for n in _FILE_EP_RE.findall(match.group(2))]
            if len(numbers) == 2 and numbers[0] < numbers[1] <= numbers[0] + 200:
                numbers = list(range(numbers[0], numbers[1] + 1))
            observed.update((season, episode) for episode in numbers)
    return observed


async def _verify_content(
    session: AsyncSession,
    repo: SubscriptionRepository,
    item: MediaItem,
    rows: list[WantedItem],
    info_hash: str,
    status: TorrentStatus,
) -> None:
    """核验已完成种子的内容：承诺的集数不在文件清单里 → 缺失部分退回重找。

    库存对账只认领真实存在的文件——种子里根本没有的集，扫多少轮都不会
    入库，工单会以 grabbed 永远挂着，任务中心那条"等待入库"永不消失
    （真实案例：全集包被判定覆盖特别篇 7 集，62 个文件全是正剧）。

    判定刻意保守，证据不足一律不动：
    - 电影不判（单元没有集号语义）；
    - 文件清单为空（旧适配器不上报）不判；
    - 清单里一个集数都认不出（原盘目录/非常规命名）不判——只有清单
      "明确认得出集数、且明确没有这一集"时才断言缺失；
    - 只退回缺失的工单，清单里存在的集数继续等对账正常关单。
    """
    if MediaKind(item.kind) is MediaKind.MOVIE:
        return
    if not status.files:
        return
    observed = _observed_units(status.files)
    if not observed:
        return
    missing = [w for w in rows if (w.season_number, w.episode_number) not in observed]
    if not missing:
        return
    await _requeue(
        session,
        repo,
        item,
        missing,
        info_hash,
        message=(
            f"「{status.name}」已下载完成，但内容里没有 {units_text(missing)} 对应的"
            f"文件（声明的覆盖范围与实际内容不符），这部分退回重新寻找资源；"
            "种子内实际存在的集数不受影响，照常入库"
        ),
        reason="content_missing",
    )


async def subscription_download_snapshot(session: AsyncSession, subscription_id: int) -> list[dict]:
    """订阅详情页的实时下载进度快照。

    把该订阅的在途工单按种子分组，逐个到可用下载器里查当前状态（速度/ETA/
    进度），返回展示用字典列表。纯读操作：不落库、不改工单——工单的死活
    仍归上面的救援巡检管，这里只回答"此刻下到哪了"。

    种子在所有下载器中都查不到时 state="missing"（可能刚被手动删除，
    救援巡检稍后会把工单退回重找），前端据此给出解释而不是显示 0%。
    """
    result = await session.execute(
        select(WantedItem).where(
            WantedItem.subscription_id == subscription_id,
            WantedItem.status.in_(_IN_FLIGHT),  # type: ignore[attr-defined]
            WantedItem.info_hash.is_not(None),  # type: ignore[union-attr]
        )
    )
    groups: dict[str, list[WantedItem]] = {}
    for row in result.scalars().all():
        assert row.info_hash is not None
        groups.setdefault(row.info_hash, []).append(row)
    if not groups:
        return []

    downloaders = await _usable_downloaders(session)
    snapshots: list[dict] = []
    for info_hash, rows in sorted(groups.items()):
        units = [
            {"season_number": w.season_number, "episode_number": w.episode_number}
            for w in sorted(rows, key=lambda w: (w.season_number, w.episode_number))
        ]
        # 快照只用 info 字段（进度/速度/ETA），不取文件清单——5 秒一轮的
        # 轮询没必要每次都把整包文件列表拉回来
        found = (
            await _query_torrent(info_hash, downloaders, include_files=False)
            if downloaders
            else None
        )
        if found is None:
            snapshots.append(
                {
                    "info_hash": info_hash,
                    "name": None,
                    "progress": None,
                    "size_bytes": None,
                    "dlspeed_bytes": None,
                    "eta_seconds": None,
                    "state": "missing",
                    "downloader_name": None,
                    "units": units,
                }
            )
            continue
        downloader_row, status = found
        snapshots.append(
            {
                "info_hash": info_hash,
                "name": status.name,
                "progress": status.progress,
                "size_bytes": status.size_bytes,
                "dlspeed_bytes": status.dlspeed_bytes,
                "eta_seconds": status.eta_seconds,
                "state": status.state,
                "downloader_name": downloader_row.name,
                "units": units,
            }
        )
    return snapshots


def _stalled(rows: list[WantedItem]) -> bool:
    """整组工单是否已卡死：以最近一次状态推进的时间为基准。"""
    threshold = utcnow() - timedelta(days=STALLED_REQUEUE_DAYS)
    return all((w.grabbed_at or w.updated_at) < threshold for w in rows)


async def _requeue(
    session: AsyncSession,
    repo: SubscriptionRepository,
    item: MediaItem,
    rows: list[WantedItem],
    info_hash: str,
    *,
    message: str,
    reason: str,
) -> None:
    """把一组在途工单退回 wanted：冷却后重新找资源，记中文活动。"""
    now = utcnow()
    retry_at = now + timedelta(minutes=_MISSING_RETRY_MINUTES)
    for w in rows:
        await session.execute(
            update(WantedItem)
            .where(WantedItem.id == w.id)
            .values(
                status=WantedStatus.WANTED,
                info_hash=None,
                grabbed_at=None,
                downloaded_at=None,
                next_search_at=retry_at,
                updated_at=now,
            )
        )
    await session.commit()
    # 工单退回后旧种子的落点告警已无意义（重找会产生新种子/新告警）
    await resolve_notices(
        session, dedupe_key=f"subscription.landing:{rows[0].subscription_id}:{info_hash}"
    )
    await repo.add_activity(
        SubscriptionActivity(
            subscription_id=rows[0].subscription_id,
            wanted_item_id=rows[0].id,
            type=ActivityType.DISPATCH_FAILED,
            message=message,
            payload={"info_hash": info_hash, "reason": reason},
        )
    )
    logger.warning("《%s》的种子 %s 已退回队列：%s", item.title, info_hash, reason)
