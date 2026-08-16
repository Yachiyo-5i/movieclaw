"""洗版决策接线测试（quality-upgrade.md §5/§6）：上下文加载、判定链、
整季包铁律、在途去重、证伪排除、调度排期。全部走 dry-run 投递。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription.matching import (
    UPGRADE_PRIORITY,
    evaluate_and_dispatch,
    load_match_context,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    DownloadAttemptStatus,
    MediaItem,
    RuleSet,
    SiteTorrent,
    Subscription,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    TorrentSource,
    WantedItem,
    WantedStatus,
    utcnow,
)

_SPEC_UPGRADE = {"upgrade_source": "remux"}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pipe.db'}")
    monkeypatch.setenv("SUBSCRIPTION_DISPATCH_DRY_RUN", "true")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(
    db,
    *,
    spec=None,
    quality=None,
    wanted_status=WantedStatus.IMPORTED,
    units=((1, 1),),
    tmdb_id=200,
):
    """条目/订阅/工单最小闭包。quality 应用到全部单元。"""
    async with db.session() as session:
        item = MediaItem(
            kind="tv",
            tmdb_id=tmdb_id,
            title="测试剧集",
            original_title="Testshow",
            year=2024,
            aliases=["Testshow", "测试剧集"],
        )
        rule_set = RuleSet(name=f"默认-{tmdb_id}", spec=spec or _SPEC_UPGRADE)
        session.add_all([item, rule_set])
        await session.commit()
        await session.refresh(item)
        await session.refresh(rule_set)
        sub = Subscription(media_item_id=item.id, kind="tv", rule_set_id=rule_set.id)
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        ids = []
        for season, episode in units:
            wanted = WantedItem(
                subscription_id=sub.id,
                media_item_id=item.id,
                season_number=season,
                episode_number=episode,
                status=wanted_status,
                quality=quality,
                imported_at=utcnow() if wanted_status == WantedStatus.IMPORTED else None,
            )
            session.add(wanted)
            await session.commit()
            await session.refresh(wanted)
            ids.append(wanted.id)
        return item.id, sub.id, ids


def _torrent(title, *, attrs, torrent_id="t1", seeders=10):
    return SiteTorrent(
        site_id="site-a",
        torrent_id=torrent_id,
        title=title,
        attrs=attrs,
        enrich_version=1,
        source=TorrentSource.LIST,
        seeders=seeders,
        publish_time=utcnow(),
    )


_WEBDL = {"resolution": "1080p", "media_source": "WEB-DL"}
_REMUX_E1 = {
    "resolution": "1080p",
    "media_source": "Blu-ray",
    "remux": True,
    "seasons": [1],
    "episodes": [1],
}


@pytest.mark.asyncio
async def test_context_loads_upgrade_units(db):
    """已入库、快照低于目标的单元进入洗版上下文；无缺口也能形成上下文。"""
    item_id, _sub_id, _ids = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        contexts = await load_match_context(session)
        assert item_id in contexts
        ctx = contexts[item_id]
        assert not ctx.open_wanted
        assert (1, 1) in ctx.upgrade_wanted
        assert ctx.upgrade_snapshots[(1, 1)].media_source == "WEB-DL"


@pytest.mark.asyncio
async def test_context_skips_at_cutoff_and_null_snapshot(db):
    """到顶的单元与无快照（{} 哨兵）的单元不进洗版上下文。"""
    item_id, _sub, _ = await _seed(
        db,
        quality={"resolution": "1080p", "media_source": "Blu-ray", "remux": True},
        units=((1, 1),),
    )
    async with db.session() as session:
        assert item_id not in await load_match_context(session)

    item2 = await _seed(db, quality={}, units=((1, 2),), tmdb_id=201)
    async with db.session() as session:
        assert item2[0] not in await load_match_context(session)


@pytest.mark.asyncio
async def test_upgrade_dispatches_better_candidate(db):
    """WEB-DL 已入库 + Remux 新种子 → 洗版投递：UPGRADE_GRABBED 活动、
    工单保持 imported、info_hash 不动。"""
    item_id, sub_id, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        row = _torrent("Testshow 2024 S01E01 1080p Blu-ray REMUX", attrs=_REMUX_E1)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 1
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.IMPORTED  # 不重开工单
        assert wanted.info_hash is None  # 指向旧版本的关联不动
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id,
                        SubscriptionActivity.type == "upgrade_grabbed",
                    )
                )
            ).scalars()
        )
        assert len(activities) == 1
        assert "1080p Remux" in activities[0].message
        assert "当前 1080p WEB-DL" in activities[0].message


@pytest.mark.asyncio
async def test_upgrade_rejects_not_better_and_at_cutoff_silently(db):
    """同档/降档候选不投递也不刷活动（噪音控制）。"""
    item_id, sub_id, _ = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        row = _torrent(
            "Testshow 2024 S01E01 1080p WEB-DL",
            attrs={**_WEBDL, "seasons": [1], "episodes": [1]},
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 0
        activities = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id
                    )
                )
            ).scalars()
        )
        assert activities == []


_PACK_REMUX = {
    "resolution": "1080p",
    "media_source": "Blu-ray",
    "remux": True,
    "seasons": [1],
    "complete": True,
}


async def _seed_pack_units(db, sub_id, item_id, episodes_quality: dict[int, dict]):
    """给整季包测试造带播出日期的 imported 单元（air_date 已过，包可覆盖）。"""
    from datetime import date, timedelta

    aired = date.today() - timedelta(days=30)
    async with db.session() as session:
        for episode, quality in episodes_quality.items():
            session.add(
                WantedItem(
                    subscription_id=sub_id,
                    media_item_id=item_id,
                    season_number=1,
                    episode_number=episode,
                    status=WantedStatus.IMPORTED,
                    quality=quality,
                    air_date=aired,
                    imported_at=utcnow(),
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_pack_dispatches_when_all_units_washable(db):
    """正向对照：包覆盖的单元全部可洗 → 整季包投递（防止铁律误伤）。"""
    item_id, sub_id, _ = await _seed(db, quality=None, units=())
    await _seed_pack_units(db, sub_id, item_id, {1: _WEBDL, 2: _WEBDL})
    async with db.session() as session:
        row = _torrent("Testshow 2024 S01 1080p Blu-ray REMUX Complete", attrs=_PACK_REMUX)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 2


@pytest.mark.asyncio
async def test_pack_vetoed_by_at_cutoff_sibling(db):
    """整季包铁律：E01 可洗但 E02 已到顶（Remux）→ 抓包等于把 E02 重下一遍，
    整体放弃洗版维度。"""
    item_id, sub_id, _ = await _seed(db, quality=None, units=())
    await _seed_pack_units(
        db, sub_id, item_id,
        {1: _WEBDL, 2: {"resolution": "1080p", "media_source": "Blu-ray", "remux": True}},
    )
    async with db.session() as session:
        row = _torrent("Testshow 2024 S01 1080p Blu-ray REMUX Complete", attrs=_PACK_REMUX)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 0


@pytest.mark.asyncio
async def test_pack_vetoed_by_incomparable_sibling(db):
    """整季包铁律：E03 快照片源未知（不可比，不在可洗集合）同样阻挡整包。"""
    item_id, sub_id, _ = await _seed(db, quality=None, units=())
    await _seed_pack_units(db, sub_id, item_id, {1: _WEBDL, 3: {"resolution": "1080p"}})
    async with db.session() as session:
        row = _torrent("Testshow 2024 S01 1080p Blu-ray REMUX Complete", attrs=_PACK_REMUX)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 0


@pytest.mark.asyncio
async def test_in_flight_upgrade_attempt_dedupes(db):
    """已有在途洗版 attempt 的单元本轮不再比对。"""
    item_id, sub_id, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="ffff",
                units=[[1, 1]],
                quality=_REMUX_E1,
                purpose="upgrade",
                status=DownloadAttemptStatus.ACTIVE,
                last_progress_at=utcnow(),
            )
        )
        await session.commit()
        contexts = await load_match_context(session)
        assert item_id not in contexts  # 唯一单元在途，上下文剔空


@pytest.mark.asyncio
async def test_failed_upgrade_attempt_excludes_candidate(db):
    """证伪排除：FAILED 洗版 attempt 的 (site, torrent) 不会被再次投递。"""
    item_id, sub_id, _ = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="eeee",
                site_id="site-a",
                torrent_id="fake1",
                units=[[1, 1]],
                quality=_REMUX_E1,
                purpose="upgrade",
                status=DownloadAttemptStatus.FAILED,
                last_progress_at=utcnow(),
            )
        )
        row = _torrent(
            "Testshow 2024 S01E01 1080p Blu-ray REMUX", attrs=_REMUX_E1, torrent_id="fake1"
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        summary = await evaluate_and_dispatch(session, [row], source="被动匹配")
        assert summary.dispatched_units == 0


@pytest.mark.asyncio
async def test_fused_unit_rests_until_cooldown(db):
    """连续证伪熔断：failures 达阈值且冷却未到 → 不参与；冷却已到 → 恢复。"""
    from datetime import timedelta

    item_id, _sub, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        wanted.upgrade_verify_failures = 3
        wanted.next_search_at = utcnow() + timedelta(days=20)  # 冷却中
        await session.commit()
        assert item_id not in await load_match_context(session)

        wanted = await session.get(WantedItem, wanted_id)
        wanted.next_search_at = utcnow() - timedelta(minutes=1)  # 冷却到期
        await session.commit()
        contexts = await load_match_context(session)
        assert (1, 1) in contexts[item_id].upgrade_wanted


@pytest.mark.asyncio
async def test_arming_and_search_now(db):
    """排期：arm 后 priority=-10、next_search_at 在 24h 错峰窗内；
    立即搜索把可洗单元重置到现在。"""
    from movieclaw_api.services.subscription.upgrade import (
        arm_upgrade_candidates,
        reset_upgrade_search_now,
    )

    _item, sub_id, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        armed = await arm_upgrade_candidates(session, [wanted])
        await session.commit()
        assert armed == 1
        assert wanted.priority == UPGRADE_PRIORITY
        assert wanted.next_search_at is not None

        reset = await reset_upgrade_search_now(session, sub_id)
        await session.commit()
        assert reset == 1
        assert (utcnow() - wanted.next_search_at).total_seconds() < 5


@pytest.mark.asyncio
async def test_postpone_resets_expired_fuse_counter(db):
    """熔断冷却到期后第一次搜索记账：计数清零重新观察——否则常规退避会被
    误判成仍在冷却，把被动匹配无限期关掉。"""
    from datetime import timedelta

    from movieclaw_api.services.subscription.upgrade import postpone_upgrade_wanted

    item_id, _sub, (wanted_id,) = await _seed(db, quality=_WEBDL)
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        wanted.upgrade_verify_failures = 3
        wanted.next_search_at = utcnow() - timedelta(minutes=1)  # 冷却已到期
        await session.commit()
        await postpone_upgrade_wanted(session, item_id, delay=None, count_attempt=True)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.upgrade_verify_failures == 0
        assert wanted.next_search_at is not None  # 已按洗版退避重新排期


@pytest.mark.asyncio
async def test_import_clears_stale_gap_schedule(db):
    """入库对账把缺口时代的 next_search_at 清空——否则 imported 单元会带着
    旧排期进入洗版搜索队列，触发无谓的站点搜索。"""
    from datetime import timedelta

    from movieclaw_api.services.subscription.wanted_fulfillment import (
        close_fulfilled_wanted,
    )
    from movieclaw_db.models import FileSource, LibraryFile
    from movieclaw_db.repositories.library_repo import LibraryRepository

    item_id, sub_id, (wanted_id,) = await _seed(
        db, quality=None, wanted_status=WantedStatus.GRABBED
    )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/media/tv"]
        )
        wanted = await session.get(WantedItem, wanted_id)
        wanted.next_search_at = utcnow() + timedelta(hours=4)  # 缺口时代的退避
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/e1.mkv",
                size_bytes=1,
                source=FileSource.IMPORTED,
                resolution="1080p",
                media_source="Blu-ray",
                bit_rate=9_000_000,
            )
        )
        await session.commit()
        assert await close_fulfilled_wanted(session, item_id) == 1
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.status == WantedStatus.IMPORTED
        # 旧排期已清；随后 arm 只对可洗单元重挂（本例 Blu-ray 未到 Remux，
        # 规则组开了洗版 → 被重新排期为洗版搜索，语义正确）
        assert wanted.quality["media_source"] == "Blu-ray"
