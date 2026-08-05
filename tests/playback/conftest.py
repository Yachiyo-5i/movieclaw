"""播放领域层测试基建：临时 SQLite + 最小媒体条目播种。"""

from __future__ import annotations

from pathlib import Path

import pytest

from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import MediaItem


@pytest.fixture
async def seeded_db(tmp_path: Path, monkeypatch):
    """建库并播种一部电影 + 一部剧集，返回 {movie: id, show: id}。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pb.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    async with get_database().session() as session:
        movie = MediaItem(
            kind="movie",
            tmdb_id=27205,
            imdb_id="tt1375666",
            title="盗梦空间",
            original_title="Inception",
            year=2010,
            aliases=[],
        )
        show = MediaItem(
            kind="tv",
            tmdb_id=1396,
            title="绝命毒师",
            original_title="Breaking Bad",
            year=2008,
            aliases=[],
        )
        session.add_all([movie, show])
        await session.commit()
        ids = {"movie": movie.id, "show": show.id}
    yield ids
    await dispose_db()
    get_settings.cache_clear()
