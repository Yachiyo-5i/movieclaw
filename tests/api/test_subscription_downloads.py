"""订阅实时下载进度快照（subscriptions.list-active-downloads 的服务层）测试。

只测快照的组装语义：在途工单分组、下载器命中/失联两种形态、字段透传。
下载器查询本身在 tests/downloader 里另测，这里一律打桩。
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import download_progress
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.subscription import SubscriptionService
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import WantedItem, WantedStatus
from movieclaw_db.models.base import utcnow
from movieclaw_downloader import TorrentStatus
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"
_AIRED = (utcnow().date() - timedelta(days=10)).isoformat()

_ROUTES = {
    "/3/movie/100": {
        "id": 100,
        "title": "测试电影",
        "original_title": "Test Movie",
        "release_date": _AIRED,
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
    },
}


def _fake_tmdb() -> TmdbClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _ROUTES.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=payload)

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'dl.db'}")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings.database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


class _FakeDownloaderRow:
    name = "测试下载器"


async def _movie_subscription_with_grabbed(session, info_hash: str) -> int:
    """建一个电影订阅并把工单置为在途（GRABBED + info_hash）。"""
    service = SubscriptionService(session, MediaLibraryService(session, _fake_tmdb()))
    subscription = await service.create(MediaKind.MOVIE, 100)
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
    for row in rows:
        row.status = WantedStatus.GRABBED
        row.info_hash = info_hash
        session.add(row)
    await session.commit()
    return subscription.id


@pytest.mark.asyncio
async def test_snapshot_returns_live_progress(db, monkeypatch) -> None:
    """下载器命中：进度/速度/ETA/状态与覆盖单元原样透传。"""
    status = TorrentStatus(
        info_hash="a" * 40,
        name="Test.Movie.2024.2160p.WEB-DL",
        progress=0.42,
        completed=False,
        save_path="/downloads",
        size_bytes=4_000_000_000,
        dlspeed_bytes=12_500_000,
        eta_seconds=1080,
        state="downloading",
    )

    async def fake_usable(session):
        return [("占位", "占位配置")]

    async def fake_query(info_hash, downloaders, *, include_files=True):
        assert info_hash == "a" * 40
        # 快照走轻量查询：不拉文件清单
        assert include_files is False
        return _FakeDownloaderRow(), status

    monkeypatch.setattr(download_progress, "_usable_downloaders", fake_usable)
    monkeypatch.setattr(download_progress, "_query_torrent", fake_query)

    async with db.session() as session:
        subscription_id = await _movie_subscription_with_grabbed(session, "a" * 40)
        rows = await download_progress.subscription_download_snapshot(session, subscription_id)

    assert len(rows) == 1
    snap = rows[0]
    assert snap["state"] == "downloading"
    assert snap["progress"] == pytest.approx(0.42)
    assert snap["dlspeed_bytes"] == 12_500_000
    assert snap["eta_seconds"] == 1080
    assert snap["downloader_name"] == "测试下载器"
    assert snap["units"] == [{"season_number": 0, "episode_number": 0}]


@pytest.mark.asyncio
async def test_snapshot_marks_missing_torrent(db, monkeypatch) -> None:
    """种子在所有下载器都查不到：state=missing，进度字段留空。"""

    async def fake_usable(session):
        return [("占位", "占位配置")]

    async def fake_query(info_hash, downloaders, *, include_files=True):
        return None

    monkeypatch.setattr(download_progress, "_usable_downloaders", fake_usable)
    monkeypatch.setattr(download_progress, "_query_torrent", fake_query)

    async with db.session() as session:
        subscription_id = await _movie_subscription_with_grabbed(session, "b" * 40)
        rows = await download_progress.subscription_download_snapshot(session, subscription_id)

    assert len(rows) == 1
    assert rows[0]["state"] == "missing"
    assert rows[0]["progress"] is None
    assert rows[0]["downloader_name"] is None


@pytest.mark.asyncio
async def test_snapshot_empty_without_inflight(db) -> None:
    """没有在途工单时直接返回空列表，不触碰下载器。"""
    async with db.session() as session:
        service = SubscriptionService(session, MediaLibraryService(session, _fake_tmdb()))
        subscription = await service.create(MediaKind.MOVIE, 100)
        assert subscription.id is not None
        rows = await download_progress.subscription_download_snapshot(session, subscription.id)
    assert rows == []
