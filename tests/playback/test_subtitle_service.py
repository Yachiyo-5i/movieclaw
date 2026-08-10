"""字幕播放领域服务（B 层，docs/design/jellyfin-subtitle.md §3）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from movieclaw_db.models import LibraryFile
from movieclaw_playback.subtitles import (
    SUBTITLE_OFF,
    SubtitleRef,
    SubtitleServeError,
    embedded_track,
    external_track,
    parse_embedded_track,
    parse_external_track,
    pick_default_subtitle,
    resolve_default_audio,
    resolve_default_subtitle,
    resolve_external_subtitle,
    serve_subtitle,
)

SRT = "1\n00:00:01,000 --> 00:00:02,500\n简体中文字幕测试\n\n"


# ---------------------------------------------------------------------------
# 中性轨引用（§3.1）
# ---------------------------------------------------------------------------


def test_track_ref_roundtrip() -> None:
    assert parse_embedded_track(embedded_track(2)) == 2
    assert parse_external_track(external_track("Movie.chs.srt")) == "Movie.chs.srt"


def test_track_ref_rejects_garbage() -> None:
    assert parse_embedded_track("external:x.srt") is None
    assert parse_embedded_track("embedded:notanint") is None
    assert parse_embedded_track("embedded:-1") is None
    assert parse_external_track("embedded:0") is None
    assert parse_external_track("external:") is None


# ---------------------------------------------------------------------------
# 内容服务（§3.2：编码归一 + srt↔vtt）
# ---------------------------------------------------------------------------


def _file_with_subs(path: str, externals: list[dict]) -> LibraryFile:
    return LibraryFile(
        library_id=1,
        media_item_id=1,
        file_path=path,
        external_subtitles=externals,
        source="scanned",
    )


def test_resolve_external_subtitle_builds_path(tmp_path: Path) -> None:
    f = _file_with_subs(
        str(tmp_path / "Movie.mkv"),
        [{"filename": "Movie.chs.srt", "format": "srt"}],
    )
    ref = resolve_external_subtitle(f, "external:Movie.chs.srt")
    assert ref == SubtitleRef(path=tmp_path / "Movie.chs.srt", format="srt")


def test_resolve_external_subtitle_rejects_embedded_and_unknown(tmp_path: Path) -> None:
    f = _file_with_subs(str(tmp_path / "Movie.mkv"), [])
    assert resolve_external_subtitle(f, "embedded:0") is None
    assert resolve_external_subtitle(f, "external:nope.srt") is None


def test_serve_gbk_srt_normalized_to_utf8(tmp_path: Path) -> None:
    """GBK 中文字幕 → UTF-8 输出（中文用户核心价值）。"""
    sub = tmp_path / "Movie.chs.srt"
    sub.write_bytes(SRT.encode("gbk"))
    content, mime = serve_subtitle(SubtitleRef(path=sub, format="srt"), "srt")
    assert mime == "application/x-subrip"
    assert "简体中文字幕测试" in content.decode("utf-8")


def test_serve_big5_normalized_to_utf8(tmp_path: Path) -> None:
    text = "1\n00:00:01,000 --> 00:00:02,000\n繁體中文字幕測試繁體字幕\n\n"
    sub = tmp_path / "Movie.cht.srt"
    sub.write_bytes(text.encode("big5"))
    content, _ = serve_subtitle(SubtitleRef(path=sub, format="srt"), None)
    assert "繁體中文字幕測試" in content.decode("utf-8")


def test_serve_srt_to_vtt(tmp_path: Path) -> None:
    sub = tmp_path / "Movie.srt"
    sub.write_text(SRT, encoding="utf-8")
    content, mime = serve_subtitle(SubtitleRef(path=sub, format="srt"), "vtt")
    text = content.decode("utf-8")
    assert mime == "text/vtt"
    assert text.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.500" in text  # vtt 用点不用逗号
    assert "简体中文字幕测试" in text


def test_serve_ass_passthrough_but_no_cross_conversion(tmp_path: Path) -> None:
    ass = tmp_path / "Movie.ass"
    ass.write_text(
        "[Script Info]\nTitle: t\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好\n",
        encoding="utf-8",
    )
    content, mime = serve_subtitle(SubtitleRef(path=ass, format="ass"), "ass")
    assert mime == "text/x-ssa"
    assert "你好" in content.decode("utf-8")
    with pytest.raises(SubtitleServeError):
        serve_subtitle(SubtitleRef(path=ass, format="ass"), "vtt")


def test_serve_empty_format_means_source_format(tmp_path: Path) -> None:
    sub = tmp_path / "Movie.srt"
    sub.write_text(SRT, encoding="utf-8")
    _, mime = serve_subtitle(SubtitleRef(path=sub, format="srt"), None)
    assert mime == "application/x-subrip"


def test_serve_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SubtitleServeError):
        serve_subtitle(SubtitleRef(path=tmp_path / "gone.srt", format="srt"), "srt")


# ---------------------------------------------------------------------------
# 默认字幕轨选择（§3.4：外挂 ↓ → default ↓ → 非 forced ↓，forced 沉底）
# ---------------------------------------------------------------------------


def _file_tracks(
    embedded: list[dict] | None, external: list[dict] | None
) -> LibraryFile:
    return LibraryFile(
        library_id=1,
        media_item_id=1,
        file_path="/media/Movie.mkv",
        subtitle_streams=embedded,
        external_subtitles=external,
        source="scanned",
    )


def test_pick_external_wins_over_embedded_default() -> None:
    f = _file_tracks(
        [{"codec": "subrip", "default": True}],
        [{"filename": "Movie.chs.srt", "format": "srt"}],
    )
    assert pick_default_subtitle(f) == "external:Movie.chs.srt"


def test_pick_default_flag_over_plain_embedded() -> None:
    f = _file_tracks(
        [{"codec": "subrip"}, {"codec": "ass", "default": True}],
        [],
    )
    assert pick_default_subtitle(f) == "embedded:1"


def test_pick_forced_sinks_below_full_subtitle() -> None:
    # forced 是外语片段部分字幕：有完整字幕可选时绝不选它（v4 终审修正项）
    f = _file_tracks(
        [{"codec": "subrip", "forced": True}, {"codec": "subrip", "default": True}],
        [],
    )
    assert pick_default_subtitle(f) == "embedded:1"


def test_pick_forced_only_when_nothing_else() -> None:
    f = _file_tracks([{"codec": "subrip", "forced": True}], [])
    assert pick_default_subtitle(f) == "embedded:0"


def test_pick_none_when_no_flags_and_no_external() -> None:
    # 只有无旗标的内封轨：不自动开字幕（Default 模式过滤条件不命中）
    f = _file_tracks([{"codec": "subrip"}], [])
    assert pick_default_subtitle(f) is None


def test_pick_external_default_flag_first_among_externals() -> None:
    f = _file_tracks(
        [],
        [
            {"filename": "Movie.eng.srt", "format": "srt"},
            {"filename": "Movie.chs.default.srt", "format": "srt", "default": True},
        ],
    )
    assert pick_default_subtitle(f) == "external:Movie.chs.default.srt"


# ---------------------------------------------------------------------------
# 记忆裁决（§3.3/§3.4：off 恒生效、失效回落）
# ---------------------------------------------------------------------------


def test_resolve_default_off_always_wins() -> None:
    f = _file_tracks([], [{"filename": "Movie.chs.srt", "format": "srt"}])
    assert resolve_default_subtitle(f, SUBTITLE_OFF) == SUBTITLE_OFF


def test_resolve_default_remembered_valid() -> None:
    f = _file_tracks([{"codec": "subrip"}], [])
    assert resolve_default_subtitle(f, "embedded:0") == "embedded:0"


def test_resolve_default_stale_falls_back_to_policy() -> None:
    f = _file_tracks([], [{"filename": "Movie.chs.srt", "format": "srt"}])
    # 记忆指向已删除的字幕文件 → 回落选择策略（选现存外挂）
    assert resolve_default_subtitle(f, "external:gone.srt") == "external:Movie.chs.srt"


def test_resolve_default_audio_validity() -> None:
    f = LibraryFile(
        library_id=1,
        media_item_id=1,
        file_path="/m.mkv",
        audio_streams=[{"codec": "aac"}, {"codec": "dts"}],
        source="scanned",
    )
    assert resolve_default_audio(f, "embedded:1") == 1
    assert resolve_default_audio(f, "embedded:5") is None  # 悬空
    assert resolve_default_audio(f, None) is None


# ---------------------------------------------------------------------------
# 架构守护：B 层不 import 协议层（jellyfin-subtitle.md §6）
# ---------------------------------------------------------------------------


def test_playback_domain_never_imports_jellyfin_layer() -> None:
    import ast

    src = Path(__file__).resolve().parents[2] / "src" / "movieclaw_playback"
    for py in src.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith("movieclaw_jellyfin"), (
                    f"领域层文件 {py.name} import 了协议层 {name}——"
                    "Jellyfin 只是翻译皮，方言不得渗入领域层"
                )
