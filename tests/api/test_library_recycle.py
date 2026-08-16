"""媒体库文件回收机制测试（docs/design/library-file-recycle.md）：
第三态状态机、做种保护、恢复/立即清理、到期清理任务、复活防线。"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.recycle import (
    purge_due_files,
    purge_file,
    recycle_file,
    restore_file,
)
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, FileState, LibraryFile, utcnow
from movieclaw_db.repositories.library_repo import LibraryRepository

_TRIGGER = {"kind": "subscription", "id": 1, "label": "《测试》订阅洗版"}


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'recycle.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed_file(db, tmp_path, *, hardlink=True, name="Movie.2020.1080p.mkv"):
    """真实 tmp 库根 + 一条在位台账行；hardlink=True 模拟硬链入库形态。"""
    root = tmp_path / "movies"
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_bytes(b"data")
    if hardlink:
        (tmp_path / "downloads").mkdir(exist_ok=True)
        os.link(path, tmp_path / "downloads" / name)
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name=f"电影库-{name}", kind="movie", root_paths=[str(root)]
        )
        row = LibraryFile(
            library_id=library.id,
            file_path=str(path),
            size_bytes=4,
            source=FileSource.IMPORTED,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id, path, root


@pytest.mark.asyncio
async def test_recycle_moves_hardlinked_file_to_trash(db, tmp_path):
    """硬链文件：移入库根回收站，file_path 更新为当前位置（全站不变量），
    原路径存 trash_original_path，默认按保留期倒计时，审计快照落行。"""
    file_id, path, root = await _seed_file(db, tmp_path)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        outcome = await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        await session.commit()
        assert outcome == "moved_to_trash"
        assert row.state == FileState.TRASHED
        assert not path.exists()
        assert row.file_path.startswith(str(root / ".movieclaw-trash"))
        assert row.trash_original_path == str(path)
        assert row.purge_after is not None
        assert row.trash_context["trigger"]["label"] == "《测试》订阅洗版"


@pytest.mark.asyncio
async def test_recycle_keeps_unique_copy_in_place(db, tmp_path):
    """做种保护：唯一硬链接的文件原地待回收——文件不动、不自动删。"""
    file_id, path, _root = await _seed_file(db, tmp_path, hardlink=False)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        outcome = await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        await session.commit()
        assert outcome == "kept_in_place"
        assert row.state == FileState.TRASHED
        assert path.exists()  # 文件原位未动，做种不受影响
        assert row.file_path == str(path)
        assert row.trash_original_path is None
        assert row.purge_after is None  # 不自动删


@pytest.mark.asyncio
async def test_restore_moves_file_back(db, tmp_path):
    """恢复：文件移回原路径，状态与附属字段全部清零。"""
    file_id, path, _root = await _seed_file(db, tmp_path)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        assert await restore_file(session, row) is True
        await session.commit()
        assert row.state == FileState.IN_PLACE
        assert row.file_path == str(path)
        assert path.exists()
        assert row.trashed_at is None and row.purge_after is None
        assert row.trash_context is None


@pytest.mark.asyncio
async def test_purge_deletes_file_and_row(db, tmp_path):
    """立即清理：删物理文件 + 删行；在位行拒绝清理。"""
    file_id, path, _root = await _seed_file(db, tmp_path)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        assert await purge_file(session, row) is False  # 在位行不许直接清
        await recycle_file(
            session, row, reason="manual", trigger={"kind": "member", "id": 1}, note="手动删除"
        )
        trash_path = row.file_path
        assert await purge_file(session, row) is True
        await session.commit()
        assert not (tmp_path / trash_path).exists()
        assert await session.get(LibraryFile, file_id) is None


@pytest.mark.asyncio
async def test_purge_due_files_expires_and_converges(db, tmp_path):
    """到期清理任务：过期的删文件+删行；未到期保留；文件消失的行收敛。"""
    expired_id, _p1, _ = await _seed_file(db, tmp_path, name="expired.mkv")
    fresh_id, _p2, _ = await _seed_file(db, tmp_path, name="fresh.mkv")
    gone_id, gone_path, _ = await _seed_file(db, tmp_path, hardlink=False, name="gone.mkv")
    async with db.session() as session:
        for fid, purge_delta in ((expired_id, -1), (fresh_id, 1)):
            row = await session.get(LibraryFile, fid)
            await recycle_file(
                session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
            )
            row.purge_after = utcnow() + timedelta(hours=purge_delta)
        gone = await session.get(LibraryFile, gone_id)
        await recycle_file(
            session, gone, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        await session.commit()
    gone_path.unlink()  # 待回收期间文件被外部删除

    await purge_due_files()
    async with db.session() as session:
        assert await session.get(LibraryFile, expired_id) is None  # 到期清理
        assert await session.get(LibraryFile, gone_id) is None  # 消失收敛
        fresh = await session.get(LibraryFile, fresh_id)
        assert fresh is not None and fresh.state == FileState.TRASHED  # 未到期保留


@pytest.mark.asyncio
async def test_state_guards_block_resurrection(db, tmp_path):
    """复活防线：缺失检测不覆盖待回收状态，revive 也不复活待回收行。"""
    file_id, _path, _root = await _seed_file(db, tmp_path, hardlink=False)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        await recycle_file(
            session, row, reason="upgrade_refuted", trigger=_TRIGGER, note="证伪隔离"
        )
        row.mark_missing()  # 对账误触：不得覆盖待回收
        assert row.state == FileState.TRASHED and row.missing_since is None
        row.revive()  # 扫描按路径命中：不得复活
        assert row.state == FileState.TRASHED
        await session.commit()


@pytest.mark.asyncio
async def test_in_place_scope_excludes_trashed(db, tmp_path):
    """共享口径：待回收行不进默认库存口径。"""
    file_id, _path, _root = await _seed_file(db, tmp_path)
    async with db.session() as session:
        row = await session.get(LibraryFile, file_id)
        await recycle_file(
            session, row, reason="upgrade_replaced", trigger=_TRIGGER, note="洗版替换"
        )
        await session.commit()
        in_place = list(
            (await session.execute(select(LibraryFile).where(LibraryFile.in_place()))).scalars()
        )
        assert in_place == []
