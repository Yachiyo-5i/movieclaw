"""自动刷分享率引擎的纯决策逻辑测试：候选评估、免费窗口、效率 EMA、汰换。

引擎的 IO 编排（下载器对账/提交）依赖真实下载器，属集成范畴；这里锁死的是
全部安全约束与决策规则——H&R 绝不碰、保留期绝不删、预算腾不出就放弃。
设计见 docs/design/site-protection-ratio-boost.md。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from movieclaw_api.services.ratio_boost import (
    apply_observation,
    assess_candidate,
    evictable,
    free_window_sufficient,
    hand_over_if_claimed,
    pick_evictions,
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
        """首次观测以 created_at 为基线：上传量从 0 起算，能得出真实速率。"""
        task = _task(created_at=_NOW - timedelta(seconds=1000), uploaded_bytes=0)
        task.last_checked_at = None
        apply_observation(task, uploaded_bytes=1_000_000, completed=True, now=_NOW)
        # rate = 1MB/1000s = 1000 B/s，EMA = 0.3 * 1000 + 0.7 * 0 = 300
        assert task.uploaded_bytes == 1_000_000
        assert task.upload_rate_ema == 300.0
        assert task.last_checked_at == _NOW

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
    def test_hold_period_is_inviolable(self) -> None:
        """入池不满 72 小时的任务在任何条件下不可汰换（H&R 安全垫）。"""
        young = _task(created_at=_NOW - timedelta(hours=71), upload_rate_ema=0.0)
        assert not evictable(young, _NOW)
        old = _task(created_at=_NOW - timedelta(hours=73), upload_rate_ema=0.0)
        assert evictable(old, _NOW)

    def test_uncompleted_and_hot_are_kept(self) -> None:
        assert not evictable(_task(completed=False), _NOW)
        # 上传 EMA 高于地板（10 KiB/s）= 还在产出，留下
        assert not evictable(_task(upload_rate_ema=11 * 1024), _NOW)

    def test_pick_lowest_efficiency_first(self) -> None:
        slow = _task(torrent_id="slow", upload_rate_ema=10.0, size_bytes=10 * _GIB)
        slower = _task(torrent_id="slower", upload_rate_ema=1.0, size_bytes=10 * _GIB)
        picked = pick_evictions([slow, slower], need_bytes=10 * _GIB, now=_NOW)
        assert picked is not None
        assert [t.torrent_id for t in picked] == ["slower"]

    def test_insufficient_space_returns_none(self) -> None:
        """可汰换的加起来腾不出所需空间 → 返回 None，放弃准入而非删更多。"""
        protected_by_hold = _task(created_at=_NOW - timedelta(hours=1), size_bytes=50 * _GIB)
        small = _task(torrent_id="s", upload_rate_ema=0.0, size_bytes=5 * _GIB)
        assert pick_evictions([protected_by_hold, small], need_bytes=20 * _GIB, now=_NOW) is None


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
