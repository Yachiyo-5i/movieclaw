"""自动刷分享率引擎：盯本地索引的免费种第一时间抢下做种，预算内自动汰换。

完整设计见 docs/design/site-protection-ratio-boost.md。每 tick 三步：

1. **对账**：从每台涉及的下载器 ``list_torrents`` 读一次全量（无逐任务请求），
   按 infohash 对回 ``ratio_boost_task`` 台账——累计上传量差分出上传速度 EMA，
   台账里有、下载器里没有的标记 missing（用户手动删除是明确意图，只让出
   预算，不追究不抢回）。
2. **预算收敛**：某站在池占用超过预算（用户调小了预算）时，按汰换规则删
   低效任务，直到回到预算内或没有可汰换的为止。
3. **准入**：扫描各开启刷流站点的免费新种，评分排序后在预算内提交；预算
   不够时先尝试汰换腾位，腾不出就放弃候选。

安全约束（比效率更重要，任何改动不得破坏）：

- **只碰自己的任务**：提交时发现种子已在下载器中（``already_exists``）的
  绝不入台账管理，与订阅管线 ``owned_by_movieclaw`` 同一哲学；
- **绝不制造 H&R**：候选排除明确标注 H&R 的种子；72 小时最低保留期兜底
  多数站点不提供 H&R 标记（三态 NULL）的情况——保留期内的任务在任何
  预算压力下都不会被删；
- **免费窗口内下得完才抢**：免费期过后继续下载会产生真实下载量，反而
  伤害分享率，宁可放弃。
"""

from __future__ import annotations

import contextlib
import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.engine import get_database
from movieclaw_db.models import (
    BoostTaskState,
    DownloaderClient,
    ManualDownloadIntent,
    RatioBoostStat,
    RatioBoostTask,
    SiteCredential,
    SiteTorrent,
    SubscriptionDownloadAttempt,
    utcnow,
)
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_db.repositories.downloader_repo import DownloaderRepository
from movieclaw_downloader.base import BaseDownloader
from movieclaw_downloader.factory import create_downloader
from movieclaw_downloader.models import DownloaderConfig, TorrentBrief
from movieclaw_scheduler.registry import register_task

logger = logging.getLogger("movieclaw_api.ratio_boost")

# ---------------------------------------------------------------------------
# 引擎参数（docs/design/site-protection-ratio-boost.md 第 2 节）
# ---------------------------------------------------------------------------

_TICK_SECONDS = 300
# 候选窗口：只抢发布 24 小时内的免费种（新种 peer 群最活跃，上传机会最大）
_FRESH_WINDOW = timedelta(hours=24)
# 促销观测新鲜度：免费/做种数来自索引快照，观测太旧时促销可能已结束，
# 抢一个"其实已不免费"的种子会产生真实下载量——宁可等下一轮同步再看
_VOLATILE_FRESHNESS = timedelta(hours=2)
# 止损：免费窗口已过仍未下完（剩余超过 1/10）→ 继续下就是付费下载，放弃；
# 下载 48 小时仍未完成 → 死种，放弃让出预算（二者都是未完成任务，
# 不受 72 小时保留期约束——保留期保护的是已完成的做种）
_ABANDON_REMAINING_FRACTION = 0.1
_STUCK_AFTER = timedelta(hours=48)
# 免费窗口安全垫：按保守下载速度估算下载时长，窗口不足即放弃
_ASSUMED_DL_SPEED = 5 * 1024 * 1024  # 5 MiB/s
_MIN_FREE_MARGIN = timedelta(hours=2)
# 汰换三条件：下载完成 + 入池满 72 小时（H&R 安全垫）+ 周转太慢（见下）
_MIN_HOLD = timedelta(hours=72)
# 效率的衡量单位是「周转」= 单位存储的上传速度（rate/size）。预算约束的是
# 存储×时间，最大化总上传是个背包问题，按密度（rate/size）贪心保留/汰换
# 即近似最优——绝对速度会留错资产：200 GiB 跑 10 KiB/s（周转 240 天）远差于
# 2 GiB 跑 8 KiB/s（周转 3 天）。地板：10 天内传不出自己体积一遍的可替换
_EVICT_TURNOVER_DAYS = 10
# 上传速度 EMA 的时间窗口：PT 上传以「天」为周期突发（晚高峰猛、白天静），
# 短窗口会把昨晚狂传的种子在今天下午误判成死种。24 小时窗配合 72 小时
# 保留期，汰换判断建立在约 3 天的公平测量上。α 按时距计算（1-e^(-dt/W)），
# tick 间隔漂移也不影响窗口语义
_EMA_WINDOW_SECONDS = 24 * 3600
# 单种体积上限 = 预算的 1/4：单种吃掉大半预算会让汰换失去弹性
_MAX_SIZE_BUDGET_FRACTION = 4
# 每站每 tick 最多提交数：对站点保持克制
_MAX_ADMIT_PER_TICK = 3
# 提交后宽限：刚提交的任务可能还没出现在下载器列表里，不能立刻判 missing
_MISSING_GRACE = timedelta(minutes=10)
# 最小准入余量：余量（剩余预算 + 可汰换容量）低于此值时视为"没有能力接新种"——
# 准入扫描跳过，索引同步也回落到正常自适应节奏（值得抢的免费种极少小于 1 GiB）
_MIN_ADMISSION_HEADROOM = 1024**3
# 余量恢复后推同步一把的阈值：游标排期还在 15 分钟开外才值得打断
#（刚回落不久/本来就快的排期没必要动）
_NUDGE_THRESHOLD = timedelta(minutes=15)
# 小时桶统计的保留时长：展示窗口最长 7 天，30 天留足余量后清理
_STAT_RETENTION = timedelta(days=30)
# 刷流任务的下载器分类与保存子目录：与媒体下载（movieclaw）彻底隔离，
# 不落媒体库、不进监听导入规则的视野
BOOST_CATEGORY = "movieclaw-boost"
BOOST_TAG = "movieclaw-boost"
_BOOST_SUBDIR = "movieclaw-boost"


# ---------------------------------------------------------------------------
# 纯决策函数（无 IO，单测覆盖）
# ---------------------------------------------------------------------------


def free_window_sufficient(
    size_bytes: int, free_deadline: datetime | None, now: datetime
) -> bool:
    """免费窗口是否足够把种子下完（含安全垫）。

    deadline 为 NULL 表示"无促销截止/长期免费/未知"——索引层已把 M-Team
    长期免费哨兵归一为 NULL，且候选查询以 is_free=True 为前提，此处 NULL
    按"窗口充足"处理。
    """
    if free_deadline is None:
        return True
    required = max(_MIN_FREE_MARGIN, timedelta(seconds=size_bytes / _ASSUMED_DL_SPEED))
    return free_deadline - now >= required


def assess_candidate(
    row: SiteTorrent,
    *,
    now: datetime,
    budget_bytes: int,
    tracked_torrent_ids: set[str],
) -> tuple[bool, float]:
    """评估一个索引行是否值得抢，返回（是否合格, 评分）。

    评分 = leechers / (seeders + 1) × 上传系数：分数高 = 供不应求 =
    上传效率预期高；2x 上传的种子同样的流量记双倍，评分翻倍。

    评分刻意**不除以体积**：每个下载者都要下完整个体积，剩余需求字节
    ≈ leechers × size，做种者份额 ≈ L×size/(S+1)——除以占用的 size 后，
    每字节期望回报 ≈ L/(S+1)，体积恰好消掉。小体积的真正优势是敏捷性
    （下得快、免费窗口风险小、汰换颗粒度细），由准入排序的同分决胜体现
    （见 _admit_candidates），不在评分里重复计价。
    SQL 侧已做粗筛，这里是完整判据（防御性重复 + 可单测）。
    """
    if row.torrent_id in tracked_torrent_ids:
        return False, 0.0  # 抢过的不再抢（含已汰换的：证明过效率低）
    if not row.download_url:
        return False, 0.0
    if row.size_bytes is None or row.size_bytes <= 0:
        return False, 0.0
    if row.size_bytes > budget_bytes // _MAX_SIZE_BUDGET_FRACTION:
        return False, 0.0
    if row.is_free is not True:
        return False, 0.0
    if row.hit_and_run is True:
        return False, 0.0  # 明确标注 H&R 考核的绝不碰
    if not row.leechers or row.leechers < 1:
        return False, 0.0  # 没有下载者就没有上传对象
    if row.publish_time is None or now - row.publish_time > _FRESH_WINDOW:
        return False, 0.0
    if (
        row.volatile_refreshed_at is None
        or now - row.volatile_refreshed_at > _VOLATILE_FRESHNESS
    ):
        return False, 0.0  # 促销观测太旧，等同步刷新后再评估
    if not free_window_sufficient(row.size_bytes, row.free_deadline, now):
        return False, 0.0
    score = row.leechers / ((row.seeders or 0) + 1)
    if row.upload_volume_factor and row.upload_volume_factor > 1.0:
        score *= row.upload_volume_factor
    return True, score


def apply_observation(
    task: RatioBoostTask,
    *,
    uploaded_bytes: int | None,
    completed: bool,
    now: datetime,
) -> None:
    """把下载器的一次观测写回台账：完成位 + 上传量差分 → 上传速度 EMA。

    uploaded_bytes 为 None（旧适配器不提供）时只更新完成位，绝不能当 0
    参与差分——会把 EMA 错误打到 0 触发误汰换。
    差分为负说明下载器重建过任务（重新校验/换实例），重置基线不更新 EMA。
    """
    task.completed = task.completed or completed
    if uploaded_bytes is not None:
        baseline_at = task.last_checked_at or task.created_at
        dt = (now - baseline_at).total_seconds()
        delta = uploaded_bytes - task.uploaded_bytes
        if dt > 0 and delta >= 0:
            rate = delta / dt
            # 时距感知 EMA：等效 24 小时观测窗口（见 _EMA_WINDOW_SECONDS）
            alpha = 1 - math.exp(-dt / _EMA_WINDOW_SECONDS)
            task.upload_rate_ema = alpha * rate + (1 - alpha) * task.upload_rate_ema
        task.uploaded_bytes = max(uploaded_bytes, 0)
    task.last_checked_at = now
    task.updated_at = now


def hand_over_if_claimed(
    task: RatioBoostTask, claimed_hashes: set[str], now: datetime
) -> bool:
    """种子被订阅投递/手动下载认领时，把刷流任务转出管理。返回是否发生转出。

    与订阅的碰撞是双向的，这里处理「刷流先抢、订阅后到」的方向：订阅投递
    命中同一 infohash 时下载器幂等返回 already_exists，工单照常记账——此后
    这份数据是订阅的依赖，刷流**绝不能再把它连数据汰换掉**。转出 = 置
    MISSING 让出预算、任务与数据原样保留，之后归订阅的所有权/H&R 状态机
    管辖（反方向「订阅先抢、刷流后到」由准入排除 + already_exists 双重拦截）。
    """
    if task.state != BoostTaskState.ACTIVE or task.info_hash not in claimed_hashes:
        return False
    task.state = BoostTaskState.MISSING
    task.evicted_at = now
    task.evict_reason = "已被订阅/手动下载认领，移出刷流管理（预算让出，任务与数据保留）"
    task.updated_at = now
    return True


def stop_loss_reason(task: RatioBoostTask, progress: float, now: datetime) -> str | None:
    """未完成任务的止损判定：该放弃则返回中文原因，否则 None。

    与汰换（针对已完成的做种）不同，止损针对**下载中**的任务，不受 72 小时
    保留期约束——多数站点的 H&R 考核针对已完成下载，未完成任务放弃的风险
    远小于"免费窗口过后继续付费下载"的确定伤害：

    - 免费窗口已过、剩余还超过 1/10：每多下一字节都是付费流量，删；
      已下到 9 成以上则放行下完（删了全白费，剩余付费量很小）；
    - 下载 48 小时仍未完成：死种/无源，永远占着预算，删。
    """
    if task.completed:
        return None
    if (
        task.free_deadline is not None
        and now > task.free_deadline
        and (1.0 - progress) > _ABANDON_REMAINING_FRACTION
    ):
        return f"免费窗口已过仍未下完（进度 {progress:.0%}），止损放弃避免付费下载"
    if now - task.created_at >= _STUCK_AFTER:
        return "下载 48 小时仍未完成（死种或无可用资源），放弃让出预算"
    return None


def turnover_seconds(task: RatioBoostTask) -> float:
    """周转周期：按当前上传 EMA，把自己的体积再上传一遍需要多少秒。

    这是刷流效率的统一度量——「这块存储隔多久为分享率贡献一次自己的大小」。
    EMA 为 0（完全没人要）时返回无穷大。
    """
    if task.upload_rate_ema <= 0:
        return math.inf
    return task.size_bytes / task.upload_rate_ema


def evictable(task: RatioBoostTask, now: datetime) -> bool:
    """任务是否可被汰换：下载完成 + 过了最低保留期 + 周转太慢。

    「周转太慢」= 按当前上传 EMA，10 天都传不出自己体积的一遍（rate/size
    密度地板）。用周转而非绝对速度：预算约束的是存储×时间，留下的应该是
    单位存储产出高的资产——大种子必须跑出与体积相称的速度才配占着位置。

    72 小时保留期是 H&R 的安全垫（候选虽排除了明确 H&R，但多数站点不提供
    标记），任何预算压力下都不能绕过。
    """
    return (
        task.state == BoostTaskState.ACTIVE
        and task.completed
        and now - task.created_at >= _MIN_HOLD
        and turnover_seconds(task) > _EVICT_TURNOVER_DAYS * 86400
    )


def admission_headroom(
    tasks: list[RatioBoostTask], budget_bytes: int, now: datetime
) -> int:
    """当前的准入余量 = 剩余预算 + 可汰换任务的占用。

    这是"刷流还有没有能力接新种"的统一判据：预算满但有低效老种可换时，
    发现新种仍有意义（会触发汰换腾位）；满且换不动时，发现了也下不了——
    准入扫描与索引同步的快节奏（wants_fast_sync）都以它为开关。
    """
    used = sum(t.size_bytes for t in tasks if t.state == BoostTaskState.ACTIVE)
    reclaimable = sum(t.size_bytes for t in tasks if evictable(t, now))
    return budget_bytes - used + reclaimable


def pick_evictions(
    tasks: list[RatioBoostTask], need_bytes: int, now: datetime
) -> list[RatioBoostTask] | None:
    """从可汰换任务里按周转从慢到快（单位存储产出从低到高）挑出足够腾出
    need_bytes 的一批。

    腾不够返回 None——调用方放弃准入，**绝不提前动保留期内的任务**。
    """
    candidates = sorted(
        (t for t in tasks if evictable(t, now)), key=turnover_seconds, reverse=True
    )
    picked: list[RatioBoostTask] = []
    freed = 0
    for task in candidates:
        if freed >= need_bytes:
            break
        picked.append(task)
        freed += task.size_bytes
    return picked if freed >= need_bytes else None


# ---------------------------------------------------------------------------
# 引擎主体
# ---------------------------------------------------------------------------


class _DownloaderPool:
    """tick 内的下载器连接池：每台只建一次连接、读一次全量列表，tick 末统一关闭。

    某台不可达时记为 None——该台上的任务本 tick 跳过对账，也不参与汰换
    （删不掉的任务不能被"计划汰换"，否则预算账会虚假平衡）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._adapters: dict[int, BaseDownloader | None] = {}
        self._briefs: dict[int, dict[str, TorrentBrief] | None] = {}

    async def adapter(self, downloader_id: int) -> BaseDownloader | None:
        if downloader_id in self._adapters:
            return self._adapters[downloader_id]
        row = await self._session.get(DownloaderClient, downloader_id)
        adapter: BaseDownloader | None = None
        if row is not None:
            try:
                repo = DownloaderRepository(self._session)
                adapter = create_downloader(
                    DownloaderConfig(
                        type=row.client_type.value,
                        url=row.url,
                        username=row.username,
                        password=repo.decrypted_password(row),
                    )
                )
            except Exception:  # noqa: BLE001 -- 单台下载器故障不拖垮整个 tick
                logger.warning("刷流：连接下载器 #%d 失败", downloader_id, exc_info=True)
                adapter = None
        self._adapters[downloader_id] = adapter
        return adapter

    async def briefs(self, downloader_id: int) -> dict[str, TorrentBrief] | None:
        """该下载器的 infohash → TorrentBrief 映射；不可达返回 None。"""
        if downloader_id in self._briefs:
            return self._briefs[downloader_id]
        adapter = await self.adapter(downloader_id)
        mapping: dict[str, TorrentBrief] | None = None
        if adapter is not None:
            try:
                mapping = {b.info_hash: b for b in await adapter.list_torrents()}
            except Exception as exc:  # noqa: BLE001
                logger.warning("刷流：读取下载器 #%d 任务列表失败：%s", downloader_id, exc)
        self._briefs[downloader_id] = mapping
        return mapping

    async def close(self) -> None:
        for adapter in self._adapters.values():
            if adapter is not None:
                with contextlib.suppress(Exception):
                    await adapter.close()


async def _claimed_hashes(session: AsyncSession) -> set[str]:
    """被订阅投递或手动下载认领的全部 infohash（统一小写）。"""
    attempt_rows = (
        (await session.execute(select(SubscriptionDownloadAttempt.info_hash))).scalars().all()
    )
    intent_rows = (await session.execute(select(ManualDownloadIntent.info_hash))).scalars().all()
    return {h.lower() for h in [*attempt_rows, *intent_rows] if h}


async def _refresh_tasks(
    session: AsyncSession, pool: _DownloaderPool, tasks: list[RatioBoostTask], now: datetime
) -> dict[str, int]:
    """第一步对账：把下载器观测写回台账，找不到的标记 missing。
    返回本 tick 各站点的上传增量（site_id → 字节），供小时桶统计累加。

    对账前先做认领转出（见 hand_over_if_claimed）：被订阅/手动下载认领的
    任务立即脱离刷流管理，从根上杜绝后续任何汰换触碰它的数据。
    """
    deltas: dict[str, int] = {}
    claimed = await _claimed_hashes(session)
    for task in tasks:
        if hand_over_if_claimed(task, claimed, now):
            logger.info("刷流任务已被认领，转出管理：%s（%s）", task.title, task.site_id)
            continue
        if task.downloader_id is None:
            # 下载器配置已被删除（外键 SET NULL）：任务无从管理，让出预算
            task.state = BoostTaskState.MISSING
            task.evicted_at = now
            task.evict_reason = "下载器配置已删除，任务脱离管理"
            continue
        mapping = await pool.briefs(task.downloader_id)
        if mapping is None:
            continue  # 下载器不可达：本 tick 跳过，不能误判 missing
        brief = mapping.get(task.info_hash)
        if brief is None:
            if now - task.created_at < _MISSING_GRACE:
                continue  # 刚提交的任务可能还没出现在列表里
            task.state = BoostTaskState.MISSING
            task.evicted_at = now
            task.evict_reason = "任务已从下载器中消失（通常是用户手动删除），让出刷流预算"
            logger.info("刷流任务失踪，让出预算：%s（%s）", task.title, task.site_id)
            continue
        uploaded_before = task.uploaded_bytes
        apply_observation(
            task, uploaded_bytes=brief.uploaded_bytes, completed=brief.completed, now=now
        )
        # 上传增量按站点归集（下载器重建导致的负差分已在 apply_observation 归零基线）
        gained = max(0, task.uploaded_bytes - uploaded_before)
        if gained:
            deltas[task.site_id] = deltas.get(task.site_id, 0) + gained
        # 未完成任务的止损：免费窗口过期 / 长期卡死 → 连数据删除，让出预算
        reason = stop_loss_reason(task, brief.progress or 0.0, now)
        if reason is not None:
            await _evict(pool, task, now, reason=reason)
    await session.commit()
    return deltas


async def _evict(pool: _DownloaderPool, task: RatioBoostTask, now: datetime, reason: str) -> bool:
    """执行一次汰换：从下载器连数据一起删，台账置 EVICTED。失败返回 False。

    下载器缺失/不可达时返回 False——删不掉的任务不能记成已汰换，否则预算账
    会虚假平衡（对账步骤会另行处理 downloader_id 为 None 的脱管任务）。
    """
    if task.downloader_id is None:
        return False
    adapter = await pool.adapter(task.downloader_id)
    if adapter is None:
        return False
    try:
        await adapter.delete_torrent(task.info_hash, delete_files=True)
    except Exception as exc:  # noqa: BLE001 -- 删除失败不拖垮 tick，下轮再试
        logger.warning("刷流汰换删除失败（%s）：%s", task.title, exc)
        return False
    task.state = BoostTaskState.EVICTED
    task.evicted_at = now
    task.evict_reason = reason
    task.updated_at = now
    turnover = turnover_seconds(task)
    logger.info(
        "已汰换刷流任务：%s（%s，占用 %.1f GiB，上传 EMA %.1f KiB/s，周转约 %s）",
        task.title,
        task.site_id,
        task.size_bytes / 1024**3,
        task.upload_rate_ema / 1024,
        "∞" if math.isinf(turnover) else f"{turnover / 86400:.1f} 天",
    )
    return True


async def _default_downloader(session: AsyncSession) -> DownloaderClient | None:
    """取默认且可用的下载器（与 torrent_submit 同判据），供准入提交。"""
    result = await session.execute(
        select(DownloaderClient).where(
            DownloaderClient.is_default.is_(True),  # type: ignore[attr-defined]
            DownloaderClient.enabled.is_(True),  # type: ignore[attr-defined]
            DownloaderClient.status == ConfigStatus.ACTIVE,
        )
    )
    return result.scalars().first()


def _boost_save_path(downloader: DownloaderClient) -> str | None:
    """刷流保存目录：下载器默认目录下的专属子目录；未配置默认目录则交给
    下载器自身默认（此时无法建子目录，靠分类区分）。"""
    if not downloader.save_path:
        return None
    return downloader.save_path.rstrip("/") + "/" + _BOOST_SUBDIR


async def _record_stats(
    session: AsyncSession,
    *,
    deltas: dict[str, int],
    used_by_site: dict[str, int],
    now: datetime,
) -> None:
    """把本 tick 的观测累进小时桶：上传增量 + 在池占用采样。

    覆盖「有在池任务 ∪ 有上传增量」的站点——占用采样必须每 tick 都记
    （平均在池体积 = 采样和 / 采样数），不能只在有上传时记，否则安静时段
    会把平均值虚高。顺手清理超过保留期的旧桶。
    """
    sites = set(used_by_site) | set(deltas)
    if not sites:
        return
    bucket = now.replace(minute=0, second=0, microsecond=0)
    for site_id in sites:
        row = (
            await session.execute(
                select(RatioBoostStat).where(
                    RatioBoostStat.site_id == site_id,
                    RatioBoostStat.bucket_start == bucket,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = RatioBoostStat(site_id=site_id, bucket_start=bucket)
            session.add(row)
        row.uploaded_bytes += deltas.get(site_id, 0)
        row.used_bytes_sum += used_by_site.get(site_id, 0)
        row.tick_count += 1
        row.updated_at = now
    await session.execute(
        delete(RatioBoostStat).where(
            RatioBoostStat.bucket_start < now - _STAT_RETENTION  # type: ignore[arg-type]
        )
    )
    await session.commit()


async def _nudge_slow_cursor(session: AsyncSession, site_id: str, now: datetime) -> None:
    """准入余量恢复后，把还在慢排期的同步游标标记到期，发现速度立刻回升。

    无余量期间同步回落到自适应节奏（可能已放疏到小时级）；余量恢复的信号
    只有刷流引擎自己知道，不推这一把就要等旧排期走完才重新钉住。排期在
    15 分钟内的不动——马上就要同步了，没必要打断。
    """
    from movieclaw_db.repositories.torrent_repo import TorrentRepository

    repo = TorrentRepository(session)
    cursor = await repo.get_cursor(site_id)
    if cursor is not None and cursor.next_sync_at is not None and (
        cursor.next_sync_at - now > _NUDGE_THRESHOLD
    ):
        await repo.expire_cursor(site_id)
        logger.info("刷流余量恢复，站点 %s 的索引同步已提前", site_id)


async def _admit_candidates(
    session: AsyncSession,
    pool: _DownloaderPool,
    cred: SiteCredential,
    site_tasks: list[RatioBoostTask],
    now: datetime,
) -> None:
    """第三步准入：扫描该站免费新种，评分排序后在预算内提交。"""
    from movieclaw_api.services.torrent_submit import submit_torrent

    downloader = await _default_downloader(session)
    if downloader is None:
        logger.debug("刷流：没有可用的默认下载器，站点 %s 本轮不准入", cred.site_id)
        return

    site_id = cred.site_id
    budget = cred.boost_budget_bytes
    used = sum(t.size_bytes for t in site_tasks if t.state == BoostTaskState.ACTIVE)

    # 余量开关：池子满且换不动时不扫候选（发现了也下不了）；同步端由
    # wants_fast_sync 同一判据回落节奏。有余量时若游标还在慢排期（此前
    # 无余量回落遗留的），推一把让发现速度立刻恢复
    if admission_headroom(site_tasks, budget, now) < _MIN_ADMISSION_HEADROOM:
        return
    await _nudge_slow_cursor(session, site_id, now)

    # 抢过的不再抢（任何状态都算：missing/evicted 是明确结论，不能反复拉扯）
    tracked = set(
        (
            await session.execute(
                select(RatioBoostTask.torrent_id).where(RatioBoostTask.site_id == site_id)
            )
        )
        .scalars()
        .all()
    )
    # 订阅投递 / 手动下载已认领的同站种子也不抢：那是媒体下载的领地，刷流
    # 抢过去只会制造 already_exists 空转（「订阅先抢、刷流后到」方向的拦截）
    for model in (SubscriptionDownloadAttempt, ManualDownloadIntent):
        tracked.update(
            (
                await session.execute(
                    select(model.torrent_id).where(
                        model.site_id == site_id,
                        model.torrent_id != None,  # noqa: E711 -- SQL 表达式需用 !=
                    )
                )
            )
            .scalars()
            .all()
        )

    # SQL 粗筛（免费 + 新发布 + 有下载者 + 非明确 H&R），完整判据在 assess_candidate
    rows = (
        (
            await session.execute(
                select(SiteTorrent)
                .where(
                    SiteTorrent.site_id == site_id,
                    SiteTorrent.is_free == True,  # noqa: E712 -- SQL 表达式需用 ==
                    SiteTorrent.publish_time != None,  # noqa: E711
                    SiteTorrent.publish_time >= now - _FRESH_WINDOW,  # type: ignore[operator]
                    SiteTorrent.leechers != None,  # noqa: E711
                    SiteTorrent.leechers >= 1,  # type: ignore[operator]
                    SiteTorrent.size_bytes != None,  # noqa: E711
                    or_(
                        SiteTorrent.hit_and_run == None,  # noqa: E711
                        SiteTorrent.hit_and_run == False,  # noqa: E712
                    ),
                )
                .order_by(SiteTorrent.publish_time.desc())  # type: ignore[union-attr]
                .limit(200)
            )
        )
        .scalars()
        .all()
    )

    scored: list[tuple[float, SiteTorrent]] = []
    for row in rows:
        ok, score = assess_candidate(
            row, now=now, budget_bytes=budget, tracked_torrent_ids=tracked
        )
        if ok:
            scored.append((score, row))
    # 评分降序；同分小体积优先——每字节期望回报与体积近似无关（见
    # assess_candidate），小的下得快、免费窗口风险小、汰换颗粒度细
    scored.sort(key=lambda pair: (-pair[0], pair[1].size_bytes or 0))

    admitted = 0
    save_path = _boost_save_path(downloader)
    for score, row in scored:
        if admitted >= _MAX_ADMIT_PER_TICK:
            break
        size = row.size_bytes or 0
        space = budget - used
        if size > space:
            # 预算不够：尝试汰换低效任务腾位；腾不出就看下一个（更小的）候选
            plan = pick_evictions(site_tasks, size - space, now)
            if plan is None:
                continue
            for victim in plan:
                if await _evict(
                    pool, victim, now, reason="为更高效的新免费种腾出预算（上传效率过低）"
                ):
                    used -= victim.size_bytes
            if size > budget - used:
                continue  # 部分删除失败，位置仍不够
        try:
            result, _ = await submit_torrent(
                session,
                site_id=site_id,
                download_url=row.download_url,
                tags=[BOOST_TAG],
                save_path=save_path,
                downloader_id=downloader.id,
                category=BOOST_CATEGORY,
            )
        except Exception as exc:  # noqa: BLE001
            # 站点/下载器/配置任一环节失败：本站本轮停止准入，下轮重试。
            # 失败原因已是可读中文（AppException 约定），直接进日志。
            logger.warning("刷流准入提交失败（站点 %s）：%s", site_id, exc)
            break
        if not result.info_hash:
            continue  # 极罕见：无法解析 infohash 的种子无法追踪，放弃
        if result.already_exists:
            # 所有权铁律：用户自己已在下的种子绝不纳入管理（永不自动删除）。
            # 台账记一条 missing 终态防止每 tick 重复抢，不占预算不计统计。
            session.add(
                RatioBoostTask(
                    site_id=site_id,
                    torrent_id=row.torrent_id,
                    info_hash=result.info_hash.lower(),
                    downloader_id=downloader.id,
                    title=row.title,
                    size_bytes=size,
                    state=BoostTaskState.MISSING,
                    evicted_at=now,
                    evict_reason="提交时任务已在下载器中（非刷流所有，不纳入管理）",
                )
            )
            await session.commit()
            continue
        task = RatioBoostTask(
            site_id=site_id,
            torrent_id=row.torrent_id,
            info_hash=result.info_hash.lower(),
            downloader_id=downloader.id,
            title=row.title,
            size_bytes=size,
            free_deadline=row.free_deadline,
        )
        session.add(task)
        await session.commit()
        site_tasks.append(task)
        used += size
        admitted += 1
        logger.info(
            "刷流已抢下免费种：%s（%s，%.1f GiB，评分 %.2f，已用 %.1f/%.1f GiB）",
            row.title,
            site_id,
            size / 1024**3,
            score,
            used / 1024**3,
            budget / 1024**3,
        )
    await session.commit()


@register_task(
    "ratio_boost",
    title="自动刷分享率",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=_TICK_SECONDS,
    description=(
        "盯各开启刷流站点的免费新种，第一时间抢下做种提高分享率；"
        "在站点各自的存储预算内自动汰换上传效率低的任务。"
    ),
)
async def run_ratio_boost() -> None:
    """tick 任务体：对账 → 预算收敛 → 准入。没有刷流站点且无在池任务时零成本返回。"""
    now = utcnow()
    async with get_database().session() as session:
        boost_creds = (
            (
                await session.execute(
                    select(SiteCredential).where(
                        SiteCredential.enabled == True,  # noqa: E712
                        SiteCredential.status == ConfigStatus.ACTIVE,
                        SiteCredential.boost_enabled == True,  # noqa: E712
                    )
                )
            )
            .scalars()
            .all()
        )
        active_tasks = (
            (
                await session.execute(
                    select(RatioBoostTask).where(RatioBoostTask.state == BoostTaskState.ACTIVE)
                )
            )
            .scalars()
            .all()
        )
        if not boost_creds and not active_tasks:
            return

        pool = _DownloaderPool(session)
        try:
            # ① 对账：即使站点已关掉刷流，在池任务仍继续追踪（它们还在做种）
            deltas = await _refresh_tasks(session, pool, list(active_tasks), now)
            # 小时桶统计：上传增量 + 在池占用采样（转出/汰换后的任务不计占用）
            used_by_site: dict[str, int] = {}
            for t in active_tasks:
                if t.state == BoostTaskState.ACTIVE:
                    used_by_site[t.site_id] = used_by_site.get(t.site_id, 0) + t.size_bytes
            await _record_stats(session, deltas=deltas, used_by_site=used_by_site, now=now)

            for cred in boost_creds:
                site_tasks = [
                    t
                    for t in active_tasks
                    if t.site_id == cred.site_id and t.state == BoostTaskState.ACTIVE
                ]
                # ② 预算收敛：用户调小预算后逐步退到预算内（绝不动保留期内任务）
                used = sum(t.size_bytes for t in site_tasks)
                if used > cred.boost_budget_bytes:
                    # 周转最慢（单位存储产出最低）的先走
                    for victim in sorted(
                        (t for t in site_tasks if evictable(t, now)),
                        key=turnover_seconds,
                        reverse=True,
                    ):
                        if used <= cred.boost_budget_bytes:
                            break
                        if await _evict(pool, victim, now, reason="预算调小后收敛（上传效率过低）"):
                            used -= victim.size_bytes
                    await session.commit()
                # ③ 准入
                await _admit_candidates(session, pool, cred, site_tasks, now)
        finally:
            await pool.close()


async def wants_fast_sync(session: AsyncSession, cred: SiteCredential) -> bool:
    """站点索引同步是否应钉在最快节奏（供 torrent_sync 每轮同步后询问）。

    动态判定，而非"开了刷流就一直快"：预算满且无可汰换时，发现新种也没
    机会下，高频刷索引纯属浪费站点请求——此时回 False，同步回到正常的
    冷热自适应/指数退避；余量恢复（汰换解锁、用户调大预算、任务转出）后
    自动回 True 重新钉住。
    """
    if not cred.boost_enabled:
        return False
    tasks = (
        (
            await session.execute(
                select(RatioBoostTask).where(
                    RatioBoostTask.site_id == cred.site_id,
                    RatioBoostTask.state == BoostTaskState.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    return (
        admission_headroom(list(tasks), cred.boost_budget_bytes, utcnow())
        >= _MIN_ADMISSION_HEADROOM
    )


async def release_site_tasks(session: AsyncSession, site_id: str) -> int:
    """站点配置被删除时，把该站在池的刷流任务全部转出管理，返回转出数。

    预算的主体（站点）已不存在，但任务与数据保留继续做种——删数据太激进
    （用户可能有意保种），转出后由用户在下载器里按 movieclaw-boost 分类
    自行处理。供 SiteConfigService.delete 调用，与凭据删除同一事务节奏。
    """
    tasks = (
        (
            await session.execute(
                select(RatioBoostTask).where(
                    RatioBoostTask.site_id == site_id,
                    RatioBoostTask.state == BoostTaskState.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for task in tasks:
        task.state = BoostTaskState.MISSING
        task.evicted_at = now
        task.evict_reason = "站点配置已删除，转出刷流管理（任务与数据保留做种）"
        task.updated_at = now
    if tasks:
        await session.commit()
        logger.info("站点 %s 已删除，%d 个刷流任务转出管理（保留做种）", site_id, len(tasks))
    return len(tasks)


async def collect_boost_stats(session: AsyncSession) -> dict:
    """按 site_id 聚合刷流统计，供 GET /sites/boost-stats 展示。

    覆盖两类站点的并集：台账里出现过的 ∪ 当前开着刷流的（后者可能还没有
    任何任务，也要让前端拿到预算数字画进度条）。

    近期窗口指标来自小时桶（ratio_boost_stat）：近 24 小时 / 近 7 天的
    上传量与**平均在池体积**（采样和/采样数），支撑"用 X GB 种子贡献了
    Y GB 上传"的过程展示。
    """
    from movieclaw_api.schemas.site import SiteBoostStatsView

    now = utcnow()

    async def window(since: datetime) -> dict[str, tuple[int, int]]:
        """窗口聚合：site_id → (上传量, 平均在池体积)。"""
        rows = (
            await session.execute(
                select(
                    RatioBoostStat.site_id,
                    func.sum(RatioBoostStat.uploaded_bytes),
                    func.sum(RatioBoostStat.used_bytes_sum),
                    func.sum(RatioBoostStat.tick_count),
                )
                .where(RatioBoostStat.bucket_start >= since)  # type: ignore[arg-type]
                .group_by(RatioBoostStat.site_id)
            )
        ).all()
        return {
            site_id: (int(up or 0), int((used_sum or 0) / ticks) if ticks else 0)
            for site_id, up, used_sum, ticks in rows
        }

    day_window = await window(now - timedelta(hours=24))
    week_window = await window(now - timedelta(days=7))

    rows = (
        await session.execute(
            select(
                RatioBoostTask.site_id,
                func.sum(
                    case((RatioBoostTask.state == BoostTaskState.ACTIVE, 1), else_=0)
                ).label("active_count"),
                func.sum(
                    case(
                        (RatioBoostTask.state == BoostTaskState.ACTIVE, RatioBoostTask.size_bytes),
                        else_=0,
                    )
                ).label("used_bytes"),
                func.sum(RatioBoostTask.uploaded_bytes).label("uploaded_total"),
                func.sum(
                    case((RatioBoostTask.state == BoostTaskState.EVICTED, 1), else_=0)
                ).label("evicted_count"),
            ).group_by(RatioBoostTask.site_id)
        )
    ).all()
    budgets = {
        c.site_id: c
        for c in (await session.execute(select(SiteCredential))).scalars().all()
    }
    ledger = {
        site_id: (
            int(active_count or 0),
            int(used_bytes or 0),
            int(uploaded_total or 0),
            int(evicted_count or 0),
        )
        for site_id, active_count, used_bytes, uploaded_total, evicted_count in rows
    }
    # 三源并集：台账里出现过的 ∪ 近期窗口有数据的 ∪ 当前开着刷流的——
    # 任一来源的站点都要出现在结果里（如任务全部转出但窗口内仍有上传贡献）
    site_ids = (
        set(ledger)
        | set(week_window)
        | {sid for sid, c in budgets.items() if c.boost_enabled}
    )
    stats: dict[str, SiteBoostStatsView] = {}
    for site_id in site_ids:
        cred = budgets.get(site_id)
        active_count, used_bytes, uploaded_total, evicted_count = ledger.get(
            site_id, (0, 0, 0, 0)
        )
        day = day_window.get(site_id, (0, 0))
        week = week_window.get(site_id, (0, 0))
        stats[site_id] = SiteBoostStatsView(
            active_count=active_count,
            used_bytes=used_bytes,
            budget_bytes=cred.boost_budget_bytes if cred else 0,
            uploaded_bytes_total=uploaded_total,
            evicted_count=evicted_count,
            uploaded_bytes_24h=day[0],
            avg_used_bytes_24h=day[1],
            uploaded_bytes_7d=week[0],
            avg_used_bytes_7d=week[1],
        )
    return stats
