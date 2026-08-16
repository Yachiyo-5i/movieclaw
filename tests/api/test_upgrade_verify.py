"""洗版入库验证测试（quality-upgrade.md §6.3/§7）：实测确认、证伪排除、
熔断、旧版本回收站、旧任务清理通道、手工升级路径。"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription.wanted_fulfillment import close_fulfilled_wanted
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    DownloadAttemptStatus,
    FileSource,
    LibraryFile,
    MediaItem,
    RuleSet,
    Subscription,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    SystemNotice,
    WantedItem,
    WantedStatus,
    utcnow,
)
from movieclaw_db.repositories.library_repo import LibraryRepository

_WEBDL = {"resolution": "1080p", "media_source": "WEB-DL"}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'verify.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(db, tmp_path, *, quality=_WEBDL, old_hash="old1"):
    """库(真实 tmp 根)/条目/订阅/imported 工单 + 旧版本物理文件。"""
    root = tmp_path / "tv"
    root.mkdir(exist_ok=True)
    old_file = root / "Testshow.S01E01.1080p.WEB-DL.mkv"
    old_file.write_bytes(b"old")
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=[str(root)]
        )
        item = MediaItem(
            kind="tv", tmdb_id=200, title="测试剧集", original_title="Testshow", year=2024,
            aliases=["Testshow"],
        )
        rule_set = RuleSet(name="默认", spec={"upgrade_source": "remux"})
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
            status=WantedStatus.IMPORTED,
            quality=quality,
            info_hash=old_hash,
            imported_at=utcnow(),
        )
        session.add(wanted)
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item.id,
                season_number=1,
                episode_number=1,
                file_path=str(old_file),
                size_bytes=3,
                source=FileSource.IMPORTED,
                resolution="1080p",
                media_source="WEB-DL",
                bit_rate=5_000_000,
            )
        )
        # 旧版本对应的下载 attempt（洗版确认后应进清理通道）
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub.id,
                info_hash=old_hash,
                units=[[1, 1]],
                quality=_WEBDL,
                status=DownloadAttemptStatus.COMPLETED,
                last_progress_at=utcnow(),
            )
        )
        await session.commit()
        await session.refresh(wanted)
        return library, item.id, sub.id, wanted.id, root, old_file


async def _add_upgrade_delivery(
    db, sub_id, item_id, library_id, root, *, claimed_quality, probed, new_hash="new1"
):
    """模拟洗版 attempt + 其下载文件入库：新 library_file 行 + attempt。"""
    new_file = root / "Testshow.S01E01.1080p.REMUX.mkv"
    new_file.write_bytes(b"new-version")
    async with db.session() as session:
        session.add(
            SubscriptionDownloadAttempt(
                subscription_id=sub_id,
                info_hash=new_hash,
                site_id="site-a",
                torrent_id="up1",
                units=[[1, 1]],
                quality=claimed_quality,
                purpose="upgrade",
                status=DownloadAttemptStatus.COMPLETED,
                last_progress_at=utcnow(),
            )
        )
        session.add(
            LibraryFile(
                library_id=library_id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path=str(new_file),
                size_bytes=11,
                source=FileSource.IMPORTED,
                site_id="site-a",
                torrent_id="up1",
                **probed,
            )
        )
        await session.commit()
    return new_file


@pytest.mark.asyncio
async def test_confirm_upgrade_replaces_old_and_chains_cleanup(db, tmp_path):
    """实测确认：快照刷新、info_hash 切换、旧文件进回收站、旧 attempt 进
    清理通道、UPGRADED 活动。"""
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(db, tmp_path)
    await _add_upgrade_delivery(
        db, sub_id, item_id, library.id, root,
        claimed_quality={"resolution": "1080p", "media_source": "Blu-ray", "remux": True},
        probed={"resolution": "1080p", "bit_rate": 30_000_000},
    )
    async with db.session() as session:
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality["remux"] is True
        assert wanted.quality["media_source"] == "Blu-ray"
        assert wanted.info_hash == "new1"
        assert wanted.upgrade_verify_failures == 0
        # 旧文件物理移入回收站
        assert not old_file.exists()
        assert (root / ".movieclaw-trash").is_dir()
        assert len(list((root / ".movieclaw-trash").iterdir())) == 1
        # 旧 attempt 进入清理通道，新 attempt 完结并记录替换关系
        old_attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "old1"
                )
            )
        ).scalar_one()
        new_attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "new1"
                )
            )
        ).scalar_one()
        assert old_attempt.status == DownloadAttemptStatus.CLEANUP_PENDING
        assert new_attempt.status == DownloadAttemptStatus.IMPORTED
        assert new_attempt.replaces_attempt_id == old_attempt.id
        # 活动
        acts = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.type == "upgraded"
                    )
                )
            ).scalars()
        )
        assert len(acts) == 1
        assert "1080p WEB-DL → 1080p Remux" in acts[0].message


@pytest.mark.asyncio
async def test_refuted_upgrade_trashes_new_file_and_excludes(db, tmp_path):
    """证伪：标称 Remux 实测仍是 WEB-DL 档 → 新文件进回收站、旧文件不动、
    attempt=FAILED、熔断计数 +1、UPGRADE_VERIFY_FAILED 活动。"""
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(db, tmp_path)
    new_file = await _add_upgrade_delivery(
        db, sub_id, item_id, library.id, root,
        # 纯分辨率虚标：标称 2160p WEB-DL，实测 1080p → 与基线同档，证伪
        # （片源声明如实采信是 §4.2 明确接受的残余风险，不在证伪范围）
        claimed_quality={"resolution": "2160p", "media_source": "WEB-DL"},
        probed={"resolution": "1080p", "bit_rate": 6_000_000},
    )
    async with db.session() as session:
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality == _WEBDL  # 基线不动
        assert wanted.info_hash == "old1"
        assert wanted.upgrade_verify_failures == 1
        assert old_file.exists()  # 旧版本原样在位
        assert not new_file.exists()  # 证伪文件移入回收站
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "new1"
                )
            )
        ).scalar_one()
        assert attempt.status == DownloadAttemptStatus.FAILED
        acts = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.type == "upgrade_verify_failed"
                    )
                )
            ).scalars()
        )
        assert len(acts) == 1
        assert "实测" in acts[0].message


@pytest.mark.asyncio
async def test_third_refutation_fuses_and_notices(db, tmp_path):
    """连续第 3 次证伪：转入 30 天冷却 + system_notice。"""
    library, item_id, sub_id, wanted_id, root, _old = await _seed(db, tmp_path)
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        wanted.upgrade_verify_failures = 2
        await session.commit()
    await _add_upgrade_delivery(
        db, sub_id, item_id, library.id, root,
        claimed_quality={"resolution": "2160p", "media_source": "WEB-DL"},
        probed={"resolution": "1080p", "bit_rate": 6_000_000},
    )
    async with db.session() as session:
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.upgrade_verify_failures == 3
        assert wanted.next_search_at is not None
        assert (wanted.next_search_at - utcnow()).days >= 29
        notice = (
            await session.execute(
                select(SystemNotice).where(
                    SystemNotice.dedupe_key == f"subscription.upgrade:{sub_id}:1:1"
                )
            )
        ).scalar_one()
        assert "连续 3 次" in notice.message


@pytest.mark.asyncio
async def test_manual_better_file_confirms_without_attempt(db, tmp_path):
    """手工塞入更优文件（无 attempt）：实测为证同样确认，快照刷新、旧文件
    进回收站、info_hash 保持（没有新种子可关联）。"""
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(db, tmp_path)
    manual = root / "Testshow.S01E01.2160p.WEB-DL.mkv"
    manual.write_bytes(b"manual-4k")
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path=str(manual),
                size_bytes=9,
                source=FileSource.SCANNED,
                resolution="2160p",
                media_source="WEB-DL",
                bit_rate=20_000_000,
            )
        )
        await session.commit()
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality["resolution"] == "2160p"
        assert wanted.info_hash == "old1"  # 无新 attempt 可关联，保持
        assert not old_file.exists() and manual.exists()


@pytest.mark.asyncio
async def test_equal_manual_file_only_collected(db, tmp_path):
    """手工塞入同档文件：仅收编为多版本，不动快照、不触发任何洗版活动。"""
    library, item_id, _sub, wanted_id, root, old_file = await _seed(db, tmp_path)
    dup = root / "Testshow.S01E01.1080p.WEB-DL.DUP.mkv"
    dup.write_bytes(b"dup")
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path=str(dup),
                size_bytes=3,
                source=FileSource.SCANNED,
                resolution="1080p",
                media_source="WEB-DL",
                bit_rate=5_000_000,
            )
        )
        await session.commit()
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality == _WEBDL
        assert old_file.exists() and dup.exists()  # 两个版本都保留
        acts = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.type.in_(("upgraded", "upgrade_verify_failed"))  # type: ignore[attr-defined]
                    )
                )
            ).scalars()
        )
        assert acts == []


@pytest.mark.asyncio
async def test_no_upgrade_rule_never_touches_files(db, tmp_path):
    """未开洗版的规则组：手工塞入更优文件只静默刷新基线，绝不移动/删除
    任何文件（删除性动作必须有洗版目标这个显式 opt-in）。"""
    library, item_id, _sub, wanted_id, root, old_file = await _seed(db, tmp_path)
    async with db.session() as session:
        rule_set = (await session.execute(select(RuleSet))).scalar_one()
        rule_set.spec = {}  # 关掉洗版
        await session.commit()
    better = root / "Testshow.S01E01.2160p.WEB-DL.mkv"
    better.write_bytes(b"manual-4k")
    async with db.session() as session:
        session.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=item_id,
                season_number=1,
                episode_number=1,
                file_path=str(better),
                size_bytes=9,
                source=FileSource.SCANNED,
                resolution="2160p",
                media_source="WEB-DL",
                bit_rate=20_000_000,
            )
        )
        await session.commit()
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.quality["resolution"] == "2160p"  # 基线静默刷新
        assert old_file.exists() and better.exists()  # 两份文件都原地不动
        assert not (root / ".movieclaw-trash").exists()
        acts = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.type.in_(("upgraded", "upgrade_verify_failed"))  # type: ignore[attr-defined]
                    )
                )
            ).scalars()
        )
        assert acts == []


@pytest.mark.asyncio
async def test_mixed_attempt_gap_unit_not_refuted(db, tmp_path):
    """混合投递（purpose=upgrade 的 attempt 同时覆盖缺口单元）：靠这次投递
    才入库的缺口单元不能走证伪分支——判据是单元入库时间晚于 attempt 创建。"""
    from datetime import timedelta as _td

    library, item_id, sub_id, wanted_id, root, _old = await _seed(db, tmp_path)
    async with db.session() as session:
        wanted = await session.get(WantedItem, wanted_id)
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "old1"
                )
            )
        ).scalar_one()
        # 模拟：attempt 是混合洗版投递，单元靠它入库（imported_at 晚于 attempt）
        attempt.purpose = "upgrade"
        attempt.created_at = utcnow() - _td(minutes=10)
        wanted.imported_at = utcnow()
        await session.commit()
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "old1"
                )
            )
        ).scalar_one()
        assert wanted.upgrade_verify_failures == 0  # 没有被误证伪
        assert attempt.status == DownloadAttemptStatus.COMPLETED  # 未被打成 FAILED


@pytest.mark.asyncio
async def test_honest_resource_superseded_is_cancelled_not_refuted(db, tmp_path):
    """诚实资源被抢先 ≠ 证伪：下载期间基线被手工入库的更优版本刷高，
    洗版结果落地时已不再需要——attempt 收口为 CANCELLED，不计熔断、
    不进排除清单、不写证伪活动。"""
    library, item_id, sub_id, wanted_id, root, old_file = await _seed(db, tmp_path)
    async with db.session() as session:
        # 基线已被手工 Remux 抢先刷高
        wanted = await session.get(WantedItem, wanted_id)
        wanted.quality = {"resolution": "1080p", "media_source": "Blu-ray", "remux": True}
        await session.commit()
    # 洗版 attempt 诚实交付了它声称的 Blu-ray 重编码（低于新基线但符合声称）
    new_file = await _add_upgrade_delivery(
        db, sub_id, item_id, library.id, root,
        claimed_quality={"resolution": "1080p", "media_source": "Blu-ray"},
        probed={"resolution": "1080p", "bit_rate": 15_000_000},
    )
    async with db.session() as session:
        await close_fulfilled_wanted(session, item_id)
        wanted = await session.get(WantedItem, wanted_id)
        assert wanted.upgrade_verify_failures == 0  # 不计熔断
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.info_hash == "new1"
                )
            )
        ).scalar_one()
        assert attempt.status == DownloadAttemptStatus.CANCELLED  # 不是 FAILED
        assert "抢先" in (attempt.cleanup_note or "")
        assert not new_file.exists()  # 不需要的结果仍进回收站
        acts = list(
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.type == "upgrade_verify_failed"
                    )
                )
            ).scalars()
        )
        assert acts == []  # 没有错误的证伪指控
