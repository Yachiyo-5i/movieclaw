"""条目转移：分错库的作品换库时，磁盘目录与台账必须一起搬到位。

覆盖用户场景「韩剧被路由进了大陆华语剧库」的完整补救路径：预览算得对、
执行后目录真的搬走了、台账改挂了新库、订阅跟着改挂、目标已有同名目录时
被拦住不覆盖。跨盘分支（复制而非改名）没法在单个 tmp_path 里造出来，
用直接调用 ``_move`` 的方式不做覆盖——它的 EXDEV 分支靠代码审阅保证。
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from movieclaw_api.api.routes.libraries import (
    preview_transfer,
    transfer_library_item,
)
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.library import TransferPayload
from movieclaw_api.services import jobs
from movieclaw_api.services.library import transfer as transfer_svc
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, JobStatus, LibraryFile, MediaItem, RuleSet, Subscription
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'transfer.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    await jobs.init_job_dispatcher(max_parallel=1)
    yield get_database()
    await jobs.close_job_dispatcher()
    await dispose_db()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_media_server_notify(monkeypatch):
    """转移收尾会通知下游媒体服务器刷新——测试里没配置，直接短路。"""

    async def _noop() -> None:
        return None

    monkeypatch.setattr(
        "movieclaw_api.services.media_server_notify.notify_media_server_refresh", _noop
    )


async def _drain_transfer(source_id: int, target_id: int) -> transfer_svc.TransferSummary:
    """等后台转移任务跑完并取回结论（最多等 5 秒，正常是毫秒级）。"""
    for _ in range(500):
        async with get_database().session() as session:
            latest = await jobs.latest_job_for_resource(
                session, "library", source_id, job_type="library.transfer"
            )
        if latest is not None and latest.status is JobStatus.SUCCEEDED:
            result = dict(latest.result or {})
            result.pop("message", None)
            return transfer_svc.TransferSummary(**result)
        await asyncio.sleep(0.01)
    raise AssertionError("转移作业没有在时限内留下结论")


def _make_series(root, name: str, episodes: int = 2):
    """在库根下造一个条目目录：Season 01 + 若干集 + NFO/海报/字幕。"""
    entry = root / name
    season = entry / "Season 01"
    season.mkdir(parents=True)
    (entry / "poster.jpg").write_bytes(b"poster")
    (entry / "tvshow.nfo").write_text("<tvshow/>", encoding="utf-8")
    paths = []
    for i in range(1, episodes + 1):
        video = season / f"{name} - S01E{i:02d}.mkv"
        video.write_bytes(b"x" * 100)
        (season / f"{name} - S01E{i:02d}.zh.srt").write_text("sub", encoding="utf-8")
        paths.append(video)
    return entry, paths


async def _setup(db, tmp_path, *, with_subscription: bool = False):
    """两个剧集库（源=大陆华语剧、目标=韩剧）+ 一部分错库的剧。"""
    source_root = tmp_path / "media" / "大陆"
    target_root = tmp_path / "media" / "韩剧"
    source_root.mkdir(parents=True)
    target_root.mkdir(parents=True)
    entry, videos = _make_series(source_root, "机智的医生生活 (2020)")

    async with db.session() as session:
        repo = LibraryRepository(session)
        source = await repo.create(name="大陆华语剧", kind="tv", root_paths=[str(source_root)])
        target = await repo.create(name="韩剧", kind="tv", root_paths=[str(target_root)])
        item = MediaItem(kind="tv", tmdb_id=96162, title="机智的医生生活", original_title="K")
        session.add(item)
        await session.flush()
        assert source.id and target.id and item.id
        for i, video in enumerate(videos, start=1):
            session.add(
                LibraryFile(
                    library_id=source.id,
                    media_item_id=item.id,
                    season_number=1,
                    episode_number=i,
                    file_path=str(video),
                    size_bytes=100,
                    source=FileSource.SCANNED,
                )
            )
        if with_subscription:
            rule_set = RuleSet(name="默认规则组", is_default=True, spec={})
            session.add(rule_set)
            await session.flush()
            assert rule_set.id
            session.add(
                Subscription(
                    media_item_id=item.id,
                    kind="tv",
                    library_id=source.id,
                    rule_set_id=rule_set.id,
                    status="active",
                )
            )
        await session.commit()
        return source.id, target.id, item.id, entry, target_root


async def test_preview_lists_entry_dir_and_size(db, tmp_path) -> None:
    """预览把「整个条目目录搬到目标库主根下」这件事讲清楚：一个目录单元、
    体积等于台账之和、目标路径在目标主根下、没有阻断问题。"""
    source_id, target_id, item_id, entry, target_root = await _setup(db, tmp_path)
    async with get_database().session() as session:
        resp = await preview_transfer(source_id, item_id, target_id, session=session)
    view = resp.data
    assert view.blocked == []
    assert len(view.moves) == 1
    move = view.moves[0]
    assert move.is_dir is True
    assert move.source_path == str(entry)
    assert move.target_path == str(target_root / entry.name)
    assert move.file_count == 2
    assert view.total_bytes == 200
    assert view.cross_device is False  # 同一个 tmp_path，必然同盘


async def test_transfer_moves_directory_and_reassigns_ledger(db, tmp_path) -> None:
    """执行后：源目录消失、目标目录含全部刮削产物、台账改挂目标库且路径随迁。"""
    source_id, target_id, item_id, entry, target_root = await _setup(db, tmp_path)
    async with get_database().session() as session:
        await transfer_library_item(
            source_id, item_id, TransferPayload(target_library_id=target_id), session=session
        )
    summary = await _drain_transfer(source_id, target_id)

    assert summary.errors == []
    assert summary.files_relocated == 2
    assert summary.bytes_moved == 200

    moved = target_root / entry.name
    assert not entry.exists(), "源条目目录应已搬走"
    assert (moved / "poster.jpg").is_file()
    assert (moved / "tvshow.nfo").is_file()
    assert (moved / "Season 01" / "机智的医生生活 (2020) - S01E01.mkv").is_file()
    assert (moved / "Season 01" / "机智的医生生活 (2020) - S01E01.zh.srt").is_file()

    async with get_database().session() as session:
        from sqlmodel import select

        rows = list((await session.execute(select(LibraryFile))).scalars().all())
    assert {r.library_id for r in rows} == {target_id}
    assert all(r.file_path.startswith(str(moved)) for r in rows)
    assert all(r.media_item_id == item_id for r in rows), "身份锚不该被转移动过"


async def test_transfer_reassigns_subscription(db, tmp_path) -> None:
    """订阅一并改挂目标库——否则下一集下载完又按旧库投递，白搬一场。"""
    source_id, target_id, item_id, _entry, _root = await _setup(
        db, tmp_path, with_subscription=True
    )
    async with get_database().session() as session:
        await transfer_library_item(
            source_id, item_id, TransferPayload(target_library_id=target_id), session=session
        )
    summary = await _drain_transfer(source_id, target_id)

    assert summary.subscription_moved is True
    async with get_database().session() as session:
        from sqlmodel import select

        sub = (await session.execute(select(Subscription))).scalar_one()
    assert sub.library_id == target_id


async def test_blocked_when_target_has_same_directory(db, tmp_path) -> None:
    """目标库里已有同名目录 → 判为阻断，绝不覆盖也绝不合并。"""
    source_id, target_id, item_id, entry, target_root = await _setup(db, tmp_path)
    (target_root / entry.name).mkdir()

    async with get_database().session() as session:
        resp = await preview_transfer(source_id, item_id, target_id, session=session)
        assert resp.data.blocked, "同名目录必须被预览标为阻断"
        assert resp.data.moves == []

        with pytest.raises(Exception) as excinfo:  # ConflictException
            await transfer_library_item(
                source_id, item_id, TransferPayload(target_library_id=target_id), session=session
            )
        assert "已存在同名目录" in excinfo.value.message  # type: ignore[attr-defined]
    assert entry.exists(), "被阻断时源目录必须原封不动"


async def test_reject_cross_kind_and_same_library(db, tmp_path) -> None:
    """跨类型（剧集→电影库）与转到自己都在校验层拒掉，不进搬运。"""
    source_id, _target_id, item_id, _entry, _root = await _setup(db, tmp_path)
    movie_root = tmp_path / "media" / "电影"
    movie_root.mkdir()
    async with get_database().session() as session:
        movie_lib = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(movie_root)]
        )
        await session.commit()
        assert movie_lib.id

        with pytest.raises(BadRequestException, match="同类型"):
            await preview_transfer(source_id, item_id, movie_lib.id, session=session)
        with pytest.raises(BadRequestException, match="无需转移"):
            await preview_transfer(source_id, item_id, source_id, session=session)


async def test_mixed_directory_falls_back_to_per_file(db, tmp_path) -> None:
    """条目目录里混着另一个条目的文件时，退化为只搬本片的文件 + 字幕，
    目录本身与别人的文件留在原地。"""
    source_id, target_id, item_id, entry, target_root = await _setup(db, tmp_path)
    intruder = entry / "Season 01" / "别人的片子.mkv"
    intruder.write_bytes(b"other")
    async with get_database().session() as session:
        other = MediaItem(kind="tv", tmdb_id=999, title="别人的片子", original_title="O")
        session.add(other)
        await session.flush()
        session.add(
            LibraryFile(
                library_id=source_id,
                media_item_id=other.id,
                season_number=1,
                episode_number=1,
                file_path=str(intruder),
                size_bytes=5,
                source=FileSource.SCANNED,
            )
        )
        await session.commit()

        resp = await preview_transfer(source_id, item_id, target_id, session=session)
    assert resp.data.blocked == []
    assert len(resp.data.moves) == 2, "两个视频文件各自成为一个搬运单元"
    assert all(not m.is_dir for m in resp.data.moves)
    assert any("其他条目" in s.reason for s in resp.data.skips)

    async with get_database().session() as session:
        await transfer_library_item(
            source_id, item_id, TransferPayload(target_library_id=target_id), session=session
        )
    summary = await _drain_transfer(source_id, target_id)
    assert summary.errors == []
    moved_season = target_root / entry.name / "Season 01"
    assert (moved_season / "机智的医生生活 (2020) - S01E01.mkv").is_file()
    assert (moved_season / "机智的医生生活 (2020) - S01E01.zh.srt").is_file(), "字幕应跟着走"
    assert intruder.is_file(), "别人的文件必须留在原地"
    assert (entry / "poster.jpg").is_file(), "混合目录不整搬，刮削产物留在原目录"
