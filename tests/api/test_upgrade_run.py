"""手动洗版测试（quality-upgrade.md §13）：存量工单物化原语 + 一轮洗版组合动作。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services.subscription.upgrade import (
    materialize_owned_wanted,
    run_upgrade_round,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionDownloadAttempt,
    SubscriptionStatus,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository

_WEBDL = {"resolution": "1080p", "media_source": "WEB-DL"}
_REMUX = {"resolution": "1080p", "media_source": "Blu-ray", "remux": True}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'run.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


@pytest_asyncio.fixture
def kicked(monkeypatch):
    """拦截 kick_search_soon（真实实现会在事件循环里起后台搜索任务）。"""
    calls: list[bool] = []
    from movieclaw_api.services.subscription import wanted_search

    monkeypatch.setattr(wanted_search, "kick_search_soon", lambda: calls.append(True))
    return calls


async def _seed(
    db,
    *,
    spec=None,
    episodes=((1, 1), (1, 2), (1, 3)),
    status=SubscriptionStatus.COMPLETED,
):
    """库/条目（含集数据）/订阅 的最小闭包；集全部已播出。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/media/tv"]
        )
        item = MediaItem(kind="tv", tmdb_id=300, title="存量剧", original_title="Stock", year=2020)
        rule_set = RuleSet(
            name="洗版组", spec={"upgrade_source": "remux"} if spec is None else spec
        )
        session.add_all([item, rule_set])
        await session.commit()
        await session.refresh(item)
        await session.refresh(rule_set)
        for season, episode in episodes:
            session.add(
                MediaEpisode(
                    media_item_id=item.id,
                    season_number=season,
                    episode_number=episode,
                    air_date=date(2020, 1, episode),
                )
            )
        sub = Subscription(
            media_item_id=item.id,
            kind="tv",
            rule_set_id=rule_set.id,
            library_id=library.id,
            selected_seasons=[1],
            status=status,
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        return library.id, item.id, sub.id, rule_set.id


def _file(library_id, item_id, season, episode, **attrs):
    return LibraryFile(
        library_id=library_id,
        media_item_id=item_id,
        season_number=season,
        episode_number=episode,
        file_path=f"/media/tv/Stock/S{season:02d}E{episode:02d}.mkv",
        size_bytes=1,
        source=FileSource.SCANNED,
        **attrs,
    )


async def _add_wanted(session, sub_id, item_id, season, episode, **kwargs):
    kwargs.setdefault("status", WantedStatus.IMPORTED)
    if kwargs["status"] == WantedStatus.IMPORTED:
        kwargs.setdefault("imported_at", utcnow())
    row = WantedItem(
        subscription_id=sub_id,
        media_item_id=item_id,
        season_number=season,
        episode_number=episode,
        **kwargs,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# §13.1 物化原语
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_creates_rows_only_for_owned_units(db):
    """库里在位的单元补建 imported 行；缺失单元不建（那是订阅缺口逻辑的事）。"""
    library_id, item_id, sub_id, _rs = await _seed(db)
    async with db.session() as session:
        f1 = _file(library_id, item_id, 1, 1)
        f2 = _file(library_id, item_id, 1, 2)
        session.add_all([f1, f2])
        await session.commit()
        await session.refresh(f1)

        sub = await session.get(Subscription, sub_id)
        created = await materialize_owned_wanted(session, sub)
        await session.commit()

        assert {(w.season_number, w.episode_number) for w in created} == {(1, 1), (1, 2)}
        assert all(w.status == WantedStatus.IMPORTED and w.in_scope for w in created)
        assert all(w.quality is None for w in created)  # 快照留给回填/一轮洗版补
        e1 = next(w for w in created if w.episode_number == 1)
        assert e1.imported_at == f1.created_at  # 取文件真实入库时间
        assert e1.air_date == date(2020, 1, 1)


@pytest.mark.asyncio
async def test_materialize_idempotent_and_respects_existing_rows(db):
    """已有行（含 in_scope=False 的历史行）一律不动；重复调用零新建。"""
    library_id, item_id, sub_id, _rs = await _seed(db)
    async with db.session() as session:
        session.add_all(
            [_file(library_id, item_id, 1, 1), _file(library_id, item_id, 1, 2)]
        )
        await session.commit()
        existing = await _add_wanted(
            session, sub_id, item_id, 1, 1, in_scope=False, quality=_WEBDL
        )

        sub = await session.get(Subscription, sub_id)
        created = await materialize_owned_wanted(session, sub)
        assert {(w.season_number, w.episode_number) for w in created} == {(1, 2)}
        assert not await materialize_owned_wanted(session, sub)  # 幂等
        await session.commit()

        row = await session.get(WantedItem, existing.id)
        assert row.in_scope is False and row.quality == _WEBDL  # 历史行原样


# ---------------------------------------------------------------------------
# §13.2 一轮洗版
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_rejects_paused_subscription(db, kicked):
    _lib, _item, sub_id, _rs = await _seed(db, status=SubscriptionStatus.PAUSED)
    async with db.session() as session:
        with pytest.raises(BadRequestException, match="已暂停"):
            await run_upgrade_round(session, sub_id)


@pytest.mark.asyncio
async def test_run_rejects_rule_set_without_upgrade_target(db, kicked):
    """换入的组和现用的组都必须配了洗版目标，否则中文报错引导配置。"""
    _lib, _item, sub_id, _rs = await _seed(db, spec={})
    async with db.session() as session:
        plain = RuleSet(name="无洗版组", spec={})
        session.add(plain)
        await session.commit()
        await session.refresh(plain)
        with pytest.raises(BadRequestException, match="未配置洗版目标"):
            await run_upgrade_round(session, sub_id, rule_set_id=plain.id)
        with pytest.raises(BadRequestException, match="未配置洗版目标"):
            await run_upgrade_round(session, sub_id)


@pytest.mark.asyncio
async def test_run_switches_rule_set_and_classifies_units(db, kicked):
    """换组 + 逐集体检：可洗/到顶/无法确认/缺失 各归各位；可洗单元
    priority=0 且立即到期；缺失单元不动（照常走缺口逻辑）。"""
    library_id, item_id, sub_id, _rs = await _seed(
        db, spec={}, episodes=((1, 1), (1, 2), (1, 3), (1, 4), (1, 5))
    )
    async with db.session() as session:
        upgrade_rs = RuleSet(name="换入洗版组", spec={"upgrade_source": "remux"})
        session.add(upgrade_rs)
        # 体检要看快照，给已入库单元配文件（物化不会重建已有行，但
        # E4 缺失、无文件）
        session.add_all(
            [
                _file(library_id, item_id, 1, 1),
                _file(library_id, item_id, 1, 2),
                _file(library_id, item_id, 1, 3),
                _file(library_id, item_id, 1, 5),
            ]
        )
        await session.commit()
        await session.refresh(upgrade_rs)
        w1 = await _add_wanted(session, sub_id, item_id, 1, 1, quality=_WEBDL)
        await _add_wanted(session, sub_id, item_id, 1, 2, quality=_REMUX)
        await _add_wanted(session, sub_id, item_id, 1, 3, quality={})
        w4 = await _add_wanted(
            session, sub_id, item_id, 1, 4, status=WantedStatus.WANTED
        )
        # 第三态：同分辨率但片源未知——证明不了低于目标也证明不了达标，
        # 必须报"无法确认"而不是冒充"已达目标"
        await _add_wanted(session, sub_id, item_id, 1, 5, quality={"resolution": "1080p"})

        before = utcnow()
        report = await run_upgrade_round(session, sub_id, rule_set_id=upgrade_rs.id)

        assert report["rule_set_id"] == upgrade_rs.id
        assert report["counts"] == {
            "upgradable": 1,
            "at_cutoff": 1,
            "in_flight": 0,
            "not_comparable": 2,
            "missing": 1,
        }
        states = {
            (u["season_number"], u["episode_number"]): u["state"] for u in report["units"]
        }
        assert states == {
            (1, 1): "upgradable",
            (1, 2): "at_cutoff",
            (1, 3): "not_comparable",
            (1, 4): "missing",
            (1, 5): "not_comparable",
        }
        assert report["target_label"]
        assert "可洗版" in report["summary"]

        sub = await session.get(Subscription, sub_id)
        assert sub.rule_set_id == upgrade_rs.id
        row = await session.get(WantedItem, w1.id)
        assert row.priority == 0 and row.next_search_at is not None
        assert row.next_search_at <= utcnow() and row.next_search_at >= before
        missing = await session.get(WantedItem, w4.id)
        assert missing.status == WantedStatus.WANTED  # 缺失行不被体检改写
        assert kicked  # 有可洗+缺失 → 踢了一次搜索


@pytest.mark.asyncio
async def test_run_materializes_and_fills_snapshot_inline(db, kicked):
    """存量内容（有文件无工单）：一轮洗版当场物化 + 补快照，体检直接可见。"""
    library_id, item_id, sub_id, _rs = await _seed(db, episodes=((1, 1),))
    async with db.session() as session:
        session.add(
            _file(
                library_id,
                item_id,
                1,
                1,
                resolution="1080p",
                media_source="WEB-DL",
                bit_rate=5_000_000,
            )
        )
        await session.commit()

        report = await run_upgrade_round(session, sub_id)
        assert report["counts"]["upgradable"] == 1
        wanted = (
            await session.execute(
                select(WantedItem).where(WantedItem.subscription_id == sub_id)
            )
        ).scalar_one()
        assert wanted.status == WantedStatus.IMPORTED
        assert wanted.quality["media_source"] == "WEB-DL"
        assert wanted.priority == 0 and wanted.next_search_at is not None


@pytest.mark.asyncio
async def test_run_reports_in_flight_units_untouched(db, kicked):
    """已在洗版在途的单元如实标注 in_flight，不重复排期、不踢搜索。"""
    library_id, item_id, sub_id, _rs = await _seed(db, episodes=((1, 1),))
    async with db.session() as session:
        session.add(_file(library_id, item_id, 1, 1))
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash="upg111",
                units=[[1, 1]],
                purpose="upgrade",
                last_progress_at=utcnow(),
            )
        )
        await session.commit()
        w1 = await _add_wanted(session, sub_id, item_id, 1, 1, quality=_WEBDL)

        report = await run_upgrade_round(session, sub_id)
        assert report["counts"] == {
            "upgradable": 0,
            "at_cutoff": 0,
            "in_flight": 1,
            "not_comparable": 0,
            "missing": 0,
        }
        row = await session.get(WantedItem, w1.id)
        assert row.next_search_at is None  # 未被重复排期
        assert not kicked


@pytest.mark.asyncio
async def test_run_defuses_fused_unit(db, kicked):
    """熔断中的单元：手动触发视为人工介入，清零证伪计数并立即排期。"""
    library_id, item_id, sub_id, _rs = await _seed(db, episodes=((1, 1),))
    async with db.session() as session:
        session.add(_file(library_id, item_id, 1, 1))
        await session.commit()
        w1 = await _add_wanted(
            session,
            sub_id,
            item_id,
            1,
            1,
            quality=_WEBDL,
            upgrade_verify_failures=3,
            next_search_at=utcnow() + timedelta(days=30),
        )

        report = await run_upgrade_round(session, sub_id)
        assert report["counts"]["upgradable"] == 1
        row = await session.get(WantedItem, w1.id)
        assert row.upgrade_verify_failures == 0
        assert row.next_search_at <= utcnow()
        assert kicked
