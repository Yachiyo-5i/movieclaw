"""下载监听导入的测试。

覆盖：完成检测（下载器权威信号优先、进行中标记阻断并重置计时、指纹
静默窗口、逐文件探测门禁）、硬链接/复制两种搬运策略、电影/剧集的规范
落位、台账幂等（同指纹不重复处理、指纹变化自动重试、季包增量补集）、
识别失败的失败记录、配置校验（监听目录与根路径重叠拒绝）。识别与季集
解析依赖 NER 模型与 TMDB，此处打桩——识别链本体由扫描器测试覆盖。
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.ingest as ingest_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services import jobs
from movieclaw_api.services.import_watch_config import ImportWatchConfigService
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    ImportWatch,
    IngestEntry,
    IngestStatus,
    Job,
    JobStatus,
    Library,
    LibraryFile,
    ManualDownloadIntent,
    MediaItem,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind

_FAKE_SPEC = SimpleNamespace(
    resolution="1080p",
    video_codec="hevc",
    hdr=None,
    bit_depth=10,
    duration_seconds=3600,
    bit_rate=None,
    audio_streams=[],
    subtitle_streams=[],
)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'ingest.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    # 每个测试独立的静默观察表/挂起表 + 立即落定的静默窗口（两次巡检即可
    # 导入：第一轮记录指纹，第二轮确认稳定）；下载器概览缓存清空（默认无
    # 下载器 → 权威信号缺席 → 走启发式路径）
    monkeypatch.setattr(ingest_mod, "_stability", {})
    monkeypatch.setattr(ingest_mod, "_deferred", {})
    monkeypatch.setattr(ingest_mod, "_failed_retry", {})
    monkeypatch.setattr(ingest_mod, "QUIET_SECONDS", 0)
    monkeypatch.setattr(ingest_mod, "_briefs_cache", (float("-inf"), None))
    yield get_database()
    await jobs.close_job_dispatcher()
    await dispose_db()
    get_settings.cache_clear()


async def _make_library(db, *, kind: MediaKind, root) -> int:
    root.mkdir(parents=True, exist_ok=True)
    async with db.session() as session:
        row = await LibraryRepository(session).create(
            name=f"测试{kind.value}库", kind=kind.value, root_paths=[str(root)]
        )
        return row.id


async def _make_rule(db, *, library_id: int, source, strategy="hardlink") -> None:
    async with db.session() as session:
        session.add(ImportWatch(source_path=str(source), strategy=strategy, library_id=library_id))
        await session.commit()


async def _make_item(db, *, kind: MediaKind, title: str, year: int) -> MediaItem:
    async with db.session() as session:
        item = MediaItem(kind=kind.value, tmdb_id=300, title=title, original_title=title, year=year)
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item


async def _get_library(db, library_id: int) -> Library:
    async with db.session() as session:
        row = await session.get(Library, library_id)
        assert row is not None
        return row


async def _wait_job_status(job_id: str, status: JobStatus, timeout: float = 3.0) -> Job:
    """等待统一调度器收口状态，避免测试依赖固定 sleep。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        async with get_database().session() as session:
            row = await session.get(Job, job_id)
            assert row is not None
            if row.status is status:
                return row
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"作业未进入 {status.value}，当前为 {row.status.value}")
        await asyncio.sleep(0.01)


def _stub_identify(monkeypatch, item):
    async def identify(session, kind, watch_root, main, spec):
        return item

    monkeypatch.setattr(ingest_mod, "_identify", identify)


def _fixed_rule(watch, strategy="hardlink", library_id=None) -> ImportWatch:
    """指定库规则的瞬时对象（_sweep_dir 只读 source_path/strategy/kind）。"""
    return ImportWatch(source_path=str(watch), strategy=strategy, library_id=library_id)


async def _sweep_twice(db, library_id, watch, strategy="hardlink"):
    """两轮巡检：第一轮记录指纹，第二轮确认静默后处理。"""
    for _ in range(2):
        library = await _get_library(db, library_id)
        await ingest_mod._sweep_dir(
            _fixed_rule(watch, strategy, library_id), library, execute_inline=True
        )


@pytest.mark.asyncio
async def test_marker_blocks_and_resets_quiet_window(db, tmp_path, monkeypatch):
    """有下载中标记：任凭巡检多少轮都不入库；标记消失后重新静默再导入。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    entry = watch / "某电影 (2020)"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")
    marker = entry / "movie.mkv.aria2"
    marker.write_bytes(b"ctl")

    await _sweep_twice(db, library_id, watch)
    await _sweep_twice(db, library_id, watch)
    assert not (root / "某电影 (2020)").exists()
    # 标记在场必须留痕：CIFS/NFS 等无事件挂载上标记消失不会有事件，
    # 兜底巡检全靠 _has_pending 看见它才不跳过该目录（回归）
    assert ingest_mod._has_pending(str(watch))

    marker.unlink()
    await _sweep_twice(db, library_id, watch)
    assert (root / "某电影 (2020)" / "某电影 (2020).mkv").read_bytes() == b"video"


@pytest.mark.asyncio
async def test_unstable_fingerprint_defers_import(db, tmp_path, monkeypatch):
    """指纹还在变化（写入中）：不导入；稳定后下一轮才导入。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    entry = watch / "某电影 (2020)"
    entry.mkdir()
    video = entry / "movie.mkv"
    video.write_bytes(b"part1")

    library = await _get_library(db, library_id)
    rule = _fixed_rule(watch, library_id=library_id)
    await ingest_mod._sweep_dir(rule, library, execute_inline=True)  # 记录指纹 A
    video.write_bytes(b"part1-part2")  # 下载继续，指纹变为 B
    await ingest_mod._sweep_dir(rule, library, execute_inline=True)  # B 首见，重新起算
    assert not (root / "某电影 (2020)").exists()
    await ingest_mod._sweep_dir(rule, library, execute_inline=True)  # B 稳定 → 导入
    assert (root / "某电影 (2020)" / "某电影 (2020).mkv").read_bytes() == b"part1-part2"


@pytest.mark.asyncio
async def test_movie_hardlink_import_and_ledger(db, tmp_path, monkeypatch):
    """电影硬链接入库：同 inode 零占用、台账落账、处理台账 imported、源文件不动。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    entry = watch / "Some.Movie.2020.1080p"
    entry.mkdir()
    src = entry / "some.movie.mkv"
    src.write_bytes(b"video")

    await _sweep_twice(db, library_id, watch)

    target = root / "某电影 (2020)" / "某电影 (2020).mkv"
    assert target.stat().st_ino == src.stat().st_ino  # 硬链接：同一 inode
    assert src.read_bytes() == b"video"  # 源文件原地保留（保种）
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        records = list((await session.execute(select(IngestEntry))).scalars().all())
    assert [f.file_path for f in files] == [str(target)]
    assert files[0].resolution == "1080p"
    assert [r.status for r in records] == [IngestStatus.IMPORTED]
    assert records[0].imported_count == 1

    # 幂等：指纹未变，再巡检不重复处理
    await _sweep_twice(db, library_id, watch)
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(files) == 1


@pytest.mark.asyncio
async def test_copy_strategy(db, tmp_path, monkeypatch):
    """复制策略：目标是独立文件（不同 inode），内容一致。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    src = watch / "某电影 (2020).mkv"  # 裸文件条目
    src.write_bytes(b"video")

    await _sweep_twice(db, library_id, watch, strategy="copy")

    target = root / "某电影 (2020)" / "某电影 (2020).mkv"
    assert target.read_bytes() == b"video"
    assert target.stat().st_ino != src.stat().st_ino
    assert not target.with_name(target.name + ".part").exists()  # 临时文件已清


@pytest.mark.asyncio
async def test_probe_gate_blocks_partial_file(db, tmp_path, monkeypatch):
    """探测门禁：ffprobe 可用但主视频探测失败 → 记 failed，不搬运。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: None)
    monkeypatch.setattr(ingest_mod, "ffprobe_available", lambda: True)

    entry = watch / "某电影 (2020)"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"partial")

    await _sweep_twice(db, library_id, watch)

    assert not (root / "某电影 (2020)").exists()
    async with db.session() as session:
        records = list((await session.execute(select(IngestEntry))).scalars().all())
    assert [r.status for r in records] == [IngestStatus.FAILED]
    assert "探测失败" in (records[0].message or "")


@pytest.mark.asyncio
async def test_identify_failure_goes_pending_without_retry(db, tmp_path, monkeypatch):
    """识别不出记 pending（信息不足，等人拍板）：指纹不变**永不**定时重试
    （不打 TMDB），指纹变化（用户改名/补文件）才重新处理。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    calls = {"n": 0}

    async def identify_none(session, kind, watch_root, main, spec):
        calls["n"] += 1
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)

    entry = watch / "unknown-release"
    entry.mkdir()
    video = entry / "video.mkv"
    video.write_bytes(b"x")

    await _sweep_twice(db, library_id, watch)
    assert calls["n"] == 1
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.PENDING
    assert "认领" in (record.message or "")

    # 指纹不变：待处理不定时重试（等的是人，不是时间）
    await _sweep_twice(db, library_id, watch)
    assert calls["n"] == 1

    # 指纹变化（如用户改名/补文件）：重新处理
    video.write_bytes(b"xy")
    await _sweep_twice(db, library_id, watch)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_tmdb_outage_goes_failed_not_pending(db, tmp_path, monkeypatch):
    """TMDB 不可达是环境故障：记 failed（退避重试），不能钉进待处理清单。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    async def identify_outage(session, kind, watch_root, main, spec):
        raise ingest_mod.IdentifyUnavailable("TMDB 暂时不可达（模拟）")

    monkeypatch.setattr(ingest_mod, "_identify", identify_outage)

    entry = watch / "some-release"
    entry.mkdir()
    (entry / "video.mkv").write_bytes(b"x")

    await _sweep_twice(db, library_id, watch)
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.FAILED
    assert "自动重试" in (record.message or "")


@pytest.mark.asyncio
async def test_tv_import_and_incremental_episodes(db, tmp_path, monkeypatch):
    """剧集：按季集落 Season 目录；季包补集后指纹变化，增量导入新集。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)
    # 季集解析依赖 NER 模型（测试环境缺失），按文件名打桩：epN.mkv → S01EN
    monkeypatch.setattr(
        ingest_mod, "_unit", lambda file, entry: (1, int(file.stem.removeprefix("ep")))
    )

    entry = watch / "测试剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"e1")
    (entry / "ep2.mkv").write_bytes(b"e2")

    await _sweep_twice(db, library_id, watch)

    season_dir = root / "测试剧集 (2024)" / "Season 01"
    assert (season_dir / "测试剧集 (2024) - S01E01.mkv").read_bytes() == b"e1"
    assert (season_dir / "测试剧集 (2024) - S01E02.mkv").read_bytes() == b"e2"

    # 补集：指纹变化 → 重新处理，已在库的 E01/E02 幂等跳过，只新增 E03
    (entry / "ep3.mkv").write_bytes(b"e3")
    await _sweep_twice(db, library_id, watch)
    assert (season_dir / "测试剧集 (2024) - S01E03.mkv").read_bytes() == b"e3"
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert len(files) == 3
    assert record.imported_count == 3


@pytest.mark.asyncio
async def test_downloader_signal_is_authoritative(db, tmp_path, monkeypatch):
    """名称匹配到下载器种子：未完成时任凭静默也不导入；完成则单轮立即导入。"""
    from movieclaw_downloader import TorrentBrief

    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    brief = TorrentBrief(name="Some.Movie.2020", content_name="Some.Movie.2020", completed=False)

    async def briefs():
        return [brief]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)

    entry = watch / "Some.Movie.2020"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")

    # 下载器说没完成：静默窗口为 0 也不能导入（暂停种子的根治场景）；
    # 条目须进入挂起表——完成瞬间 API 慢半拍时全靠它被轮询/兜底接住（回归）
    await _sweep_twice(db, library_id, watch)
    await _sweep_twice(db, library_id, watch)
    assert not (root / "某电影 (2020)").exists()
    assert str(entry) in ingest_mod._deferred

    # 下载器确认完成：无需静默等待，单轮巡检立即导入，挂起记录清除
    brief.completed = True
    library = await _get_library(db, library_id)
    await ingest_mod._sweep_dir(
        _fixed_rule(watch, library_id=library_id), library, execute_inline=True
    )
    assert (root / "某电影 (2020)" / "某电影 (2020).mkv").read_bytes() == b"video"


@pytest.mark.asyncio
async def test_manual_download_identity_claim_via_info_hash(db, tmp_path, monkeypatch):
    """手动下载投到共享监听目录后，按提交时锚定的身份和库入库。"""
    from movieclaw_downloader import TorrentBrief

    default_root, target_root, watch = tmp_path / "default", tmp_path / "target", tmp_path / "watch"
    watch.mkdir()
    default_library_id = await _make_library(db, kind=MediaKind.MOVIE, root=default_root)
    target_root.mkdir()
    async with db.session() as session:
        target = await LibraryRepository(session).create(
            name="手动下载目标库",
            kind=MediaKind.MOVIE.value,
            root_paths=[str(target_root)],
        )
        assert target.id is not None
        target_library_id = target.id
    item = await _make_item(db, kind=MediaKind.MOVIE, title="手动确认影片", year=2024)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    # 名称识别链必然失败，只有手动提交时保存的 hash 身份锚能让导入成功。
    async def identify_none(session, kind, watch_root, main, spec):
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)
    async with db.session() as session:
        assert item.id is not None
        session.add(
            ManualDownloadIntent(
                info_hash="manualhash",
                media_item_id=item.id,
                library_id=target_library_id,
                site_id="mteam",
            )
        )
        await session.commit()

    brief = TorrentBrief(
        name="Cryptic.Manual.Release",
        content_name="Cryptic.Manual.Release",
        completed=True,
        info_hash="manualhash",
    )

    async def briefs():
        return [brief]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)
    entry = watch / "Cryptic.Manual.Release"
    entry.mkdir()
    (entry / "video.mkv").write_bytes(b"video")

    # auto 监听规则没有指定库；若没有手动身份锚，会走到 identify_none → pending。
    rule = ImportWatch(source_path=str(watch), strategy="hardlink", library_id=None, kind="movie")
    await ingest_mod._sweep_dir(rule, None, execute_inline=True)

    assert (target_root / "手动确认影片 (2024)" / "手动确认影片 (2024).mkv").exists()
    assert not (default_root / "手动确认影片 (2024)").exists()
    assert default_library_id != target_library_id
    assert str(entry) not in ingest_mod._deferred
    async with db.session() as session:
        assert (await session.execute(select(ManualDownloadIntent))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_probe_gate_applies_per_file(db, tmp_path, monkeypatch):
    """探测门禁逐文件生效：季包里残缺的单集被拦下，完整的集照常入库。"""
    root, watch = tmp_path / "tv", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.TV, root=root)
    item = await _make_item(db, kind=MediaKind.TV, title="测试剧集", year=2024)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "ffprobe_available", lambda: True)
    # ep2 残缺：探测失败；其余正常
    monkeypatch.setattr(
        ingest_mod, "probe_media", lambda p: None if "ep2" in str(p) else _FAKE_SPEC
    )
    monkeypatch.setattr(
        ingest_mod, "_unit", lambda file, entry: (1, int(file.stem.removeprefix("ep")))
    )

    entry = watch / "测试剧集 S01"
    entry.mkdir()
    (entry / "ep1.mkv").write_bytes(b"full-episode")  # 最大文件 = 主文件，探测通过
    (entry / "ep2.mkv").write_bytes(b"partial")

    await _sweep_twice(db, library_id, watch)

    season_dir = root / "测试剧集 (2024)" / "Season 01"
    assert (season_dir / "测试剧集 (2024) - S01E01.mkv").exists()
    assert not (season_dir / "测试剧集 (2024) - S01E02.mkv").exists()
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.IMPORTED
    assert record.imported_count == 1
    assert "探测失败" in (record.message or "")


@pytest.mark.asyncio
async def test_wanted_identity_claim_via_info_hash(db, tmp_path, monkeypatch):
    """匹配到订阅工单的种子：继承投递时锚定的精确身份，不走名称识别链。"""
    from movieclaw_db.models import RuleSet, Subscription, WantedItem, WantedStatus
    from movieclaw_downloader import TorrentBrief

    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    # 名称识别链打桩为必然失败：只有工单认领能给出身份
    async def identify_none(session, kind, watch_root, main, spec):
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)

    async with db.session() as session:
        rule_set = RuleSet(name="默认", spec={})
        session.add(rule_set)
        await session.commit()
        await session.refresh(rule_set)
        sub = Subscription(
            media_item_id=item.id, kind="movie", rule_set_id=rule_set.id, library_id=library_id
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        session.add(
            WantedItem(
                subscription_id=sub.id,
                media_item_id=item.id,
                season_number=0,
                episode_number=0,
                status=WantedStatus.GRABBED,
                info_hash="abc123",
            )
        )
        await session.commit()

    brief = TorrentBrief(
        name="Cryptic.Release.Name",
        content_name="Cryptic.Release.Name",
        completed=True,
        info_hash="abc123",
    )

    async def briefs():
        return [brief]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)

    entry = watch / "Cryptic.Release.Name"
    entry.mkdir()
    (entry / "video.mkv").write_bytes(b"video")

    library = await _get_library(db, library_id)
    # 下载器确认完成,单轮即处理
    await ingest_mod._sweep_dir(
        _fixed_rule(watch, library_id=library_id), library, execute_inline=True
    )
    assert (root / "某电影 (2020)" / "某电影 (2020).mkv").read_bytes() == b"video"

    # 库存对账闭环：入库单元关闭了对应工单（订阅止于投递的另一半）
    async with db.session() as session:
        wanted = (await session.execute(select(WantedItem))).scalars().one()
    assert wanted.status == WantedStatus.IMPORTED


@pytest.mark.asyncio
async def test_fallback_only_sweeps_unwatched_dirs(db, tmp_path, monkeypatch):
    """兜底巡检只扫监听覆盖不到的目录：被实时监听的目录绝不重复主动扫。"""
    root = tmp_path / "movies"
    watch1, watch2 = tmp_path / "watch1", tmp_path / "watch2"
    watch1.mkdir()
    watch2.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch1)
    await _make_rule(db, library_id=library_id, source=watch2)

    swept: list[str] = []

    async def record_sweep(rule, library):
        swept.append(rule.source_path)

    monkeypatch.setattr(ingest_mod, "_sweep_dir", record_sweep)

    class _StubWatcher:
        """只监听 watch1 的假观察者。"""

        def watched_keys(self):
            return frozenset({str(watch1)})

        async def refresh_watches(self):
            pass

    monkeypatch.setattr(ingest_mod, "_watcher", _StubWatcher())

    await ingest_mod.ingest_tick()
    assert swept == [str(watch2)]


@pytest.mark.asyncio
async def test_fallback_sweeps_watched_dir_with_pending_entries(db, tmp_path, monkeypatch):
    """回归：被监听目录里还有等待中的条目时，兜底巡检不再跳过它。

    线上实证 bug：下载完成瞬间下载器 API 仍报「未完成」→ 条目被挂起后
    无人唤醒（无新事件、静默自检挂不上、兜底又一刀切跳过被监听目录），
    入库延迟 1 小时+ 且全程无告警。兜底巡检是自检链死亡后的最后保险。
    """
    root = tmp_path / "movies"
    watch1, watch2 = tmp_path / "watch1", tmp_path / "watch2"
    watch1.mkdir()
    watch2.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch1)
    await _make_rule(db, library_id=library_id, source=watch2)

    swept: list[str] = []

    async def record_sweep(rule, library):
        swept.append(rule.source_path)

    monkeypatch.setattr(ingest_mod, "_sweep_dir", record_sweep)

    class _StubWatcher:
        """两个目录都在监听的假观察者。"""

        def watched_keys(self):
            return frozenset({str(watch1), str(watch2)})

        async def refresh_watches(self):
            pass

    monkeypatch.setattr(ingest_mod, "_watcher", _StubWatcher())
    # watch1 里有一个挂起条目（下载器报未完成）；watch2 没有任何等待
    ingest_mod._deferred[str(watch1 / "Some.Movie.2020")] = 0.0

    await ingest_mod.ingest_tick()
    assert swept == [str(watch1)]


@pytest.mark.asyncio
async def test_failed_entry_retried_by_fallback_without_new_events(db, tmp_path, monkeypatch):
    """回归：失败条目必须有唤醒源。此前失败结论落账时静默表/挂起表都被
    弹出、下载结束后也不会再有 fs 事件——三条触发路径同时失灵，台账里
    承诺的「自动退避重试」永远不会发生。修复后失败条目记入进程内失败
    重试表：_has_pending 看得见它，被实时监听的目录在没有任何新事件时
    也会被兜底巡检接住并重试。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="某电影", year=2020)
    _stub_identify(monkeypatch, item)
    # 环境故障：ffprobe 在但探测失败 → failed
    monkeypatch.setattr(ingest_mod, "ffprobe_available", lambda: True)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: None)

    entry = watch / "某电影 (2020)"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")
    await _sweep_twice(db, library_id, watch)
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.FAILED
    # 失败落账后条目留在失败重试表：兜底巡检据此不再跳过该目录（修复核心）
    assert ingest_mod._has_pending(str(watch))

    # 环境修复 + 退避到点：目录被实时监听、没有任何新 fs 事件——兜底巡检
    # 仍要看见失败条目并重试成功
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)
    monkeypatch.setattr(ingest_mod, "FAILED_RETRY_SECONDS", 0)

    class _StubWatcher:
        """目录在实时监听中、但不投递任何事件的假观察者。"""

        def watched_keys(self):
            return frozenset({str(watch)})

        async def refresh_watches(self):
            pass

        def _arm_recheck(self, source_path):
            pass

    monkeypatch.setattr(ingest_mod, "_watcher", _StubWatcher())
    await ingest_mod.ingest_tick()  # 第一轮：重新记录静默指纹
    await ingest_mod.ingest_tick()  # 第二轮：静默确认 → 创建可恢复入库 Job
    async with db.session() as session:
        job = (
            await session.execute(select(Job).where(Job.job_type == "library.ingest"))
        ).scalar_one()
    await jobs.init_job_dispatcher(max_parallel=1)
    deadline = asyncio.get_running_loop().time() + 3
    while True:
        async with db.session() as session:
            job = await session.get(Job, job.id)
            assert job is not None
        if job.status == JobStatus.SUCCEEDED:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"监听入库 Job 未完成，当前状态：{job.status}")
        await asyncio.sleep(0.01)
    assert (root / "某电影 (2020)" / "某电影 (2020).mkv").read_bytes() == b"video"
    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
    assert record.status == IngestStatus.IMPORTED


@pytest.mark.asyncio
async def test_persistent_ingest_copy_resumes_after_dispatcher_restart(db, tmp_path, monkeypatch):
    """复制到一半更新服务：作业退回队列，隐藏副本保留并从已有字节续跑。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch, strategy="copy")
    item = await _make_item(db, kind=MediaKind.MOVIE, title="续传电影", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    monkeypatch.setattr(ingest_mod, "_INGEST_COPY_CHUNK_BYTES", 8)

    async def no_assets(_media_item_id: int) -> None:
        return None

    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)

    entry = watch / "续传电影 (2026)"
    entry.mkdir()
    source = entry / "movie.mkv"
    source.write_bytes(bytes(range(128)))

    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)

    async with db.session() as session:
        job = (
            await session.execute(select(Job).where(Job.job_type == "library.ingest"))
        ).scalar_one()
        resources = await jobs.resources_for_jobs(session, [job.id])
    assert {row.resource_type for row in resources[job.id]} >= {
        "import_watch",
        "ingest_path",
        "library",
    }
    final = root / "续传电影 (2026)" / "续传电影 (2026).mkv"
    partial, state_path = ingest_mod._ingest_copy_paths(final)
    assert not final.exists()  # 巡检只入队，不在监听协程里直接搬运

    loop = asyncio.get_running_loop()
    first_chunk = asyncio.Event()
    release_thread = threading.Event()
    thread_finished = threading.Event()
    offsets: list[int] = []
    real_copy_chunk = ingest_mod._copy_ingest_chunk

    def controlled_copy_chunk(source_path, partial_path, **kwargs):
        offsets.append(partial_path.stat().st_size if partial_path.exists() else 0)
        copied = real_copy_chunk(source_path, partial_path, **kwargs)
        if len(offsets) == 1:
            loop.call_soon_threadsafe(first_chunk.set)
            release_thread.wait(timeout=2)
            thread_finished.set()
        return copied

    monkeypatch.setattr(ingest_mod, "_copy_ingest_chunk", controlled_copy_chunk)
    await jobs.init_job_dispatcher(max_parallel=1)
    await asyncio.wait_for(first_chunk.wait(), timeout=2)
    assert partial.stat().st_size == 8
    await jobs.close_job_dispatcher()
    paused = await _wait_job_status(job.id, JobStatus.QUEUED)
    assert paused.attempt == 0
    assert partial.stat().st_size == 8

    release_thread.set()
    assert await asyncio.to_thread(thread_finished.wait, 2)
    await jobs.init_job_dispatcher(max_parallel=1)
    completed = await _wait_job_status(job.id, JobStatus.SUCCEEDED)

    assert final.read_bytes() == source.read_bytes()
    assert offsets[0] == 0 and offsets[1] == 8
    assert not partial.exists() and not state_path.exists()
    assert completed.progress["percent"] == 100.0


@pytest.mark.asyncio
async def test_pending_ingest_claim_unblocks_same_job(db, tmp_path, monkeypatch):
    """识别不出时 Job 待处理；人工认领后复用同一稳定 job id 完成入库。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="认领后入库", year=2026)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)

    async def identify_none(*_args):
        return None

    async def ensure_item(_service, kind, tmdb_id):
        assert kind is MediaKind.MOVIE and tmdb_id == item.tmdb_id
        return item

    async def no_assets(_media_item_id: int) -> None:
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)
    monkeypatch.setattr(ingest_mod.MediaLibraryService, "ensure_media_item", ensure_item)
    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)

    entry = watch / "unknown-release"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")
    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)

    async with db.session() as session:
        job = (
            await session.execute(select(Job).where(Job.job_type == "library.ingest"))
        ).scalar_one()
    await jobs.init_job_dispatcher(max_parallel=1)
    blocked = await _wait_job_status(job.id, JobStatus.BLOCKED)
    assert blocked.error["code"] == "INGEST_IDENTITY_REQUIRED"

    async with db.session() as session:
        record = (await session.execute(select(IngestEntry))).scalar_one()
        assert record.status == IngestStatus.PENDING
        await ingest_mod.claim_entry(session, record.id, item.tmdb_id)

    completed = await _wait_job_status(job.id, JobStatus.SUCCEEDED)
    assert completed.id == job.id
    assert (root / "认领后入库 (2026)" / "认领后入库 (2026).mkv").exists()


@pytest.mark.asyncio
async def test_cancelled_ingest_is_not_resurrected_by_startup_sweep(db, tmp_path, monkeypatch):
    """用户取消的同一磁盘版本不能在下次启动补扫时被静默重新创建。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="取消入库", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)
    entry = watch / "取消入库 (2026)"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")

    async with db.session() as session:
        rule = (await session.execute(select(ImportWatch))).scalar_one()
        library = await session.get(Library, library_id)
        assert library is not None
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        job = (await session.execute(select(Job))).scalar_one()
        await jobs.request_cancel(session, job.id, requested_by="test")

    monkeypatch.setattr(ingest_mod, "_stability", {})  # 模拟新进程启动
    await ingest_mod._sweep_dir(rule, library)
    await ingest_mod._sweep_dir(rule, library)
    async with db.session() as session:
        all_jobs = list((await session.execute(select(Job))).scalars())
    assert len(all_jobs) == 1 and all_jobs[0].status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_auto_routed_ingest_adds_target_library_resource(db, tmp_path, monkeypatch):
    """自动路由在识别后补充真实库资源，让所有库级作业共享同一互斥锁。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    item = await _make_item(db, kind=MediaKind.MOVIE, title="自动路由电影", year=2026)
    _stub_identify(monkeypatch, item)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda _path: _FAKE_SPEC)

    async def no_assets(_media_item_id: int) -> None:
        return None

    monkeypatch.setattr("movieclaw_api.services.media_scrape.ensure_assets", no_assets)
    entry = watch / "自动路由电影 (2026)"
    entry.mkdir()
    (entry / "movie.mkv").write_bytes(b"video")
    async with db.session() as session:
        rule = ImportWatch(
            source_path=str(watch),
            strategy="hardlink",
            library_id=None,
            kind="movie",
        )
        session.add(rule)
        await session.commit()
        await session.refresh(rule)
    await ingest_mod._sweep_dir(rule, None)
    await ingest_mod._sweep_dir(rule, None)

    async with db.session() as session:
        job = (await session.execute(select(Job))).scalar_one()
    await jobs.init_job_dispatcher(max_parallel=1)
    await _wait_job_status(job.id, JobStatus.SUCCEEDED)
    async with db.session() as session:
        resources = await jobs.resources_for_jobs(session, [job.id])
    assert any(
        row.resource_type == "library"
        and row.resource_id == str(library_id)
        and row.relation == "target"
        for row in resources[job.id]
    )
    assert not ingest_mod._has_pending(str(watch))  # 重试成功后重试表清空


@pytest.mark.asyncio
async def test_deferred_recheck_polls_api_and_wakes_on_flip(db, tmp_path, monkeypatch):
    """挂起条目的状态轮询自检：种子未完成时只查 API 重挂下一轮（不触发
    巡检）；翻转成完成后立即唤醒对应目录的巡检。"""
    from movieclaw_downloader import TorrentBrief

    watch = tmp_path / "watch"
    watch.mkdir()
    brief = TorrentBrief(name="Some.Movie.2020", content_name="Some.Movie.2020", completed=False)

    async def briefs():
        return [brief]

    monkeypatch.setattr(ingest_mod, "_downloader_briefs", briefs)
    monkeypatch.setattr(ingest_mod, "DEFERRED_POLL_SECONDS", 0.01)
    ingest_mod._deferred[str(watch / "Some.Movie.2020")] = 0.0

    watcher = ingest_mod.IngestWatcher()
    try:
        watcher._arm_recheck(str(watch))
        await asyncio.sleep(0.1)
        assert watcher._queue.empty()  # 未翻转：不巡检，持续重挂
        brief.completed = True
        await asyncio.sleep(0.1)
        assert watcher._queue.get_nowait() == str(watch)  # 翻转：唤醒巡检
    finally:
        for task in watcher._rechecks.values():
            task.cancel()


def test_ingest_event_filter_ignores_read_only_events():
    """只读事件（做种上传/ffprobe 读文件）不触发巡检；写类事件放行。"""
    assert not ingest_mod._is_ingest_relevant(SimpleNamespace(event_type="opened"))
    assert not ingest_mod._is_ingest_relevant(SimpleNamespace(event_type="closed_no_write"))
    for kind in ("created", "modified", "moved", "deleted", "closed"):
        assert ingest_mod._is_ingest_relevant(SimpleNamespace(event_type=kind))


@pytest.mark.asyncio
async def test_rule_validation(db, tmp_path):
    """规则校验：与任何库根重叠拒绝、源目录去重、坏策略拒绝、同盘检测。"""
    root = tmp_path / "movies"
    library_id = await _make_library(db, kind=MediaKind.MOVIE, root=root)
    watch = tmp_path / "watch"
    watch.mkdir()
    async with db.session() as session:
        service = ImportWatchConfigService(session)
        with pytest.raises(BadRequestException):
            await service.create(
                source_path=str(root / "inbox"), strategy="hardlink", library_id=library_id
            )
        with pytest.raises(BadRequestException):
            await service.create(source_path=str(watch), strategy="move", library_id=library_id)
        # 合法创建；同源目录再建拒绝
        await service.create(source_path=str(watch), strategy="copy", library_id=library_id)
        with pytest.raises(BadRequestException):
            await service.create(source_path=str(watch), strategy="hardlink", library_id=library_id)


@pytest.mark.asyncio
async def test_entry_stats_counts_entries_and_imported_files(db, tmp_path):
    """摘要行两口径：状态 → 条目数，外加已入库文件总数（剧集季包一条目多集）。

    只认源目录顶层直系条目：嵌套子路径与其他源目录的行不串数；
    文件数只累计已入库条目（pending 行的 imported_count 不算）。
    """
    watch, other = tmp_path / "watch", tmp_path / "other"
    async with db.session() as session:
        rule = ImportWatch(source_path=str(watch), strategy="hardlink", kind="tv")
        other_rule = ImportWatch(source_path=str(other), strategy="hardlink", kind="movie")
        session.add(rule)
        session.add(other_rule)
        for path, status, files in (
            (watch / "剧A.S01", "imported", 8),
            (watch / "剧A.S02", "imported", 2),
            (watch / "认不出的目录", "pending", 0),
            (watch / "剧A.S01" / "nested", "imported", 99),  # 嵌套路径不属于本规则
            (other / "电影B", "imported", 1),
        ):
            session.add(
                IngestEntry(
                    entry_path=str(path), fingerprint="fp", status=status, imported_count=files
                )
            )
        await session.commit()
        await session.refresh(rule)
        await session.refresh(other_rule)

        stats = await ingest_mod.entry_stats(session, [rule, other_rule])

    ledger = stats[rule.id]
    assert ledger.counts["imported"] == 2
    assert ledger.counts["pending"] == 1
    assert ledger.imported_files == 10  # 8 + 2：嵌套行与 pending 行都不计
    assert stats[other_rule.id].counts["imported"] == 1
    assert stats[other_rule.id].imported_files == 1


@pytest.mark.asyncio
async def test_entry_stats_dedupes_works_across_seasons_and_versions(db, tmp_path):
    """「已入库」报作品数：同一部剧的多季与多版本合并计一部。

    真实场景（用户只入了 4 部剧却显示「已入库 11」）：一部剧的 S01/S02 各是
    一个条目，同一季的不同发布组/DV/HDR 版本又各是一个条目——条目数远大于
    作品数。按 media_item_id 去重后才是"入库了几部"。
    没有 media_item_id 的老条目（迁移前入库、回填未命中）按条目各计一部，
    宁可少合并也不凭标题猜。
    """
    watch = tmp_path / "watch"
    async with db.session() as session:
        rule = ImportWatch(source_path=str(watch), strategy="hardlink", kind="tv")
        session.add(rule)
        for name, item_id, files in (
            ("剧A.S01.CHDWEB", 1, 8),  # 同一部剧：两季 + S02 三个版本
            ("剧A.S02.MWeb.DV", 1, 6),
            ("剧A.S02.MWeb.HDR", 1, 6),
            ("剧A.S02.CMCTV.DV", 1, 6),
            ("剧B.S01", 2, 10),
            ("剧C.S01", None, 3),  # 老条目：未回填，单独计一部
            ("认不出的", None, 0),  # pending 不计入作品数
        ):
            session.add(
                IngestEntry(
                    entry_path=str(watch / name),
                    fingerprint=f"fp-{name}",
                    status="imported" if files else "pending",
                    imported_count=files,
                    media_item_id=item_id,
                )
            )
        await session.commit()
        await session.refresh(rule)

        stats = await ingest_mod.entry_stats(session, [rule])

    ledger = stats[rule.id]
    assert ledger.counts["imported"] == 6  # 条目数照旧
    assert ledger.imported_works == 3  # 剧A + 剧B + 未回填的剧C
    assert ledger.imported_files == 39
