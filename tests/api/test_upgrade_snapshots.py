"""洗版基线快照测试（quality-upgrade.md §4.1/§4.4：实测优先、出处采信名称、
存量回填纯 DB 变换）。"""

from __future__ import annotations

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription.upgrade import backfill_upgrade_snapshots
from movieclaw_api.services.subscription.wanted_fulfillment import close_fulfilled_wanted
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    LibraryFile,
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'upg.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(db, *, rule_spec=None, wanted_status=WantedStatus.GRABBED, info_hash="abc123"):
    """建 库/条目/订阅/工单 的最小闭包。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/media/tv"]
        )
        item = MediaItem(kind="tv", tmdb_id=200, title="测试剧集", original_title="Test", year=2024)
        rule_set = RuleSet(name="默认", spec=rule_spec or {})
        session.add_all([item, rule_set])
        await session.commit()
        await session.refresh(item)
        await session.refresh(rule_set)
        sub = Subscription(
            media_item_id=item.id, kind="tv", rule_set_id=rule_set.id, library_id=library.id
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        wanted = WantedItem(
            subscription_id=sub.id,
            media_item_id=item.id,
            season_number=1,
            episode_number=1,
            status=wanted_status,
            info_hash=info_hash,
            grabbed_at=utcnow(),
        )
        session.add(wanted)
        await session.commit()
        await session.refresh(wanted)
        return library.id, item.id, sub.id, wanted.id


@pytest.mark.asyncio
async def test_fulfillment_snapshot_probe_overrides_name(db):
    """入库对账落快照：分辨率/HDR 以实测为准（种子标称 2160p 实测 1080p），
    片源/制作组采信投递时的种子名解析（attempt.quality）。"""
    library_id, item_id, sub_id, wanted_id = await _seed(db)
    async with db.session() as session:
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="abc123",
                units=[[1, 1]],
                quality={"resolution": "2160p", "media_source": "WEB-DL", "release_group": "FAKE"},
                last_progress_at=utcnow(),
            )
        )
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/测试剧集 (2024)/Season 01/S01E01.mkv",
                size_bytes=1,
                source=FileSource.IMPORTED,
                resolution="1080p",
                hdr="Dolby Vision",
                bit_rate=8_000_000,
            )
        )
        await session.commit()
        assert await close_fulfilled_wanted(session, item_id) == 1
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality == {
            "resolution": "1080p",  # 实测覆盖名称的 2160p 虚标
            "media_source": "WEB-DL",  # 出处采信名称
            "release_group": "FAKE",
            "hdr": ["DV"],  # probe 的 "Dolby Vision" 归一为词表值
            "bit_rate": 8_000_000,
        }


@pytest.mark.asyncio
async def test_fulfillment_snapshot_without_attempt_uses_filename_enrich(db):
    """手工/扫描入库（无投递记录）：出处维度对文件名重跑 enrich；
    probe 完全失败时不冒充实测，分辨率取名称解析值。"""
    library_id, item_id, _sub_id, wanted_id = await _seed(db, info_hash=None)
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/Test.2024.S01E01.1080p.WEB-DL.H264-GRP.mkv",
                size_bytes=1,
                source=FileSource.SCANNED,
            )
        )
        await session.commit()
        assert await close_fulfilled_wanted(session, item_id) == 1
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality["resolution"] == "1080p"
        assert wanted.quality["media_source"] == "WEB-DL"


@pytest.mark.asyncio
async def test_backfill_fills_only_upgrade_enabled_rule_sets(db):
    """存量回填只处理配置了洗版目标的规则组引用的订阅；处理后不再重复。"""
    library_id, item_id, _sub_id, wanted_id = await _seed(
        db, rule_spec={"upgrade_source": "remux"}, wanted_status=WantedStatus.IMPORTED
    )
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path="/media/tv/e1.mkv",
                size_bytes=1,
                source=FileSource.SCANNED,
                resolution="1080p",
                media_source="WEB-DL",
                bit_rate=5_000_000,
            )
        )
        await session.commit()

    await backfill_upgrade_snapshots()
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality is not None
        assert wanted.quality["resolution"] == "1080p"
        assert wanted.quality["media_source"] == "WEB-DL"


@pytest.mark.asyncio
async def test_backfill_skips_rule_sets_without_upgrade(db):
    """未配置洗版目标：历史单元保持 NULL，不做无谓回填。"""
    _library_id, _item_id, _sub_id, wanted_id = await _seed(
        db, rule_spec={}, wanted_status=WantedStatus.IMPORTED
    )
    await backfill_upgrade_snapshots()
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality is None


@pytest.mark.asyncio
async def test_backfill_marks_unresolvable_unit_with_sentinel(db):
    """在位文件缺失（imported 但文件已丢）：写 {} 哨兵，避免每 tick 重试。"""
    _library_id, _item_id, _sub_id, wanted_id = await _seed(
        db, rule_spec={"upgrade_source": "remux"}, wanted_status=WantedStatus.IMPORTED
    )
    await backfill_upgrade_snapshots()
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality == {}
