"""洗版 attempt 状态机测试：巡检不误杀、换源走洗版语义、并发投递防线。

背景（真实教训）：attempt→工单的关联在缺口语义下是 ``info_hash 相等 ∧
status 在途``，对洗版 attempt 恒为空（工单不重开、info_hash 指向旧版本）——
不分流的话，洗版 attempt 会在首个巡检 tick 被当成"工单已闭合"错误完结，
死种也永远换不了源。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.download_progress as progress_mod
from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    DownloadAttemptStatus,
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
    utcnow,
)

_WEBDL = {"resolution": "1080p", "media_source": "WEB-DL"}
_REMUX = {"resolution": "1080p", "media_source": "Blu-ray", "remux": True}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(db, *, in_scope=True):
    """imported 工单（info_hash 指向旧版本）+ 在途洗版 attempt（新 hash）。"""
    async with db.session() as session:
        item = MediaItem(kind="tv", tmdb_id=200, title="T", original_title="T", year=2024)
        rule_set = RuleSet(name="默认", spec={"upgrade_source": "remux"})
        session.add_all([item, rule_set])
        await session.commit()
        await session.refresh(item)
        await session.refresh(rule_set)
        sub = Subscription(media_item_id=item.id, kind="tv", rule_set_id=rule_set.id)
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        wanted = WantedItem(
            subscription_id=sub.id,
            media_item_id=item.id,
            season_number=1,
            episode_number=1,
            status=WantedStatus.IMPORTED,
            in_scope=in_scope,
            quality=_WEBDL,
            info_hash="oldhash",
            imported_at=utcnow(),
        )
        attempt = SubscriptionDownloadAttempt(
            subscription_id=sub.id,
            info_hash="newhash",
            site_id="site-a",
            torrent_id="up1",
            units=[[1, 1]],
            quality=_REMUX,
            purpose="upgrade",
            status=DownloadAttemptStatus.ACTIVE,
            last_progress_at=utcnow(),
        )
        session.add_all([wanted, attempt])
        await session.commit()
        await session.refresh(attempt)
        return sub.id, attempt.id, wanted.id


@pytest.mark.asyncio
async def test_observe_does_not_close_in_flight_upgrade_attempt(db, monkeypatch):
    """巡检看到洗版 attempt：不得因"缺口工单为空"打成 IMPORTED，
    继续正常观察心跳（下载器不可达时状态原样保留）。"""
    _sub, attempt_id, _w = await _seed(db)

    async def lookup_unknown(*args, **kwargs):
        return progress_mod._TorrentLookup(match=None, reachable_count=0)

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup_unknown)
    await progress_mod._observe_attempt(attempt_id, downloaders=[])
    async with db.session() as session:
        attempt = await session.get(SubscriptionDownloadAttempt, attempt_id)
        assert attempt.status == DownloadAttemptStatus.ACTIVE  # 不是 IMPORTED


@pytest.mark.asyncio
async def test_observe_cancels_upgrade_attempt_when_unit_descoped(db, monkeypatch):
    """洗版单元退出订阅范围：巡检止损取消 attempt（不碰下载器任务）。"""
    _sub, attempt_id, _w = await _seed(db, in_scope=False)

    async def lookup_unknown(*args, **kwargs):
        return progress_mod._TorrentLookup(match=None, reachable_count=0)

    monkeypatch.setattr(progress_mod, "_lookup_torrent", lookup_unknown)
    await progress_mod._observe_attempt(attempt_id, downloaders=[])
    async with db.session() as session:
        attempt = await session.get(SubscriptionDownloadAttempt, attempt_id)
        assert attempt.status == DownloadAttemptStatus.CANCELLED
        assert "退出当前订阅范围" in (attempt.cleanup_note or "")


@pytest.mark.asyncio
async def test_trial_open_target_uses_upgrade_semantics(db):
    """洗版源的换源试用：目标判定走洗版语义（imported 且在范围内=开放），
    不再被缺口语义误判为"工单已满足"而立即 fail_trial。"""
    sub_id, attempt_id, _w = await _seed(db)
    async with db.session() as session:
        old = await session.get(SubscriptionDownloadAttempt, attempt_id)
        old.status = DownloadAttemptStatus.REPLACEMENT_PENDING
        trial = SubscriptionDownloadAttempt(
            subscription_id=sub_id,
            info_hash="trialhash",
            replaces_attempt_id=attempt_id,
            units=[[1, 1]],
            quality=_REMUX,
            purpose="upgrade",
            status=DownloadAttemptStatus.TRIAL,
            last_progress_at=utcnow(),
        )
        session.add(trial)
        await session.commit()
        await session.refresh(trial)
        assert await progress_mod._trial_has_open_target(session, trial) is True


@pytest.mark.asyncio
async def test_replacement_targets_resolve_for_upgrade_attempt(db):
    """换源候选评估的目标工单：洗版 attempt 解析到 imported 行（否则死掉的
    洗版源永远投不出替代源）。"""
    from movieclaw_api.services.subscription.replacement import _current_attempt_wanted

    _sub, attempt_id, wanted_id = await _seed(db)
    async with db.session() as session:
        attempt = await session.get(SubscriptionDownloadAttempt, attempt_id)
        rows = await _current_attempt_wanted(session, attempt)
        assert [r.id for r in rows] == [wanted_id]


@pytest.mark.asyncio
async def test_dispatch_filters_units_with_in_flight_upgrade(db):
    """投递前复查：已有在途洗版 attempt 的单元被剔除（并发防线）。"""
    from movieclaw_api.services.subscription.dispatch import _filter_upgrade_in_flight

    sub_id, _attempt_id, wanted_id = await _seed(db)
    async with db.session() as session:
        sub = await session.get(Subscription, sub_id)
        wanted = await session.get(WantedItem, wanted_id)
        remaining = await _filter_upgrade_in_flight(session, sub, [wanted])
        assert remaining == []
        # 无在途 attempt 的单元不受影响
        other = WantedItem(
            subscription_id=sub_id,
            media_item_id=wanted.media_item_id,
            season_number=1,
            episode_number=2,
            status=WantedStatus.IMPORTED,
            quality=_WEBDL,
            imported_at=utcnow(),
        )
        session.add(other)
        await session.commit()
        await session.refresh(other)
        remaining = await _filter_upgrade_in_flight(session, sub, [other])
        assert [r.id for r in remaining] == [other.id]
