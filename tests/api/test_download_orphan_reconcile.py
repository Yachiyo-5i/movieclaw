"""孤儿在途工单对账测试：attempt 已终态、工单仍在途的组补跑完成核验。

真实案例（十三邀 S06）：洗版混合投递在种子仍在下载时就被验证收口
（分批入库先交付洗版目标集，attempt 置 imported、离开心跳照看集合），
种子几小时后下载完成，落点/内容核验永远轮不到——包里根本不存在的集
（覆盖虚标）以 grabbed 永久挂起。
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import download_progress
from movieclaw_api.services.download_progress import _reconcile_orphaned_group
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    ActivityType,
    DownloadAttemptStatus,
    SubscriptionActivity,
    SubscriptionDownloadAttempt,
    WantedItem,
    WantedStatus,
)
from movieclaw_db.models.base import utcnow
from movieclaw_downloader import TorrentStatus
from movieclaw_downloader.models import TorrentFile
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"
_HASH = "59e5ba444a1fdc482c0e090764436cfc89f9691d"

_TODAY = utcnow().date()
_AIRED = (_TODAY - timedelta(days=30)).isoformat()

_ROUTES = {
    "/3/tv/300": {
        "id": 300,
        "name": "测试剧集",
        "original_name": "Test Show",
        "first_air_date": "2024-01-01",
        "status": "Returning Series",
        "external_ids": {},
        "alternative_titles": {"results": []},
        "translations": {"translations": []},
        "seasons": [{"season_number": 1}],
    },
    "/3/tv/300/season/1": {
        "name": "第 1 季",
        "air_date": _AIRED,
        "episodes": [
            {"episode_number": 1, "name": "E1", "air_date": _AIRED},
            {"episode_number": 2, "name": "E2", "air_date": _AIRED},
        ],
    },
}


def _fake_tmdb() -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _ROUTES.get(request.url.path)
        return httpx.Response(200 if payload else 404, json=payload or {})

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'orphan.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


class _FakeDownloaderRow:
    name = "测试下载器"
    path_mappings = None


async def _seed_orphan(session, *, attempt_status: DownloadAttemptStatus) -> int:
    """建订阅、全部工单在途、attempt 已是给定终态（无人照看）。"""
    service = SubscriptionService(session, MediaLibraryService(session, _fake_tmdb()))
    subscription = await service.create(MediaKind.TV, 300, selected_seasons=[1])
    assert subscription.id is not None
    rows = (
        (
            await session.execute(
                select(WantedItem).where(WantedItem.subscription_id == subscription.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows
    aged = utcnow() - timedelta(minutes=25)
    for row in rows:
        row.status = WantedStatus.GRABBED
        row.info_hash = _HASH
        row.grabbed_at = aged
        session.add(row)
    session.add(
        SubscriptionDownloadAttempt(
            subscription_id=subscription.id,
            info_hash=_HASH,
            units=[[r.season_number, r.episode_number] for r in rows],
            status=attempt_status,
            purpose="upgrade",
            last_progress_at=aged,
            last_observed_at=aged,
        )
    )
    await session.commit()
    return subscription.id


def _completed_status(tmp_path, file_paths: list[str]) -> TorrentStatus:
    root = file_paths[0].split("/")[0] if file_paths else "Show"
    (tmp_path / root).mkdir(parents=True, exist_ok=True)
    return TorrentStatus(
        info_hash=_HASH,
        name="Test.Show.Pack",
        progress=1.0,
        completed=True,
        save_path=str(tmp_path),
        files=[TorrentFile(path=p, size_bytes=1) for p in file_paths],
        state="completed",
    )


def _stub_query(monkeypatch, status: TorrentStatus | None) -> None:
    async def fake_query(info_hash, downloaders, *, include_files=True):
        return None if status is None else (_FakeDownloaderRow(), status)

    monkeypatch.setattr(download_progress, "_query_torrent", fake_query)


@pytest.mark.asyncio
async def test_orphaned_imported_attempt_requeues_missing(db, tmp_path, monkeypatch) -> None:
    """attempt 已 imported（洗版收口）、种子随后下载完成：缺失集退回重找
    并写负面记忆，清单里存在的集保持在途等对账。"""
    async with db.session() as session:
        sub_id = await _seed_orphan(session, attempt_status=DownloadAttemptStatus.IMPORTED)
    _stub_query(monkeypatch, _completed_status(tmp_path, ["Show/Show.S01E01.mkv"]))

    await _reconcile_orphaned_group(sub_id, _HASH, [(object(), object())])

    async with db.session() as session:
        rows = (
            (
                await session.execute(
                    select(WantedItem).where(WantedItem.subscription_id == sub_id)
                )
            )
            .scalars()
            .all()
        )
        wanted = {(w.season_number, w.episode_number): w for w in rows}
        assert wanted[(1, 2)].status == WantedStatus.WANTED  # 缺失集退回
        assert wanted[(1, 2)].info_hash is None
        assert wanted[(1, 1)].status == WantedStatus.GRABBED  # 存在的集等对账
        attempt = (
            await session.execute(
                select(SubscriptionDownloadAttempt).where(
                    SubscriptionDownloadAttempt.subscription_id == sub_id
                )
            )
        ).scalar_one()
        assert [1, 2] in (attempt.content_missing or {}).get("units", [])  # 负面记忆
        activities = (
            (
                await session.execute(
                    select(SubscriptionActivity).where(
                        SubscriptionActivity.subscription_id == sub_id,
                        SubscriptionActivity.type == ActivityType.DISPATCH_FAILED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(activities) == 1
        assert activities[0].payload["reason"] == "content_missing"


@pytest.mark.asyncio
async def test_orphaned_group_untouched_when_torrent_absent_or_incomplete(
    db, tmp_path, monkeypatch
) -> None:
    """证据不足不动：种子不在下载器里，或还没下载完成，孤儿组保持原样。"""
    async with db.session() as session:
        sub_id = await _seed_orphan(session, attempt_status=DownloadAttemptStatus.IMPORTED)

    _stub_query(monkeypatch, None)  # 下载器里查不到
    await _reconcile_orphaned_group(sub_id, _HASH, [(object(), object())])

    incomplete = _completed_status(tmp_path, ["Show/Show.S01E01.mkv"])
    incomplete = incomplete.model_copy(update={"completed": False, "progress": 0.5})
    _stub_query(monkeypatch, incomplete)
    await _reconcile_orphaned_group(sub_id, _HASH, [(object(), object())])

    async with db.session() as session:
        rows = (
            (
                await session.execute(
                    select(WantedItem).where(WantedItem.subscription_id == sub_id)
                )
            )
            .scalars()
            .all()
        )
        assert all(w.status == WantedStatus.GRABBED for w in rows)
        assert all(w.info_hash == _HASH for w in rows)
