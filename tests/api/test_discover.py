"""发现页接口的端到端测试：鉴权集成、成功链路、未配置 Key 的引导错误。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.media_discover import reset_media_service
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_media import TmdbNetworkError
from movieclaw_media.models import (
    DiscoverLayout,
    DiscoverRowStub,
    MediaCard,
    MediaDetail,
    MediaFacts,
    MediaKind,
    MediaRow,
    MediaSearchItem,
    MediaSource,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    # 显式置空以覆盖代码内置的默认 Key：用例需要「未配置」状态且不许出网
    monkeypatch.setenv("TMDB_API_KEY", "")
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    reset_media_service()

    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        # 建号即自动登录，后续请求携带会话 Cookie
        c.post(
            "/api/v1/auth/bootstrap",
            json={"username": "admin", "password": "s3cret-pass"},
        )
        yield c

    reset_media_service()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def _sample_card() -> MediaCard:
    return MediaCard(
        id="42",
        type=MediaKind.MOVIE,
        title="示例电影",
        original_title="Sample",
        year=2026,
        rating=8.0,
        genres=["科幻"],
        overview="……",
        poster_url="https://image.tmdb.org/t/p/w500/x.jpg",
        backdrop_url="https://image.tmdb.org/t/p/w1280/y.jpg",
    )


class _StubService:
    def layout(self, kind: MediaKind) -> DiscoverLayout:
        return DiscoverLayout(
            has_hero=True,
            rows=[DiscoverRowStub(id="popular", title="热门电影")],
        )

    async def discover_hero(self, kind: MediaKind) -> list[MediaCard]:
        return [_sample_card()]

    async def discover_row(self, kind: MediaKind, row_id: str) -> MediaRow | None:
        if row_id != "popular":
            return None
        return MediaRow(id="popular", title="热门电影", items=[_sample_card()])

    async def search(self, keyword: str) -> list[MediaSearchItem]:
        return [
            MediaSearchItem(
                id="26266893",
                source=MediaSource.DOUBAN,
                title=keyword,
                rating=7.9,
                poster_url="https://img3.doubanio.com/a.jpg",
            )
        ]

    async def media_detail(self, douban_id: str):
        card = _sample_card()
        card.id = douban_id
        return MediaDetail(
            card=card,
            facts=MediaFacts(aliases=["示例别名"], source_url="https://m.douban.com/"),
        )


class _StubTmdbService(_StubService):
    """新 Discover 领域接口用的 TMDB 桩，签名与真实 provider 保持一致。"""

    async def search(self, keyword: str) -> list[MediaSearchItem]:
        return [
            MediaSearchItem(
                id="438631",
                source=MediaSource.TMDB,
                type=MediaKind.MOVIE,
                title=keyword,
                year=2021,
                rating=8.1,
                poster_url="https://image.tmdb.org/t/p/w342/dune.jpg",
            )
        ]

    async def media_detail(self, kind: MediaKind, tmdb_id: int) -> MediaDetail:
        card = _sample_card()
        card.id = str(tmdb_id)
        card.type = kind
        return MediaDetail(
            card=card,
            facts=MediaFacts(aliases=["示例别名"], source_url="https://www.themoviedb.org/"),
        )


class _StubDoubanService(_StubService):
    """新 Discover 领域接口用的豆瓣桩。"""

    async def full_collection(self, collection_id: str) -> MediaRow:
        return MediaRow(
            id=collection_id,
            title="完整榜单",
            items=[_sample_card()],
        )


def _patch_title_discovery_providers(monkeypatch) -> None:
    """替换领域服务懒加载的两个 provider，保证新接口测试不出网。"""
    from movieclaw_api.services import media_discover

    monkeypatch.setattr(media_discover, "get_media_service", lambda: _StubTmdbService())
    monkeypatch.setattr(media_discover, "get_douban_media_service", lambda: _StubDoubanService())


def test_search_titles_without_key_returns_guidance(client: TestClient) -> None:
    """TMDB 未配置 Key 时，统一搜索返回中文配置引导。"""
    resp = client.post(
        "/api/v1/search/titles",
        json={"query": "沙丘", "provider": "tmdb", "save_history": False},
    )
    assert resp.status_code == 502
    assert "TMDB_API_KEY" in resp.json()["message"]


def test_discovery_ui_rejects_unknown_media_type(client: TestClient) -> None:
    """展示编排只接受 movie / tv，其余按参数校验拒绝。"""
    assert client.get("/api/v1/ui/discovery/book").status_code == 422


# ---------------------------------------------------------------------------
# 新 Discover 领域契约：片单、统一搜索、稳定引用与详情
# ---------------------------------------------------------------------------


def test_list_collections_returns_stable_refs(client: TestClient, monkeypatch) -> None:
    _patch_title_discovery_providers(monkeypatch)

    resp = client.get(
        "/api/v1/discover/collections",
        params={"media_type": "movie", "provider": "tmdb"},
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["provider"] == "tmdb"
    assert [item["collection_ref"] for item in data["collections"]] == [
        "tmdb:movie:featured-weekly",
        "tmdb:movie:popular",
    ]
    assert data["collections"][0]["name"] == "本周精选"


def test_browse_collection_returns_titles_with_stable_refs(
    client: TestClient, monkeypatch
) -> None:
    _patch_title_discovery_providers(monkeypatch)

    resp = client.get(
        "/api/v1/discover/collections/tmdb:movie:popular/titles",
        params={"limit": 20},
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["collection"]["collection_ref"] == "tmdb:movie:popular"
    assert data["titles"][0]["title_ref"] == "tmdb:movie:42"
    assert data["titles"][0]["external_id"] == "42"
    assert data["returned_count"] == 1


def test_search_titles_combines_providers_and_returns_refs(
    client: TestClient, monkeypatch
) -> None:
    _patch_title_discovery_providers(monkeypatch)

    resp = client.post(
        "/api/v1/search/titles",
        json={"query": "沙丘", "provider": "all", "save_history": False},
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert {item["title_ref"] for item in data["titles"]} == {
        "tmdb:movie:438631",
        "douban:26266893",
    }
    assert data["providers"] == [
        {"provider": "douban", "success": True, "result_count": 1, "message": None},
        {"provider": "tmdb", "success": True, "result_count": 1, "message": None},
    ]


def test_search_titles_can_save_one_combined_history(client: TestClient, monkeypatch) -> None:
    _patch_title_discovery_providers(monkeypatch)

    resp = client.post(
        "/api/v1/search/titles",
        json={"query": "沙丘", "provider": "all", "save_history": True},
    )

    assert resp.status_code == 200, resp.text
    history_id = resp.json()["data"]["history_id"]
    assert isinstance(history_id, int)
    results = client.get(f"/api/v1/search/history/{history_id}/results")
    assert results.status_code == 200, results.text
    assert results.json()["data"]["vertical"] == "titles"
    assert {item["source"] for item in results.json()["data"]["items"]} == {
        "tmdb",
        "douban",
    }


def test_search_titles_preserves_successful_provider_on_partial_failure(
    client: TestClient, monkeypatch
) -> None:
    from movieclaw_api.services import media_discover

    class _UnavailableTmdb(_StubTmdbService):
        async def search(self, keyword: str) -> list[MediaSearchItem]:
            raise TmdbNetworkError("TMDB 暂时不可达")

    monkeypatch.setattr(media_discover, "get_media_service", lambda: _UnavailableTmdb())
    monkeypatch.setattr(media_discover, "get_douban_media_service", lambda: _StubDoubanService())

    resp = client.post(
        "/api/v1/search/titles",
        json={"query": "沙丘", "provider": "all"},
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert [item["title_ref"] for item in data["titles"]] == ["douban:26266893"]
    assert data["providers"][1] == {
        "provider": "tmdb",
        "success": False,
        "result_count": 0,
        "message": "TMDB 暂时不可达",
    }


def test_get_title_details_consumes_title_ref(client: TestClient, monkeypatch) -> None:
    _patch_title_discovery_providers(monkeypatch)

    resp = client.get("/api/v1/discover/titles/tmdb:movie:438631")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["title"]["title_ref"] == "tmdb:movie:438631"
    assert data["metadata"]["aliases"] == ["示例别名"]


def test_invalid_stable_refs_return_readable_400(client: TestClient, monkeypatch) -> None:
    _patch_title_discovery_providers(monkeypatch)

    collection = client.get("/api/v1/discover/collections/not-a-ref/titles")
    title = client.get("/api/v1/discover/titles/438631")

    assert collection.status_code == 400
    assert "list-collections" in collection.json()["message"]
    assert title.status_code == 400
    assert "搜索或片单接口" in title.json()["message"]


def test_discovery_ui_manifest_references_domain_collections(
    client: TestClient, monkeypatch
) -> None:
    _patch_title_discovery_providers(monkeypatch)

    resp = client.get("/api/v1/ui/discovery/movie", params={"provider": "tmdb"})

    assert resp.status_code == 200, resp.text
    sections = resp.json()["data"]["sections"]
    assert sections[0] == {
        "collection_ref": "tmdb:movie:featured-weekly",
        "title": "本周精选",
        "presentation": "hero",
        "preview_limit": 6,
        "supports_full_listing": False,
    }
