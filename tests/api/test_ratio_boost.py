"""自动刷分享率引擎的决策逻辑与统计测试：候选评估、免费窗口、效率 EMA、
汰换、小时桶过程指标。

引擎的 IO 编排（下载器对账/提交）依赖真实下载器，属集成范畴；这里锁死的是
全部安全约束与决策规则——H&R 绝不碰、保留期绝不删、预算腾不出就放弃。
设计见 docs/design/site-protection-ratio-boost.md。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from movieclaw_api.services.ratio_boost import (
    _EMA_WINDOW_SECONDS,
    admission_headroom,
    apply_observation,
    assess_candidate,
    downloader_congested,
    evictable,
    free_window_sufficient,
    hand_over_if_claimed,
    pick_evictions,
    stop_loss_reason,
    turnover_seconds,
)
from movieclaw_db.models import BoostTaskState, RatioBoostTask, SiteTorrent, TorrentSource

_NOW = datetime(2026, 8, 17, 12, 0, 0)
_GIB = 1024**3
_BUDGET = 100 * _GIB


def _row(**kw) -> SiteTorrent:
    """一条默认「完全合格」的候选行；用例按需覆盖单个字段制造不合格。"""
    defaults = dict(
        site_id="demo",
        torrent_id="t1",
        title="Free.Torrent.2160p",
        size_bytes=10 * _GIB,
        publish_time=_NOW - timedelta(hours=1),
        seeders=3,
        leechers=30,
        download_volume_factor=0.0,
        is_free=True,
        free_deadline=None,
        hit_and_run=None,
        download_url="https://demo/download/1",
        source=TorrentSource.LIST,
        volatile_refreshed_at=_NOW - timedelta(minutes=5),
    )
    defaults.update(kw)
    return SiteTorrent(**defaults)


def _task(**kw) -> RatioBoostTask:
    defaults = dict(
        site_id="demo",
        torrent_id="t1",
        info_hash="a" * 40,
        downloader_id=1,
        size_bytes=10 * _GIB,
        state=BoostTaskState.ACTIVE,
        completed=True,
        upload_rate_ema=0.0,
        created_at=_NOW - timedelta(hours=100),  # 默认已过 72 小时保留期
        updated_at=_NOW,
    )
    defaults.update(kw)
    return RatioBoostTask(**defaults)


def _assess(row: SiteTorrent, *, tracked: set[str] | None = None):
    return assess_candidate(
        row, now=_NOW, budget_bytes=_BUDGET, tracked_torrent_ids=tracked or set()
    )


# ---------------------------------------------------------------------------
# 候选评估
# ---------------------------------------------------------------------------


class TestAssessCandidate:
    def test_fully_qualified(self) -> None:
        ok, score = _assess(_row())
        assert ok
        assert score > 0

    def test_rejects_tracked(self) -> None:
        """抢过的（含已汰换的）不再抢，避免反复拉扯。"""
        ok, _ = _assess(_row(), tracked={"t1"})
        assert not ok

    def test_rejects_explicit_hit_and_run(self) -> None:
        """明确标注 H&R 考核的种子绝不碰；hit_and_run=None（站点不提供）允许。"""
        assert not _assess(_row(hit_and_run=True))[0]
        assert _assess(_row(hit_and_run=False))[0]
        assert _assess(_row(hit_and_run=None))[0]

    def test_rejects_non_free(self) -> None:
        assert not _assess(_row(is_free=False))[0]
        assert not _assess(_row(is_free=None))[0]

    def test_rejects_no_leechers(self) -> None:
        """没有下载者就没有上传对象；leechers=NULL（未观测）同样不抢。"""
        assert not _assess(_row(leechers=0))[0]
        assert not _assess(_row(leechers=None))[0]

    def test_rejects_stale_publish(self) -> None:
        assert not _assess(_row(publish_time=_NOW - timedelta(hours=25)))[0]
        assert not _assess(_row(publish_time=None))[0]

    def test_rejects_stale_promo_observation(self) -> None:
        """促销观测超过 2 小时（或从未观测）不可信——促销可能已结束，
        抢一个"其实已不免费"的种子会产生真实下载量。"""
        assert not _assess(_row(volatile_refreshed_at=_NOW - timedelta(hours=3)))[0]
        assert not _assess(_row(volatile_refreshed_at=None))[0]
        assert _assess(_row(volatile_refreshed_at=_NOW - timedelta(hours=1)))[0]

    def test_rejects_oversized(self) -> None:
        """单种 > 预算 1/4 会让汰换失去弹性。"""
        assert not _assess(_row(size_bytes=_BUDGET // 4 + 1))[0]
        assert _assess(_row(size_bytes=_BUDGET // 4))[0]

    def test_rejects_missing_essentials(self) -> None:
        assert not _assess(_row(download_url=None))[0]
        assert not _assess(_row(size_bytes=None))[0]

    def test_score_prefers_demand_over_supply(self) -> None:
        """leechers/(seeders+1)：供不应求的评分更高。"""
        _, hot = _assess(_row(seeders=1, leechers=50))
        _, cold = _assess(_row(seeders=50, leechers=5))
        assert hot > cold

    def test_score_doubles_on_2x_upload(self) -> None:
        _, base = _assess(_row(upload_volume_factor=1.0))
        _, doubled = _assess(_row(upload_volume_factor=2.0))
        assert doubled == base * 2


# ---------------------------------------------------------------------------
# 免费窗口
# ---------------------------------------------------------------------------


class TestFreeWindow:
    def test_null_deadline_is_sufficient(self) -> None:
        """NULL = 无促销截止/长期免费（索引层已归一 M-Team 哨兵）。"""
        assert free_window_sufficient(10 * _GIB, None, _NOW)

    def test_small_torrent_needs_min_margin(self) -> None:
        """小种子也要 2 小时安全垫。"""
        assert free_window_sufficient(1 * _GIB, _NOW + timedelta(hours=3), _NOW)
        assert not free_window_sufficient(1 * _GIB, _NOW + timedelta(hours=1), _NOW)

    def test_big_torrent_needs_download_time(self) -> None:
        """大种子按 5 MiB/s 保守估算：200 GiB 需约 11.4 小时，3 小时窗口不够。"""
        size = 200 * _GIB
        assert not free_window_sufficient(size, _NOW + timedelta(hours=3), _NOW)
        assert free_window_sufficient(size, _NOW + timedelta(hours=12), _NOW)


# ---------------------------------------------------------------------------
# 效率追踪（EMA）
# ---------------------------------------------------------------------------


class TestApplyObservation:
    def test_first_observation_establishes_rate(self) -> None:
        """首次观测以 created_at 为基线：上传量从 0 起算，能得出真实速率。

        EMA 是时距感知的（等效 24h 窗口）：α = 1 - e^(-dt/W)。
        """
        task = _task(created_at=_NOW - timedelta(seconds=1000), uploaded_bytes=0)
        task.last_checked_at = None
        apply_observation(task, uploaded_bytes=1_000_000, completed=True, now=_NOW)
        alpha = 1 - math.exp(-1000 / _EMA_WINDOW_SECONDS)
        assert task.uploaded_bytes == 1_000_000
        assert math.isclose(task.upload_rate_ema, alpha * 1000.0)
        assert task.last_checked_at == _NOW

    def test_daily_burst_survives_quiet_hours(self) -> None:
        """24h 窗口的意义：昨晚狂传、白天安静的种子不能几小时就被判死。

        旧的 0.3/5min EMA 一小时安静就衰减到 1.4%；24h 窗口下安静 6 小时
        仍保留约 78% 的效率读数，汰换判断以「天」为尺度。
        """
        task = _task(uploaded_bytes=1000, upload_rate_ema=100_000.0)  # 高效历史
        task.last_checked_at = _NOW - timedelta(hours=6)
        apply_observation(task, uploaded_bytes=1000, completed=True, now=_NOW)  # 6h 零上传
        assert task.upload_rate_ema > 100_000.0 * 0.75

    def test_none_uploaded_keeps_ema(self) -> None:
        """旧适配器不提供上传量时绝不能当 0 差分（会误汰换）。"""
        task = _task(uploaded_bytes=500, upload_rate_ema=123.0)
        task.last_checked_at = _NOW - timedelta(seconds=300)
        apply_observation(task, uploaded_bytes=None, completed=True, now=_NOW)
        assert task.uploaded_bytes == 500
        assert task.upload_rate_ema == 123.0
        assert task.last_checked_at == _NOW  # 时钟仍推进

    def test_negative_delta_resets_baseline(self) -> None:
        """下载器重建任务（累计上传回退）时重置基线、不更新 EMA。"""
        task = _task(uploaded_bytes=1_000_000, upload_rate_ema=50.0)
        task.last_checked_at = _NOW - timedelta(seconds=300)
        apply_observation(task, uploaded_bytes=100, completed=True, now=_NOW)
        assert task.uploaded_bytes == 100
        assert task.upload_rate_ema == 50.0

    def test_completed_is_sticky(self) -> None:
        """完成位只置不清：下载器重新校验期间闪烁不应把任务打回未完成。"""
        task = _task(completed=True)
        task.last_checked_at = _NOW - timedelta(seconds=300)
        apply_observation(task, uploaded_bytes=None, completed=False, now=_NOW)
        assert task.completed is True


# ---------------------------------------------------------------------------
# 汰换
# ---------------------------------------------------------------------------


class TestEviction:
    def test_turnover_is_the_unit(self) -> None:
        """周转 = 传一遍自己的体积要多久；没人要（EMA=0）= 无穷大。"""
        task = _task(size_bytes=10 * _GIB, upload_rate_ema=(10 * _GIB) / 86400)
        assert math.isclose(turnover_seconds(task), 86400)
        assert math.isinf(turnover_seconds(_task(upload_rate_ema=0.0)))

    def test_hold_period_is_inviolable(self) -> None:
        """入池不满保留期（默认 3 天）的任务在任何条件下不可汰换（H&R 安全垫）。"""
        young = _task(created_at=_NOW - timedelta(hours=71), upload_rate_ema=0.0)
        assert not evictable(young, _NOW)
        old = _task(created_at=_NOW - timedelta(hours=73), upload_rate_ema=0.0)
        assert evictable(old, _NOW)

    def test_hold_is_per_site_configurable(self) -> None:
        """保留期每站可配：7 天的站 5 天龄任务仍受保护；0 天 = 不设 H&R 保护。"""
        aged_5d = _task(created_at=_NOW - timedelta(days=5), upload_rate_ema=0.0)
        assert not evictable(aged_5d, _NOW, hold=timedelta(days=7))
        assert evictable(aged_5d, _NOW, hold=timedelta(days=0))

    def test_hold_zero_still_needs_mature_measurement(self) -> None:
        """hold=0 关闭的是 H&R 保护，不是判定质量：刚下完 EMA 还没暖起来的
        种子（蜂群还有下载者）不可因"看起来慢"被误杀；测量满 24h 后慢就是真慢。"""
        fresh = _task(
            created_at=_NOW - timedelta(hours=2),
            upload_rate_ema=0.0,
            swarm_leechers=5,
        )
        assert not evictable(fresh, _NOW, hold=timedelta(0))
        measured = _task(
            created_at=_NOW - timedelta(hours=25),
            upload_rate_ema=0.0,
            swarm_leechers=5,
        )
        assert evictable(measured, _NOW, hold=timedelta(0))

    def test_dead_swarm_skips_maturity_wait(self) -> None:
        """蜂群已死（tracker 汇报 0 下载者）= 未来注定零产出，不必等测量成熟
        即可汰换（仍须过保留期）；蜂群未知（None）不算死，宁可多留。"""
        dead_fresh = _task(
            created_at=_NOW - timedelta(hours=2),
            upload_rate_ema=0.0,
            swarm_leechers=0,
        )
        assert evictable(dead_fresh, _NOW, hold=timedelta(0))
        unknown_fresh = _task(
            created_at=_NOW - timedelta(hours=2),
            upload_rate_ema=0.0,
            swarm_leechers=None,
        )
        assert not evictable(unknown_fresh, _NOW, hold=timedelta(0))

    def test_uncompleted_and_fast_turnover_are_kept(self) -> None:
        assert not evictable(_task(completed=False), _NOW)
        # 周转 5 天（10 天地板以内）= 单位存储仍在产出，留下
        fast = _task(upload_rate_ema=(10 * _GIB) / (5 * 86400))
        assert not evictable(fast, _NOW)

    def test_turnover_floor_is_size_relative(self) -> None:
        """同样的绝对速度，判决取决于体积：效率的单位是 rate/size，不是 rate。

        15 KiB/s 对 5 GiB 是约 4 天周转的好资产；对 100 GiB 是 81 天周转的
        坏资产——绝对速度地板会把这两个判反。
        """
        rate = 15 * 1024.0
        small = _task(size_bytes=5 * _GIB, upload_rate_ema=rate)
        big = _task(torrent_id="big", size_bytes=100 * _GIB, upload_rate_ema=rate)
        assert not evictable(small, _NOW)
        assert evictable(big, _NOW)

    def test_pick_slowest_turnover_first(self) -> None:
        """汰换顺序按单位存储产出：大而慢的先走，哪怕它的绝对速度更高。"""
        # 绝对速度 8 KiB/s 但只有 1 GiB：周转 1.5 天…… 需为可汰换设成慢的
        dense = _task(torrent_id="dense", size_bytes=10 * _GIB, upload_rate_ema=800.0)
        sparse = _task(torrent_id="sparse", size_bytes=40 * _GIB, upload_rate_ema=1600.0)
        # dense 周转 ≈ 155 天，sparse 周转 ≈ 311 天（绝对速度反而更高）
        picked = pick_evictions([dense, sparse], need_bytes=30 * _GIB, now=_NOW)
        assert picked is not None
        assert [t.torrent_id for t in picked] == ["sparse"]

    def test_dead_swarm_evicted_before_slower_alive(self) -> None:
        """死种最先走：蜂群 0 下载者的种子未来注定零产出，优先于周转更慢
        但蜂群里还有人的种子——后者可能只是暂时安静。"""
        alive_slower = _task(
            torrent_id="alive", size_bytes=10 * _GIB, upload_rate_ema=100.0, swarm_leechers=3
        )
        dead_faster = _task(
            torrent_id="dead", size_bytes=10 * _GIB, upload_rate_ema=500.0, swarm_leechers=0
        )
        picked = pick_evictions([alive_slower, dead_faster], need_bytes=10 * _GIB, now=_NOW)
        assert picked is not None
        assert [t.torrent_id for t in picked] == ["dead"]

    def test_insufficient_space_returns_none(self) -> None:
        """可汰换的加起来腾不出所需空间 → 返回 None，放弃准入而非删更多。"""
        protected_by_hold = _task(created_at=_NOW - timedelta(hours=1), size_bytes=50 * _GIB)
        small = _task(torrent_id="s", upload_rate_ema=0.0, size_bytes=5 * _GIB)
        assert pick_evictions([protected_by_hold, small], need_bytes=20 * _GIB, now=_NOW) is None


class TestAdmissionHeadroom:
    """准入余量 = 剩余预算 + 可汰换容量——准入扫描与同步快节奏的统一开关。"""

    def test_empty_pool_full_headroom(self) -> None:
        assert admission_headroom([], _BUDGET, _NOW) == _BUDGET

    def test_evictable_capacity_counts_as_headroom(self) -> None:
        """预算被占满但有低效老种可换：发现新种仍有意义（会触发汰换腾位）。"""
        hot = _task(size_bytes=60 * _GIB, upload_rate_ema=100 * 1024)  # 高效，不可换
        idle = _task(torrent_id="i", size_bytes=40 * _GIB, upload_rate_ema=0.0)  # 可换
        assert admission_headroom([hot, idle], _BUDGET, _NOW) == 40 * _GIB

    def test_full_pool_within_hold_has_no_headroom(self) -> None:
        """池子满且全在 72 小时保留期内：换不动，余量为 0——此时准入跳过、
        索引同步回落到正常自适应（不再为发现新种白打站点）。"""
        young = _task(size_bytes=100 * _GIB, created_at=_NOW - timedelta(hours=10))
        assert admission_headroom([young], _BUDGET, _NOW) == 0

    def test_terminal_states_do_not_occupy(self) -> None:
        gone = _task(state=BoostTaskState.MISSING, size_bytes=30 * _GIB)
        assert admission_headroom([gone], _BUDGET, _NOW) == _BUDGET


# ---------------------------------------------------------------------------
# 未完成任务的止损（不受 72 小时保留期约束——保留期保护的是已完成的做种）
# ---------------------------------------------------------------------------


class TestStopLoss:
    def test_free_expired_with_much_remaining_abandons(self) -> None:
        """免费窗口已过、还剩 4 成没下：每多下一字节都是付费流量，止损。"""
        task = _task(
            completed=False,
            created_at=_NOW - timedelta(hours=5),
            free_deadline=_NOW - timedelta(hours=1),
        )
        reason = stop_loss_reason(task, progress=0.6, now=_NOW)
        assert reason is not None and "免费窗口" in reason

    def test_free_expired_but_nearly_done_finishes(self) -> None:
        """已下到 95%：删了全白费，剩余付费量很小，放行下完。"""
        task = _task(
            completed=False,
            created_at=_NOW - timedelta(hours=5),
            free_deadline=_NOW - timedelta(hours=1),
        )
        assert stop_loss_reason(task, progress=0.95, now=_NOW) is None

    def test_stuck_download_abandons(self) -> None:
        task = _task(completed=False, created_at=_NOW - timedelta(hours=49))
        reason = stop_loss_reason(task, progress=0.3, now=_NOW)
        assert reason is not None and "48 小时" in reason

    def test_healthy_incomplete_and_completed_are_kept(self) -> None:
        healthy = _task(completed=False, created_at=_NOW - timedelta(hours=2))
        assert stop_loss_reason(healthy, progress=0.3, now=_NOW) is None
        # 已完成的任务永远轮不到止损（归汰换与保留期管）
        done = _task(completed=True, free_deadline=_NOW - timedelta(hours=1))
        assert stop_loss_reason(done, progress=1.0, now=_NOW) is None

    def test_queued_48h_gets_honest_reason(self) -> None:
        """排队 48 小时同样让出预算，但原因如实说是活动位满，不误报死种。"""
        task = _task(completed=False, created_at=_NOW - timedelta(hours=49))
        reason = stop_loss_reason(task, progress=0.0, now=_NOW, queued=True)
        assert reason is not None and "排队" in reason and "死种" not in reason


class TestDownloaderCongested:
    def test_any_queued_download_means_congested(self) -> None:
        """存在排队中的下载任务 = 活动位满 = 暂停准入；否则放行。

        预算越大越需要这个闸：没有它，引擎会按自己的节奏把下载器队列塞爆，
        排队任务吃掉免费窗口、48 小时后被止损删掉，全程空转。
        """
        assert downloader_congested(["downloading", "queued", "completed"])
        assert not downloader_congested(["downloading", "completed", "stalled"])
        assert not downloader_congested([])


# ---------------------------------------------------------------------------
# 与订阅/手动下载的碰撞：认领转出
# ---------------------------------------------------------------------------


class TestHandOverIfClaimed:
    def test_claimed_task_leaves_pool_without_deletion(self) -> None:
        """被订阅认领的任务转出管理：让出预算、绝不删数据，之后归订阅状态机管。"""
        task = _task()
        assert hand_over_if_claimed(task, {task.info_hash}, _NOW)
        assert task.state == BoostTaskState.MISSING
        assert task.evicted_at == _NOW
        assert "认领" in (task.evict_reason or "")
        # 转出的任务不再是汰换候选——数据安全的关键断言
        assert not evictable(task, _NOW)

    def test_unclaimed_task_stays(self) -> None:
        task = _task()
        assert not hand_over_if_claimed(task, {"f" * 40}, _NOW)
        assert task.state == BoostTaskState.ACTIVE

    def test_terminal_states_untouched(self) -> None:
        """已终态（evicted/missing）的任务不重复转出，保留原始结论。"""
        task = _task(state=BoostTaskState.EVICTED, evict_reason="原始原因")
        assert not hand_over_if_claimed(task, {task.info_hash}, _NOW)
        assert task.evict_reason == "原始原因"


# ---------------------------------------------------------------------------
# 小时桶过程指标：近 24h/7d 用 X GB 种子贡献 Y GB 上传
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    from movieclaw_api.core.config import get_settings
    from movieclaw_db.engine import dispose_db, get_database, init_db
    from movieclaw_db.migrations import run_migrations

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'boost.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


class _FakeDownloader:
    """set_location 的记录桩；raises 非空时抛错模拟迁移失败。"""

    def __init__(self, raises: Exception | None = None):
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    async def set_location(self, info_hash: str, save_path: str) -> None:
        if self.raises is not None:
            raise self.raises
        self.calls.append((info_hash, save_path))


@pytest.mark.asyncio
async def test_reclaim_moves_data_and_hands_over(db) -> None:
    """「刷流先抢、订阅后到」的接管：迁目录 + 台账转出 + 标记 reclaimed。"""
    from movieclaw_api.services.torrent_submit import _reclaim_boost_task
    from movieclaw_downloader.models import SubmitResult

    # downloader_id 置空：测试库没有下载器配置行，接管逻辑也不依赖它
    task = _task(downloader_id=None)
    async with db.session() as session:
        session.add(task)
        await session.commit()

        fake = _FakeDownloader()
        result = await _reclaim_boost_task(
            session,
            fake,
            SubmitResult(info_hash=task.info_hash, already_exists=True),
            save_path="/downloads/movies/Show",
        )
        await session.refresh(task)

    assert result.reclaimed_from_boost
    assert fake.calls == [(task.info_hash, "/downloads/movies/Show")]
    assert task.state == BoostTaskState.MISSING
    assert "接管" in (task.evict_reason or "")


@pytest.mark.asyncio
async def test_reclaim_leaves_user_torrents_alone(db) -> None:
    """已存在的任务不是刷流的（用户自己加的）：不迁目录、不标 reclaimed。"""
    from movieclaw_api.services.torrent_submit import _reclaim_boost_task
    from movieclaw_downloader.models import SubmitResult

    async with db.session() as session:
        fake = _FakeDownloader()
        result = await _reclaim_boost_task(
            session,
            fake,
            SubmitResult(info_hash="f" * 40, already_exists=True),
            save_path="/downloads/movies",
        )

    assert not result.reclaimed_from_boost
    assert fake.calls == []


@pytest.mark.asyncio
async def test_reclaim_move_failure_keeps_boost_ownership(db) -> None:
    """迁移失败不接管：台账保持 active（由认领转出兜底），提交结果原样返回。"""
    from movieclaw_api.services.torrent_submit import _reclaim_boost_task
    from movieclaw_downloader.models import SubmitResult

    task = _task(downloader_id=None)
    async with db.session() as session:
        session.add(task)
        await session.commit()

        fake = _FakeDownloader(raises=RuntimeError("目标目录不可写"))
        result = await _reclaim_boost_task(
            session,
            fake,
            SubmitResult(info_hash=task.info_hash, already_exists=True),
            save_path="/nope",
        )
        await session.refresh(task)

    assert not result.reclaimed_from_boost
    assert task.state == BoostTaskState.ACTIVE


@pytest.mark.asyncio
async def test_hourly_buckets_accumulate_and_windows_aggregate(db) -> None:
    """同一小时内多个 tick 累进同一桶；窗口聚合给出上传量与平均在池体积。"""
    from movieclaw_api.services.ratio_boost import _record_stats, collect_boost_stats
    from movieclaw_db.models import AuthType, SiteCredential, utcnow

    now = utcnow()
    async with db.session() as session:
        session.add(
            SiteCredential(site_id="demo", auth_type=AuthType.COOKIE, boost_enabled=True)
        )
        await session.commit()

        # 两个 tick：上传 3 GiB + 1 GiB，在池占用恒为 40 GiB
        for gained in (3 * _GIB, 1 * _GIB):
            await _record_stats(
                session, deltas={"demo": gained}, used_by_site={"demo": 40 * _GIB}, now=now
            )
        stats = await collect_boost_stats(session)

    view = stats["demo"]
    assert view.uploaded_bytes_24h == 4 * _GIB
    assert view.avg_used_bytes_24h == 40 * _GIB  # 平均在池 = (40+40)/2
    # 7 天窗口包含 24 小时窗口的数据
    assert view.uploaded_bytes_7d == 4 * _GIB
    assert view.avg_used_bytes_7d == 40 * _GIB


@pytest.mark.asyncio
async def test_stat_windows_exclude_old_buckets(db) -> None:
    """8 天前的桶不进 7 天窗口；昨天的桶进 7 天窗口但不进 24 小时窗口。"""
    from movieclaw_api.services.ratio_boost import collect_boost_stats
    from movieclaw_db.models import AuthType, RatioBoostStat, SiteCredential, utcnow

    now = utcnow()
    async with db.session() as session:
        session.add(
            SiteCredential(site_id="demo", auth_type=AuthType.COOKIE, boost_enabled=True)
        )
        for age, uploaded in ((timedelta(days=8), 7 * _GIB), (timedelta(days=2), 2 * _GIB)):
            session.add(
                RatioBoostStat(
                    site_id="demo",
                    bucket_start=(now - age).replace(minute=0, second=0, microsecond=0),
                    uploaded_bytes=uploaded,
                    used_bytes_sum=10 * _GIB,
                    tick_count=1,
                )
            )
        await session.commit()
        stats = await collect_boost_stats(session)

    view = stats["demo"]
    assert view.uploaded_bytes_24h == 0
    assert view.uploaded_bytes_7d == 2 * _GIB
