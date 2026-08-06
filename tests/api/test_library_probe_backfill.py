"""扫描的介质规格补探阶段。

这一段存在的唯一理由：ffprobe 是**后装**的时候，扫描的主循环对已识别且
在位的行整体秒过，永远走不到探测那一步——「装好 ffmpeg 再重新扫描」这个
最直觉的动作必须真的有效。ffprobe 与 TMDB 均为假实现。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.items as items_mod
import movieclaw_api.services.library.scan as scan_mod
import movieclaw_api.services.media_discover as discover_mod
import movieclaw_api.services.media_probe as probe_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.library.scan import ScanSummary, scan_library
from movieclaw_api.services.media_probe import MediaSpec
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import LibraryFile
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"

_SPEC = MediaSpec(
    resolution="1080p",
    video_codec="hevc",
    hdr=None,
    bit_depth=10,
    duration_seconds=5400,
    bit_rate=8_000_000,
    audio_streams=[{"codec": "eac3", "channels": 6, "language": "chi", "default": True}],
    subtitle_streams=[{"codec": "subrip", "language": "chi"}],
)


def _body(tmdb_id: int) -> dict:
    return {
        "id": tmdb_id,
        "title": f"影片{tmdb_id}",
        "original_title": f"M{tmdb_id}",
        "release_date": "2020-01-01",
        "status": "Released",
        "external_ids": {},
        "alternative_titles": {"titles": []},
        "translations": {"translations": []},
        "overview": "简介。",
        "vote_average": 7.0,
        "runtime": 100,
        "genres": [],
        "images": {"posters": [], "backdrops": []},
        "release_dates": {"results": []},
        "credits": {"cast": [], "crew": []},
    }


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()

    def handler(request: httpx.Request) -> httpx.Response:
        seg = request.url.path.rstrip("/").split("/")[-1]
        if seg.isdigit():
            return httpx.Response(200, json=_body(int(seg)))
        return httpx.Response(200, json={"results": []})

    client = TmdbClient(api_key=_KEY, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(discover_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "get_tmdb_client", lambda: client)
    monkeypatch.setattr(scan_mod, "NEW_FILE_QUIET_SECONDS", 0)
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _no_ffprobe(monkeypatch) -> None:
    """模拟"系统里没装 ffmpeg"：探测一律失败，可用性检查为假。"""
    monkeypatch.setattr(probe_mod, "ffprobe_available", lambda: False)
    monkeypatch.setattr(scan_mod, "probe_media", lambda *_a, **_k: None)
    monkeypatch.setattr(items_mod, "probe_media", lambda *_a, **_k: None)


def _with_ffprobe(monkeypatch, calls: list | None = None) -> None:
    """模拟"ffmpeg 已装"：探测返回固定规格。"""
    def _probe(path, *_a, **_k):
        if calls is not None:
            calls.append(str(path))
        return _SPEC

    monkeypatch.setattr(probe_mod, "ffprobe_available", lambda: True)
    monkeypatch.setattr(scan_mod, "probe_media", _probe)
    monkeypatch.setattr(items_mod, "probe_media", _probe)


async def _build(db, tmp_path, count: int = 3) -> int:
    root = tmp_path / "media" / "movies"
    for i in range(count):
        entry = root / f"影片{3000 + i} (2020)"
        entry.mkdir(parents=True)
        (entry / f"影片{3000 + i}.1080p.mkv").write_bytes(b"z" * (200 + i))
        (entry / "movie.nfo").write_text(
            f"<movie><tmdbid>{3000 + i}</tmdbid></movie>", encoding="utf-8"
        )
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=[str(root)]
        )
    return library.id


async def _rows(db) -> list[LibraryFile]:
    async with db.session() as session:
        return list((await session.execute(select(LibraryFile))).scalars().all())


async def test_rescan_backfills_specs_after_ffmpeg_is_installed(db, tmp_path, monkeypatch) -> None:
    """先在没有 ffprobe 的环境入库，装上之后重扫必须把规格补齐。

    这是本模块的正题：主循环对"已识别且在位"的行整体秒过，补探阶段是
    这些行唯一的救赎路径。
    """
    _no_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path)
    await scan_library(library_id)

    rows = await _rows(db)
    assert len(rows) == 3
    assert all(r.audio_streams is None and r.resolution is None for r in rows)

    # 装上 ffmpeg，重新扫描
    _with_ffprobe(monkeypatch)
    summary = await scan_library(library_id)

    assert summary.skipped_known == 3, "主循环仍然应当秒过这些行——补探是独立阶段"
    assert summary.probed == 3
    rows = await _rows(db)
    assert all(r.resolution == "1080p" and r.video_codec == "hevc" for r in rows)
    assert all(r.audio_streams and r.audio_streams[0]["codec"] == "eac3" for r in rows)
    assert all(r.subtitle_streams for r in rows)


async def test_backfill_is_idempotent(db, tmp_path, monkeypatch) -> None:
    """补齐之后再扫不再重复探测——探过的行 audio_streams 非 NULL，直接出局。"""
    _no_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path)
    await scan_library(library_id)

    calls: list[str] = []
    _with_ffprobe(monkeypatch, calls)
    await scan_library(library_id)
    assert len(calls) == 3
    calls.clear()

    summary = await scan_library(library_id)
    assert summary.probed == 0
    assert calls == []


async def test_background_scan_skips_historical_spec_backfill(db, tmp_path, monkeypatch) -> None:
    """后台对账和目录事件只盘点台账，不能对历史文件逐个启动 ffprobe。"""
    _no_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path)
    await scan_library(library_id)

    calls: list[str] = []
    _with_ffprobe(monkeypatch, calls)
    summary = await scan_library(library_id, backfill_existing_specs=False)

    assert summary.probed == 0
    assert calls == []
    rows = await _rows(db)
    assert all(row.audio_streams is None for row in rows)


async def test_periodic_reconcile_skips_historical_spec_backfill(db, tmp_path, monkeypatch) -> None:
    """定时对账传入关闭开关，避免后台周期扫到全库历史规格。"""
    library_id = await _build(db, tmp_path, count=1)
    calls: list[tuple[int, bool]] = []

    async def fake_scan(
        current_library_id: int, *, backfill_existing_specs: bool = True
    ) -> ScanSummary:
        calls.append((current_library_id, backfill_existing_specs))
        return ScanSummary(library_id=current_library_id)

    monkeypatch.setattr(scan_mod, "scan_library", fake_scan)
    await scan_mod.reconcile_libraries()

    assert calls == [(library_id, False)]


async def test_backfill_skipped_when_ffprobe_missing(db, tmp_path, monkeypatch) -> None:
    """没装 ffprobe 时整段跳过：每轮扫描白跑一遍必然失败的探测毫无意义。"""
    calls: list[str] = []

    def _probe(path, *_a, **_k):
        calls.append(str(path))
        return None

    monkeypatch.setattr(probe_mod, "ffprobe_available", lambda: False)
    monkeypatch.setattr(scan_mod, "probe_media", _probe)
    monkeypatch.setattr(items_mod, "probe_media", _probe)

    library_id = await _build(db, tmp_path)
    await scan_library(library_id)
    calls.clear()  # 首轮入账时每个新文件探一次是正常的

    summary = await scan_library(library_id)
    assert summary.probed == 0
    assert calls == [], "可用性检查为假时不该再调 ffprobe"


async def test_backfill_skips_strm_rows(db, tmp_path, monkeypatch) -> None:
    """strm 占位行不参与补探：本体没有媒体流，探了必失败，只会污染进度分母。"""
    _no_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path, count=1)
    entry = tmp_path / "media" / "movies" / "影片4000 (2020)"
    entry.mkdir(parents=True)
    (entry / "影片4000.strm").write_text("https://cloud.example.com/m.mkv", encoding="utf-8")
    (entry / "movie.nfo").write_text("<movie><tmdbid>4000</tmdbid></movie>", encoding="utf-8")
    await scan_library(library_id)

    calls: list[str] = []
    _with_ffprobe(monkeypatch, calls)
    summary = await scan_library(library_id)

    assert summary.probed == 1, "只有真视频那行需要补探"
    assert all(not c.endswith(".strm") for c in calls)
    rows = {Path(r.file_path).suffix: r for r in await _rows(db)}
    assert rows[".strm"].audio_streams is None and rows[".strm"].resolution is None
    assert rows[".mkv"].audio_streams is not None


async def test_backfill_leaves_missing_and_ignored_rows_alone(db, tmp_path, monkeypatch) -> None:
    """已标记 missing 与用户忽略过的行不参与补探。

    前者文件根本不在磁盘上（探了也白探）；后者是用户明确处理完的决定，
    不该因为一次扫描又被摸一遍。
    """
    from movieclaw_db.models import utcnow

    _no_ffprobe(monkeypatch)
    library_id = await _build(db, tmp_path)
    await scan_library(library_id)

    rows = await _rows(db)
    async with db.session() as session:
        missing = await session.get(LibraryFile, rows[0].id)
        ignored = await session.get(LibraryFile, rows[1].id)
        # 文件真的从磁盘删掉，否则重扫会把它当"文件回归"重新入账（那条路径
        # 本来就会探测，测不出补探阶段的取舍）
        Path(missing.file_path).unlink()
        missing.missing_since = utcnow()
        ignored.ignored_at = utcnow()
        await session.commit()

    calls: list[str] = []
    _with_ffprobe(monkeypatch, calls)
    summary = await scan_library(library_id)

    assert summary.probed == 1  # 只剩第三行
    assert len(calls) == 1
    after = {r.id: r for r in await _rows(db)}
    assert after[rows[0].id].audio_streams is None
    assert after[rows[1].id].audio_streams is None
    assert after[rows[2].id].audio_streams is not None
