"""外挂字幕发现与命名解析（A 层，docs/design/jellyfin-subtitle.md §2）。"""

from __future__ import annotations

from pathlib import Path

from movieclaw_api.services.library.scan import _refresh_known_row
from movieclaw_api.services.library.subtitles import (
    discover_external_subtitles,
    match_subtitle_filename,
    parse_subtitle_tokens,
)
from movieclaw_db.models import LibraryFile

# ---------------------------------------------------------------------------
# 前缀匹配（§2.1）
# ---------------------------------------------------------------------------


def test_match_exact_stem() -> None:
    assert match_subtitle_filename("Movie", "Movie.srt") == ""


def test_match_with_tokens() -> None:
    assert match_subtitle_filename("Movie", "Movie.chs.default.srt") == "chs.default"


def test_match_case_insensitive_prefix() -> None:
    assert match_subtitle_filename("movie", "MOVIE.chs.ass") == "chs"


def test_no_match_stem_collision_without_delimiter() -> None:
    # Movie2.srt 不是 Movie 的字幕：前缀后必须是分隔符 "."
    assert match_subtitle_filename("Movie", "Movie2.srt") is None


def test_prefix_overlap_longer_stem_matches_shorter_video() -> None:
    # 多版本同目录的固有特性（对齐真 Jellyfin）：长 stem 的字幕会匹配到
    # 短 stem 的视频，多余 token 进 title
    assert match_subtitle_filename("Movie", "Movie.2160p.chs.srt") == "2160p.chs"


def test_no_match_non_subtitle_extension() -> None:
    assert match_subtitle_filename("Movie", "Movie.nfo") is None
    assert match_subtitle_filename("Movie", "Movie.sub") is None  # 歧义格式不收
    assert match_subtitle_filename("Movie", "Movie.sup") is None


def test_no_match_unrelated_name() -> None:
    assert match_subtitle_filename("Movie", "Other.srt") is None


# ---------------------------------------------------------------------------
# token 解析（§2.2，从右往左、大小写不敏感、旗标整段相等）
# ---------------------------------------------------------------------------


def test_tokens_language_and_flags() -> None:
    parsed = parse_subtitle_tokens("chs.default.forced")
    assert parsed["language"] == "chi"
    assert parsed["default"] is True
    assert parsed["forced"] is True
    assert parsed["sdh"] is False
    assert parsed["title"] is None


def test_tokens_case_insensitive() -> None:
    parsed = parse_subtitle_tokens("CHS.Default")
    assert parsed["language"] == "chi"
    assert parsed["default"] is True


def test_tokens_chinese_aliases() -> None:
    # 比 Jellyfin 多收的中文命名别名
    assert parse_subtitle_tokens("简中")["language"] == "chi"
    assert parse_subtitle_tokens("繁体")["language"] == "chi"
    assert parse_subtitle_tokens("双语")["language"] == "chi"


def test_tokens_unknown_goes_to_title_in_order() -> None:
    parsed = parse_subtitle_tokens("某字幕组.特效.chs")
    assert parsed["language"] == "chi"
    assert parsed["title"] == "某字幕组.特效"  # 还原原始顺序


def test_tokens_hi_is_sdh_not_hindi() -> None:
    # "hi" 与印地语撞名：我们只当听障旗标（语言表刻意不收 hi）
    parsed = parse_subtitle_tokens("hi")
    assert parsed["sdh"] is True
    assert parsed["language"] is None
    # 印地语用完整码仍可命中
    assert parse_subtitle_tokens("hin")["language"] == "hin"


def test_tokens_flag_requires_whole_segment() -> None:
    # 与 Jellyfin 的有意差异：Contains 会把巧合子串误判，我们整段相等
    parsed = parse_subtitle_tokens("mydefaultgroup")
    assert parsed["default"] is False
    assert parsed["title"] == "mydefaultgroup"


def test_tokens_foreign_and_cc() -> None:
    parsed = parse_subtitle_tokens("eng.foreign.cc")
    assert parsed["language"] == "eng"
    assert parsed["forced"] is True
    assert parsed["sdh"] is True


# ---------------------------------------------------------------------------
# 目录发现（§2.1/§2.3：stat 落 size/mtime，文件名序）
# ---------------------------------------------------------------------------


def _touch(path: Path, content: bytes = b"1\n00:00:01,000 --> 00:00:02,000\nx\n") -> None:
    path.write_bytes(content)


def test_discover_returns_ledger_entries(tmp_path: Path) -> None:
    video = tmp_path / "Movie.2024.mkv"
    video.write_bytes(b"fake")
    _touch(tmp_path / "Movie.2024.chs.default.srt")
    _touch(tmp_path / "Movie.2024.eng.ass")
    _touch(tmp_path / "Other.srt")  # 不相关
    (tmp_path / "Movie.2024.nfo").write_text("x")  # 非字幕

    entries = discover_external_subtitles(video)
    assert [e["filename"] for e in entries] == [
        "Movie.2024.chs.default.srt",
        "Movie.2024.eng.ass",
    ]
    chs = entries[0]
    assert chs["format"] == "srt"
    assert chs["language"] == "chi"
    assert chs["default"] is True
    assert chs["size_bytes"] > 0
    assert chs["file_mtime_ns"] > 0
    assert entries[1]["format"] == "ass"
    assert entries[1]["language"] == "eng"


def test_discover_with_provided_dir_names_skips_listing(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")
    _touch(tmp_path / "Movie.chs.srt")
    # 只把部分文件名交给它：不该自己再去列目录
    entries = discover_external_subtitles(video, dir_names=["Movie.chs.srt"])
    assert len(entries) == 1


def test_discover_empty_when_no_sidecars(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")
    assert discover_external_subtitles(video) == []


def test_discover_strm_sidecar(tmp_path: Path) -> None:
    """strm 占位文件旁的本地字幕照常发现（云播放 + 本地字幕的核心场景）。"""
    strm = tmp_path / "Movie.strm"
    strm.write_text("https://pan.example.com/movie.mp4\n")
    _touch(tmp_path / "Movie.chs.srt")
    entries = discover_external_subtitles(strm)
    assert [e["filename"] for e in entries] == ["Movie.chs.srt"]


# ---------------------------------------------------------------------------
# 秒过行的增量刷新（§2.4：外挂重发现 + mtime 变→重探 / 不变→零探测）
# ---------------------------------------------------------------------------


def _row_for(video: Path) -> LibraryFile:
    st = video.stat()
    return LibraryFile(
        library_id=1,
        media_item_id=1,
        file_path=str(video),
        size_bytes=st.st_size,
        file_mtime_ns=st.st_mtime_ns,
        container="mkv",
        audio_streams=[{"codec": "aac"}],
        subtitle_streams=[],
        source="scanned",
    )


async def test_refresh_backfills_external_subtitles(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")
    _touch(tmp_path / "Movie.chs.srt")
    row = _row_for(video)
    assert row.external_subtitles is None  # 旧行三态：未发现过

    changed = await _refresh_known_row(
        row, video, None, is_disc=False, check_media_change=False
    )
    assert changed is True
    assert [e["filename"] for e in row.external_subtitles] == ["Movie.chs.srt"]

    # 再跑一轮无变化：不产生写
    changed = await _refresh_known_row(
        row, video, None, is_disc=False, check_media_change=False
    )
    assert changed is False


async def test_refresh_disc_row_gets_empty_list(tmp_path: Path) -> None:
    disc = tmp_path / "MOVIE_DISC"
    disc.mkdir()
    row = _row_for_dir(disc)
    changed = await _refresh_known_row(
        row, disc, None, is_disc=True, check_media_change=True
    )
    assert changed is True
    assert row.external_subtitles == []


def _row_for_dir(path: Path) -> LibraryFile:
    return LibraryFile(
        library_id=1,
        media_item_id=1,
        file_path=str(path),
        source="scanned",
    )


async def test_refresh_reprobes_only_when_media_changed(
    tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")
    row = _row_for(video)
    row.external_subtitles = []

    calls: list[Path] = []

    def fake_probe(path):  # noqa: ANN001
        from movieclaw_api.services.media_probe import MediaSpec

        calls.append(Path(path))
        return MediaSpec(
            resolution="1080p",
            video_codec="hevc",
            hdr=None,
            bit_depth=10,
            duration_seconds=100,
            bit_rate=5_000_000,
            audio_streams=[{"codec": "flac"}],
            subtitle_streams=[{"codec": "ass", "language": "chi"}],
        )

    import movieclaw_api.services.library.scan as scan_mod

    monkeypatch.setattr(scan_mod, "probe_media", fake_probe)

    # (size, mtime) 未变：点名了也零探测（"文件不变就不更新"）
    changed = await _refresh_known_row(
        row, video, None, is_disc=False, check_media_change=True
    )
    assert changed is False
    assert calls == []

    # 内容变化（大小变了）：确认后重探并回写规格 + 新鲜度键
    video.write_bytes(b"replaced-with-bigger-content")
    changed = await _refresh_known_row(
        row, video, None, is_disc=False, check_media_change=True
    )
    assert changed is True
    assert calls == [video]
    assert row.video_codec == "hevc"
    assert row.subtitle_streams == [{"codec": "ass", "language": "chi"}]
    assert row.size_bytes == video.stat().st_size
    assert row.file_mtime_ns == video.stat().st_mtime_ns


async def test_refresh_probe_failure_keeps_freshness_key(
    tmp_path: Path, monkeypatch
) -> None:
    """半截写入的替换文件探测失败：保留旧新鲜度键，下一轮还会重试。"""
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")
    row = _row_for(video)
    row.external_subtitles = []
    old_mtime = row.file_mtime_ns

    import movieclaw_api.services.library.scan as scan_mod

    monkeypatch.setattr(scan_mod, "probe_media", lambda _p: None)
    video.write_bytes(b"half-written-replacement!")
    changed = await _refresh_known_row(
        row, video, None, is_disc=False, check_media_change=True
    )
    assert changed is False
    assert row.file_mtime_ns == old_mtime
