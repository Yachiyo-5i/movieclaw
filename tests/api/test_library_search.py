"""媒体库搜索接口（GET /search/library-items）的端到端测试。

覆盖：标题/原名子串匹配（忽略英文大小写）、按库分组与组内拼音排序、
未识别文件不参与搜索、无命中回空列表。搜索页「媒体库」垂直的数据源。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import LibraryFile, MediaItem
from movieclaw_db.repositories import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'search.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(db):
    """走 ASGITransport 直连应用（不跑 lifespan——db fixture 已建好库）。

    与 db fixture 共用同一个事件循环，测试里才能直接用 db.session()
    造数据、再发 HTTP 请求验证接口。
    """
    from movieclaw_api.api.deps import require_login
    from movieclaw_api.app import create_app
    from movieclaw_api.services.auth import Principal

    app = create_app()
    app.dependency_overrides[require_login] = lambda: Principal(kind="admin", name="tester")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as async_client:
        yield async_client


async def _seed(db) -> None:
    """两个库各挂几部片：电影库（沙丘/奥本海默）+ 剧集库（沙丘：预言）。"""
    async with db.session() as session:
        movie_lib = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/media/movies"]
        )
        tv_lib = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/media/tv"]
        )
        dune = MediaItem(
            kind="movie", tmdb_id=1, title="沙丘", original_title="Dune", year=2021, aliases=[]
        )
        oppenheimer = MediaItem(
            kind="movie", tmdb_id=2, title="奥本海默", original_title="Oppenheimer", aliases=[]
        )
        prophecy = MediaItem(
            kind="tv", tmdb_id=3, title="沙丘：预言", original_title="Dune: Prophecy", aliases=[]
        )
        session.add_all([dune, oppenheimer, prophecy])
        await session.flush()
        session.add_all(
            [
                LibraryFile(
                    library_id=movie_lib.id,
                    media_item_id=dune.id,
                    file_path="/media/movies/沙丘/Dune.2021.mkv",
                    size_bytes=100,
                    source="scanned",
                ),
                LibraryFile(
                    library_id=movie_lib.id,
                    media_item_id=oppenheimer.id,
                    file_path="/media/movies/奥本海默/Oppenheimer.mkv",
                    size_bytes=200,
                    source="scanned",
                ),
                LibraryFile(
                    library_id=tv_lib.id,
                    media_item_id=prophecy.id,
                    season_number=1,
                    episode_number=1,
                    file_path="/media/tv/沙丘：预言/S01E01.mkv",
                    size_bytes=300,
                    source="scanned",
                ),
                # 未识别文件：没有可靠标题，不该出现在搜索结果里
                LibraryFile(
                    library_id=movie_lib.id,
                    media_item_id=None,
                    file_path="/media/movies/未识别/dune.leak.mkv",
                    size_bytes=1,
                    source="scanned",
                ),
            ]
        )
        await session.commit()


async def _search(client: AsyncClient, keyword: str) -> list[dict]:
    resp = await client.get("/api/v1/search/library-items", params={"keyword": keyword})
    assert resp.status_code == 200
    return resp.json()["data"]


@pytest.mark.asyncio
async def test_search_groups_by_library(db, client) -> None:
    await _seed(db)
    groups = await _search(client, "沙丘")
    assert [g["library_name"] for g in groups] == ["电影库", "剧集库"]
    assert [i["title"] for i in groups[0]["items"]] == ["沙丘"]
    assert [i["title"] for i in groups[1]["items"]] == ["沙丘：预言"]
    # 组自带库信息，前端不必再对库列表
    assert groups[0]["kind"] == "movie"
    assert groups[0]["items"][0]["file_count"] == 1
    assert groups[0]["items"][0]["total_size_bytes"] == 100


@pytest.mark.asyncio
async def test_search_matches_original_title_case_insensitive(db, client) -> None:
    await _seed(db)
    groups = await _search(client, "DUNE")
    titles = [i["title"] for g in groups for i in g["items"]]
    # 原名 Dune / Dune: Prophecy 都命中；未识别文件（文件名含 dune）不出现
    assert titles == ["沙丘", "沙丘：预言"]


@pytest.mark.asyncio
async def test_search_no_match_returns_empty(db, client) -> None:
    await _seed(db)
    assert await _search(client, "星际穿越") == []
