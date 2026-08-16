"""Jellyfin 兼容层测试基建：预播种媒体库 + 完整 HTTP 栈。

模式：先用独立事件循环把库/条目/文件行播种进临时 SQLite，再启动应用
（lifespan 里的迁移对已迁移库是 no-op），全部断言走 HTTP——协议形态
本身就是被测对象。
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jellyfin.helpers import ADMIN
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    FileSource,
    Library,
    LibraryFile,
    MediaEpisode,
    MediaItem,
    MediaItemPerson,
    MediaMetadata,
    MediaSeason,
    Person,
)
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    root.mkdir()
    return root


@pytest.fixture
def seeded(tmp_path: Path, media_root: Path, monkeypatch) -> dict:
    """播种：电影库（本地文件 + strm 版本）+ 剧集库（1 剧 2 季 3 集）。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'jf.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("METADATA_DIR", str(tmp_path / "metadata"))
    get_settings.cache_clear()

    movie_file = media_root / "Inception (2010)" / "Inception.2010.2160p.mkv"
    movie_file.parent.mkdir(parents=True)
    movie_file.write_bytes(b"A" * 4096)
    strm_file = media_root / "Inception (2010)" / "Inception.2010.1080p.strm"
    strm_file.write_text("https://pan.example.com/inception-1080p.mp4\n")

    ep_files = {}
    for s, e in [(1, 1), (1, 2), (2, 1)]:
        p = media_root / "Breaking (2008)" / f"Season {s:02d}" / f"S{s:02d}E{e:02d}.mkv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"B" * 2048)
        ep_files[(s, e)] = p

    assets = tmp_path / "metadata" / "images" / "1"
    assets.mkdir(parents=True)
    # 真实可解码的 JPEG：封面拼贴（Pillow）要打开它合成
    from PIL import Image

    Image.new("RGB", (100, 150), "#4a6fa5").save(assets / "poster.jpg", "JPEG")

    ids: dict = {}

    async def _seed() -> None:
        init_db(get_settings().database_url, echo=False)
        await run_migrations()
        async with get_database().session() as session:
            movie_lib = Library(name="电影", kind="movie", root_paths=[str(media_root)])
            tv_lib = Library(name="剧集", kind="tv", root_paths=[str(media_root)])
            session.add_all([movie_lib, tv_lib])
            await session.flush()

            movie = MediaItem(
                kind="movie",
                tmdb_id=27205,
                imdb_id="tt1375666",
                title="盗梦空间",
                original_title="Inception",
                year=2010,
                aliases=["Inception", "盗梦空间"],
            )
            show = MediaItem(
                kind="tv",
                tmdb_id=1396,
                title="绝命毒师",
                original_title="Breaking Bad",
                year=2008,
                aliases=["Breaking Bad"],
                status="Ended",
            )
            session.add_all([movie, show])
            await session.flush()

            session.add_all(
                [
                    MediaMetadata(
                        media_item_id=movie.id,
                        overview="植入梦境的盗梦者。",
                        genres=["科幻", "动作"],
                        runtime_minutes=148,
                        release_date=date(2010, 7, 15),
                        content_rating="PG-13",
                        vote_average=8.4,
                        poster_file="1/poster.jpg",
                    ),
                    MediaMetadata(
                        media_item_id=show.id,
                        overview="化学老师制毒记。",
                        genres=["剧情", "犯罪"],
                        runtime_minutes=47,
                        release_date=date(2008, 1, 20),
                        vote_average=9.4,
                    ),
                    MediaSeason(media_item_id=show.id, season_number=1, name="第 1 季"),
                    MediaSeason(media_item_id=show.id, season_number=2, name="第 2 季"),
                ]
            )
            for (s, e), title in [
                ((1, 1), "Pilot"),
                ((1, 2), "Cat's in the Bag..."),
                ((2, 1), "Seven Thirty-Seven"),
            ]:
                session.add(
                    MediaEpisode(
                        media_item_id=show.id,
                        season_number=s,
                        episode_number=e,
                        name=title,
                        air_date=date(2008, 1, 20),
                        runtime_minutes=47,
                    )
                )

            session.add_all(
                [
                    LibraryFile(
                        library_id=movie_lib.id,
                        media_item_id=movie.id,
                        file_path=str(movie_file),
                        size_bytes=4096,
                        container="mkv",
                        resolution="2160p",
                        video_codec="hevc",
                        hdr="HDR10",
                        bit_depth=10,
                        duration_seconds=148 * 60,
                        bit_rate=25_000_000,
                        audio_streams=[
                            {
                                "codec": "aac",
                                "profile": None,
                                "channels": 6,
                                "channel_layout": "5.1",
                                "language": "chi",
                                "title": None,
                                "default": True,
                            }
                        ],
                        subtitle_streams=[
                            {
                                "codec": "subrip",
                                "language": "chi",
                                "title": None,
                                "forced": False,
                                "default": False,
                            }
                        ],
                        source=FileSource.SCANNED,
                    ),
                    LibraryFile(
                        library_id=movie_lib.id,
                        media_item_id=movie.id,
                        file_path=str(strm_file),
                        size_bytes=0,
                        source=FileSource.SCANNED,
                    ),
                ]
            )
            for (s, e), p in ep_files.items():
                session.add(
                    LibraryFile(
                        library_id=tv_lib.id,
                        media_item_id=show.id,
                        season_number=s,
                        episode_number=e,
                        file_path=str(p),
                        size_bytes=2048,
                        container="mkv",
                        resolution="1080p",
                        video_codec="h264",
                        duration_seconds=47 * 60,
                        source=FileSource.SCANNED,
                    )
                )
            # 演职员：2 演员 + 1 导演挂电影（People 输出与 personIds 过滤用）
            dicaprio = Person(
                tmdb_person_id=6193,
                name="莱昂纳多·迪卡普里奥",
                original_name="Leonardo DiCaprio",
                profile_path="/leo.jpg",
            )
            page = Person(tmdb_person_id=27578, name="艾伦·佩吉")
            nolan = Person(
                tmdb_person_id=525,
                name="克里斯托弗·诺兰",
                original_name="Christopher Nolan",
                profile_path="/nolan.jpg",
            )
            session.add_all([dicaprio, page, nolan])
            await session.flush()
            session.add_all(
                [
                    MediaItemPerson(
                        media_item_id=movie.id,
                        person_id=dicaprio.id,
                        department="cast",
                        character="Cobb",
                        credit_order=0,
                    ),
                    MediaItemPerson(
                        media_item_id=movie.id,
                        person_id=page.id,
                        department="cast",
                        character="Ariadne",
                        credit_order=1,
                    ),
                    MediaItemPerson(
                        media_item_id=movie.id,
                        person_id=nolan.id,
                        department="director",
                        credit_order=0,
                    ),
                ]
            )
            await session.commit()
            await LibraryRepository(session).refresh_stats([movie_lib.id, tv_lib.id])
            ids.update(
                {
                    "movie_lib": movie_lib.id,
                    "tv_lib": tv_lib.id,
                    "movie": movie.id,
                    "dicaprio": dicaprio.id,
                    "nolan": nolan.id,
                    "show": show.id,
                }
            )
        await dispose_db()

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    asyncio.run(_seed())
    return ids


@pytest.fixture
def client(seeded, monkeypatch):
    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        # 建管理员账号（引导接口），供 AuthenticateByName 校验
        resp = c.post("/api/v1/auth/bootstrap", json=ADMIN)
        assert resp.status_code == 200, resp.text
        yield c
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()
