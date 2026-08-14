"""追新发布时间预测：存量观测推导、保守降级与站点同步规划。"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription import refresh_release_forecasts
from movieclaw_api.services.torrent_sync import _plan_sync
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    MediaEpisode,
    MediaItem,
    RuleSet,
    SiteSyncCursor,
    SiteTorrent,
    Subscription,
    SubscriptionStatus,
    TorrentSource,
    WantedItem,
    WantedStatus,
    utcnow,
)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'forecast.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed_target(session, *, cadence_days: int = 7) -> tuple[WantedItem, datetime]:
    """建立 E1 已发布、E2 待追的最小周播/日播样本。"""
    today = utcnow().date()
    first_air = today - timedelta(days=1)
    target_air = first_air + timedelta(days=cadence_days)
    first_publish = datetime.combine(first_air, time(15, 35))

    rule = RuleSet(name=f"预测规则-{cadence_days}", is_default=True, spec={})
    item = MediaItem(
        kind="tv",
        tmdb_id=9000 + cadence_days,
        title="测试剧集",
        original_title="Test Show",
        year=today.year,
        aliases=["测试剧集", "Test Show"],
        status="Returning Series",
    )
    session.add_all([rule, item])
    await session.commit()
    await session.refresh(rule)
    await session.refresh(item)
    assert rule.id is not None and item.id is not None

    session.add_all(
        [
            MediaEpisode(
                media_item_id=item.id,
                season_number=1,
                episode_number=1,
                name="E1",
                air_date=first_air,
            ),
            MediaEpisode(
                media_item_id=item.id,
                season_number=1,
                episode_number=2,
                name="E2",
                air_date=target_air,
            ),
        ]
    )
    subscription = Subscription(
        media_item_id=item.id,
        kind="tv",
        selected_seasons=[],
        follow_future=True,
        rule_set_id=rule.id,
        status=SubscriptionStatus.ACTIVE,
    )
    session.add(subscription)
    await session.commit()
    await session.refresh(subscription)
    assert subscription.id is not None

    wanted = WantedItem(
        subscription_id=subscription.id,
        media_item_id=item.id,
        season_number=1,
        episode_number=2,
        status=WantedStatus.WANTED,
        air_date=target_air,
        next_search_at=datetime.combine(target_air, time()) + timedelta(hours=48),
    )
    torrent = SiteTorrent(
        site_id="site-a",
        torrent_id=f"episode-1-{cadence_days}",
        title="Test Show S01E01 1080p WEB-DL",
        attrs={
            "media_type": "tv",
            "year": today.year,
            "seasons": [1],
            "episodes": [1],
            "resolution": "1080p",
        },
        enrich_version=1,
        source=TorrentSource.LIST,
        publish_time=first_publish,
    )
    session.add_all([wanted, torrent])
    await session.commit()
    await session.refresh(wanted)
    return wanted, first_publish + timedelta(days=cadence_days)


@pytest.mark.parametrize("cadence_days", [1, 7])
async def test_e2_bootstrap_predicts_daily_and_weekly_release(db, cadence_days: int) -> None:
    """一个同季前序样本即可从 E2 启动，日播/周播由实际档期差自然决定。"""
    async with db.session() as session:
        wanted, expected = await _seed_target(session, cadence_days=cadence_days)
        changed = await refresh_release_forecasts(
            session, media_item_ids={wanted.media_item_id}
        )

        assert changed == 1
        assert wanted.release_forecast is not None
        forecast = wanted.release_forecast
        assert forecast["confidence"] == "bootstrap"
        assert forecast["sample_count"] == 1
        assert forecast["cadence_days"] == cadence_days
        assert datetime.fromisoformat(forecast["predicted_at"]).astimezone(UTC).replace(
            tzinfo=None
        ) == expected
        assert forecast["basis_units"] == [[1, 1]]
        assert forecast["sites"][0]["site_id"] == "site-a"
        assert len(forecast["sites"][0]["probe_times"]) == 3


async def test_pack_is_not_used_as_single_episode_observation(db) -> None:
    """整季包发布时间无法代表某一集，必须排除，避免污染下一集预测。"""
    async with db.session() as session:
        wanted, _expected = await _seed_target(session)
        torrent = (await session.execute(select(SiteTorrent))).scalar_one()
        assert torrent is not None
        torrent.attrs = {
            "media_type": "tv",
            "year": utcnow().year,
            "seasons": [1],
            "complete": True,
            "resolution": "1080p",
        }
        session.add(torrent)
        await session.commit()

        changed = await refresh_release_forecasts(
            session, media_item_ids={wanted.media_item_id}
        )
        assert changed == 0
        assert wanted.release_forecast is None


async def test_prediction_probe_advances_existing_site_sync(db) -> None:
    """预测只提前既有同步；普通同步消费探测点，暂停订阅后也立即失效。"""
    async with db.session() as session:
        wanted, _expected = await _seed_target(session)
        now = utcnow()
        wanted.release_forecast = {
            "version": 1,
            "target_air_date": wanted.air_date.isoformat(),
            "confidence": "bootstrap",
            "window_end": (now + timedelta(hours=1)).replace(tzinfo=UTC).isoformat(),
            "sites": [
                {
                    "site_id": "site-disabled",
                    "probe_times": [
                        (now - timedelta(minutes=2)).replace(tzinfo=UTC).isoformat()
                    ],
                },
                {
                    "site_id": "site-a",
                    "probe_times": [
                        (now - timedelta(minutes=1)).replace(tzinfo=UTC).isoformat()
                    ],
                }
            ],
        }
        cursor = SiteSyncCursor(
            site_id="site-a",
            tracking_since=now - timedelta(days=1),
            last_sync_at=now - timedelta(hours=1),
            next_sync_at=now + timedelta(hours=6),
        )
        session.add_all([wanted, cursor])
        await session.commit()

    site = SimpleNamespace(site_id="site-a")
    due, _wait = await _plan_sync([site])  # type: ignore[list-item]
    assert due == [site]

    async with db.session() as session:
        cursor = (
            await session.execute(
                select(SiteSyncCursor).where(SiteSyncCursor.site_id == "site-a")
            )
        ).scalar_one()
        cursor.last_sync_at = now - timedelta(minutes=5)
        session.add(cursor)
        await session.commit()
    due, _wait = await _plan_sync([site])  # type: ignore[list-item]
    assert due == []  # 探测点虽已到，但距上次同步不足 15 分钟，先遵守礼貌间隔

    async with db.session() as session:
        cursor = (
            await session.execute(
                select(SiteSyncCursor).where(SiteSyncCursor.site_id == "site-a")
            )
        ).scalar_one()
        cursor.last_sync_at = utcnow()
        session.add(cursor)
        await session.commit()
    due, _wait = await _plan_sync([site])  # type: ignore[list-item]
    assert due == []  # 普通同步晚于探测点，同样视为已经消费

    async with db.session() as session:
        cursor = (
            await session.execute(
                select(SiteSyncCursor).where(SiteSyncCursor.site_id == "site-a")
            )
        ).scalar_one()
        subscription = (await session.execute(select(Subscription))).scalar_one()
        cursor.last_sync_at = now - timedelta(hours=1)
        subscription.status = SubscriptionStatus.PAUSED
        session.add_all([cursor, subscription])
        await session.commit()
    due, _wait = await _plan_sync([site])  # type: ignore[list-item]
    assert due == []
