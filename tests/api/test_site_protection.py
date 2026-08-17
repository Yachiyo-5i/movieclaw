"""站点保护开关的两道闸测试（设计见 docs/design/site-protection-ratio-boost.md 1.2）。

- 第一道闸：搜索扇出——``search_all_sites(exclude_protected=True)`` 不触达
  受保护站点；不传该参数（手动搜索）时保护站点照常参与。
- 第二道闸：``drop_protected_sites``——被动匹配进管道前剔除受保护站点的
  种子行（种子同步照常索引受保护站点，全靠这道闸拦截）。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio

import movieclaw_api.services.site_search as site_search
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.site_search import search_all_sites
from movieclaw_api.services.subscription.matching import drop_protected_sites
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import AuthType, SiteCredential, SiteTorrent, TorrentSource
from movieclaw_tracker.models import SearchQuery, SearchResult, TorrentListItem


@dataclass
class _Cred:
    """_active_sites 返回值的最小替身——保护过滤只再多读一个 protected。"""

    site_id: str
    protected: bool = False


class _FakeSite:
    """假站点：记录是否被搜索过，返回一条固定结果。"""

    def __init__(self) -> None:
        self.searched = False

    async def search(self, query: SearchQuery) -> SearchResult:
        self.searched = True
        return SearchResult(
            items=[TorrentListItem(torrent_id="1", title="Hit", seeders=1)],
            page=query.page,
            total_pages=1,
        )


class _FakeManager:
    def __init__(self, sites: dict[str, _FakeSite]):
        self._sites = sites

    async def get(self, site_id: str) -> _FakeSite:
        return self._sites[site_id]


async def _async(value):
    return value


def _wire(monkeypatch, creds: list[_Cred], sites: dict[str, _FakeSite]) -> None:
    monkeypatch.setattr(site_search, "_active_sites", lambda: _async(creds))
    monkeypatch.setattr(site_search, "get_site_access", lambda: _FakeManager(sites))


# ---------------------------------------------------------------------------
# 第一道闸：搜索扇出
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_search_skips_protected_sites(monkeypatch) -> None:
    open_site, guarded_site = _FakeSite(), _FakeSite()
    _wire(
        monkeypatch,
        [_Cred("open"), _Cred("guarded", protected=True)],
        {"open": open_site, "guarded": guarded_site},
    )
    response = await search_all_sites("keyword", exclude_protected=True)
    assert open_site.searched
    assert not guarded_site.searched  # 受保护站点连请求都不发
    assert {s.site_id for s in response.sites} == {"open"}


@pytest.mark.asyncio
async def test_manual_search_still_reaches_protected_sites(monkeypatch) -> None:
    """保护只挡订阅链路；用户主动搜索（默认参数）照常触达。"""
    guarded_site = _FakeSite()
    _wire(monkeypatch, [_Cred("guarded", protected=True)], {"guarded": guarded_site})
    response = await search_all_sites("keyword")
    assert guarded_site.searched
    assert response.total == 1


# ---------------------------------------------------------------------------
# 第二道闸：评估投递前剔除受保护站点的种子行
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _torrent(site_id: str, torrent_id: str) -> SiteTorrent:
    return SiteTorrent(
        site_id=site_id, torrent_id=torrent_id, title="X", source=TorrentSource.LIST
    )


@pytest.mark.asyncio
async def test_drop_protected_sites_filters_rows(db) -> None:
    async with db.session() as session:
        session.add(
            SiteCredential(site_id="guarded", auth_type=AuthType.COOKIE, protected=True)
        )
        session.add(SiteCredential(site_id="open", auth_type=AuthType.COOKIE))
        await session.commit()

        rows = [_torrent("guarded", "1"), _torrent("open", "2"), _torrent("guarded", "3")]
        kept = await drop_protected_sites(session, rows)
        assert [(t.site_id, t.torrent_id) for t in kept] == [("open", "2")]


@pytest.mark.asyncio
async def test_drop_protected_sites_noop_without_protection(db) -> None:
    """没有任何站点开保护时原样返回（被动匹配主路径零额外开销语义）。"""
    async with db.session() as session:
        session.add(SiteCredential(site_id="open", auth_type=AuthType.COOKIE))
        await session.commit()
        rows = [_torrent("open", "1")]
        assert await drop_protected_sites(session, rows) == rows
