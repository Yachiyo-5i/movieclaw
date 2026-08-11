"""AI 字幕生成 G1（docs/design/subtitle-ai-translate.md §10 验收）。

翻译管线用假 LLM 注入（translate 层与 movieclaw_llm 解耦的直接收益）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from movieclaw_api.services.subtitle_gen import extract, source, sync, translate, validate
from movieclaw_db.models import LibraryFile

# ---------------------------------------------------------------------------
# 选源打分矩阵（§2）
# ---------------------------------------------------------------------------


def _file(embedded: list[dict] | None, external: list[dict] | None) -> LibraryFile:
    return LibraryFile(
        library_id=1,
        media_item_id=1,
        file_path="/media/Movie.mkv",
        duration_seconds=6000,
        subtitle_streams=embedded,
        external_subtitles=external,
        source="scanned",
    )


def test_rank_excludes_forced_target_and_graphic() -> None:
    f = _file(
        [
            {"codec": "subrip", "language": "eng", "forced": True},
            {"codec": "hdmv_pgs_subtitle", "language": "eng"},
        ],
        [{"filename": "Movie.chs.srt", "format": "srt", "language": "chi"}],
    )
    ranked = source.rank_candidates(f, original_language="en", target_language="chs")
    excluded = {c.candidate.key: c.excluded for c in ranked}
    assert "forced" in excluded["0"]
    assert "图形字幕" in excluded["1"]
    assert "目标语言" in excluded["Movie.chs.srt"]


def test_rank_original_language_beats_english() -> None:
    f = _file(
        [{"codec": "subrip", "language": "eng"}, {"codec": "ass", "language": "jpn"}],
        [],
    )
    ranked = source.rank_candidates(f, original_language="ja", target_language="chs")
    usable = [c for c in ranked if not c.excluded]
    assert usable[0].candidate.language == "jpn"  # 原语言 > 英语


def test_rank_embedded_wins_tie_over_external() -> None:
    # v2.1 评审修正：同为英语时内封（官方字幕概率高）排在外挂前
    f = _file(
        [{"codec": "subrip", "language": "eng"}],
        [{"filename": "Movie.eng.srt", "format": "srt", "language": "eng"}],
    )
    ranked = source.rank_candidates(f, original_language="en", target_language="chs")
    usable = [c for c in ranked if not c.excluded]
    assert usable[0].candidate.kind == "embedded"


def test_assess_completeness() -> None:
    few = [(i * 1000, i * 1000 + 500, "x") for i in range(10)]
    assert not source.assess_events(few, 6000).ok  # 条数太少
    fragment = [(i * 1000, i * 1000 + 500, "x") for i in range(60)]
    assert not source.assess_events(fragment, 6000).ok  # 只覆盖开头 1 分钟
    full = [(i * 60_000, i * 60_000 + 2000, "x") for i in range(90)]
    assert source.assess_events(full, 6000).ok


# ---------------------------------------------------------------------------
# 机检（§3.3/§3.5：折行/标点/CPS）
# ---------------------------------------------------------------------------


def test_clean_punctuation_rules() -> None:
    assert validate.clean_punctuation("你好，世界。") == "你好 世界"
    # ？！/！！ 组合是 Netflix 禁用项，收敛为首个标点
    assert validate.clean_punctuation("真的吗？！！") == "真的吗？"
    assert validate.clean_punctuation("１９８４年") == "1984年"
    assert validate.clean_punctuation("等等…") == "等等…"  # 省略号保留


def test_fold_line_at_sixteen() -> None:
    text = "这是一句明显超过十六个字上限的很长很长的台词"
    folded = validate.fold_line(text)
    lines = folded.split("\n")
    assert len(lines) == 2
    assert len(lines[0]) <= 16


def test_fold_line_short_untouched() -> None:
    assert validate.fold_line("短句") == "短句"


def test_finalize_reports_cps_overrun() -> None:
    events = [(0, 1000, "这句话在一秒内绝对读不完所以必超读速上限")]  # 1s 内 20 字
    out, report = validate.finalize_events(events)
    assert report.cps_overrun == 1
    assert report.event_count == 1


def test_looks_translated_detects_untranslated() -> None:
    assert validate.looks_translated("你好", "chs")
    assert validate.looks_translated("OK", "chs")  # 短英文放过
    assert not validate.looks_translated("This is untranslated english", "chs")


# ---------------------------------------------------------------------------
# 翻译管线（§3：假 LLM、结构校验、断点续传、熔断）
# ---------------------------------------------------------------------------

CTX = translate.FilmContext(title="测试影片", year=2024, genres=["剧情"], overview="简介")


def _events(n: int) -> list[extract.SubEvent]:
    return [(i * 2000, i * 2000 + 1500, f"line {i}") for i in range(n)]


def _good_chat(monkeypatch=None):
    calls = {"n": 0}

    async def chat(system: str, user: str) -> str:
        calls["n"] += 1
        if "找出其中反复出现的人名" in user:
            return '[{"src": "John", "dst": "约翰"}]'
        block = json.loads(user[user.index("[") :])
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    return chat, calls


async def test_translate_happy_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # cache_dir 是相对路径,隔离到临时目录
    chat, calls = _good_chat()
    events = _events(120)  # 3 块
    out, stats = await translate.translate_events(
        chat, events, CTX, "chs", file_id=1
    )
    assert len(out) == 120
    assert out[0][2] == "译0" and out[119][2] == "译119"
    assert out[5][0] == events[5][0]  # 时间轴原样
    assert stats.total_blocks == 3 and stats.failed_blocks == 0
    assert stats.glossary == {"John": "约翰"}
    # 断点在翻译层保留（写盘成功后由 tasks 层清理——写盘失败时重跑可全量续传）
    ckpt = json.loads(
        next((tmp_path / "data").rglob("*.checkpoint.json")).read_text(encoding="utf-8")
    )
    assert len(ckpt["blocks"]) == 3


async def test_translate_block_retry_then_keep_original(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    async def chat(system: str, user: str) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        return "垃圾输出不是 JSON"

    events = _events(40)  # 1 块;全失败→保留原文,1/1 块失败率 100% > 熔断阈值
    with pytest.raises(translate.TranslationAborted):
        await translate.translate_events(chat, events, CTX, "chs", file_id=2)


async def test_translate_checkpoint_resume(tmp_path: Path, monkeypatch) -> None:
    """断点续传：第二次运行不重译已完成块（§3.6：已花的钱不能作废）。"""
    monkeypatch.chdir(tmp_path)
    events = _events(100)  # 2 块
    aborted = {"hit": False}

    async def chat_first(system: str, user: str) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        block = json.loads(user[user.index("[") :])
        if block[0]["i"] >= 50:  # 第二块：模拟中断
            aborted["hit"] = True
            raise RuntimeError("网络中断")
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    with pytest.raises(RuntimeError):
        await translate.translate_events(chat_first, events, CTX, "chs", file_id=3)
    assert aborted["hit"]

    block_calls = {"n": 0}

    async def chat_second(system: str, user: str) -> str:
        if "找出其中反复出现的人名" in user:
            raise AssertionError("术语表也该从断点恢复,不该再调")
        block_calls["n"] += 1
        block = json.loads(user[user.index("[") :])
        assert block[0]["i"] == 50  # 只翻第二块
        return json.dumps([{"i": e["i"], "t": f"译{e['i']}"} for e in block])

    out, stats = await translate.translate_events(
        chat_second, events, CTX, "chs", file_id=3
    )
    assert block_calls["n"] == 1
    assert len(out) == 100 and out[99][2] == "译99"


async def test_translate_untranslated_block_detected(tmp_path: Path, monkeypatch) -> None:
    """漏翻检测：整块返回英文原文 → 校验不过 → 重试耗尽保留原文计失败。"""
    monkeypatch.chdir(tmp_path)

    async def chat(system: str, user: str) -> str:
        if "找出其中反复出现的人名" in user:
            return "[]"
        block = json.loads(user[user.index("[") :])
        return json.dumps(
            [{"i": e["i"], "t": "This line was not translated at all"} for e in block]
        )

    events = _events(40)
    with pytest.raises(translate.TranslationAborted):  # 1/1 失败率触发熔断
        await translate.translate_events(chat, events, CTX, "chs", file_id=4)


# ---------------------------------------------------------------------------
# L1 同步度（§5.2：能量 VAD + 事件命中率;人造错位样本）
# ---------------------------------------------------------------------------


def _speechy_pcm(intervals_ms: list[tuple[int, int]], total_ms: int) -> np.ndarray:
    """人造音频：语音段为响亮噪声,其余近静音。"""
    rng = np.random.default_rng(seed=7)
    pcm = rng.normal(0, 0.002, int(sync.SAMPLE_RATE * total_ms / 1000)).astype(
        np.float32
    )
    for s, e in intervals_ms:
        a, b = int(s / 1000 * sync.SAMPLE_RATE), int(e / 1000 * sync.SAMPLE_RATE)
        pcm[a:b] = rng.normal(0, 0.3, b - a).astype(np.float32)
    return pcm


def test_sync_hit_rate_aligned_vs_shifted() -> None:
    # 稀疏语音（每 10 秒说 2 秒）：错位后事件才会真正落进静音带
    speech_at = [(1000 + i * 10_000, 3000 + i * 10_000) for i in range(6)]  # 60 秒窗
    pcm = _speechy_pcm(speech_at, 60_000)
    speech = sync.speech_intervals(pcm)

    aligned = sync.event_hit_rate(speech, [(s, e) for s, e in speech_at])
    assert aligned is not None and aligned > 0.9
    # 人造 +4 秒错位（落进静音带）:命中率显著下降（§10 验收:人造偏移样本低于阈值）
    shifted = sync.event_hit_rate(
        speech, [(s + 4000, e + 4000) for s, e in speech_at[:-1]]
    )
    assert shifted is not None and shifted < sync.SYNC_THRESHOLD


def test_sync_returns_none_when_undetectable() -> None:
    assert sync.event_hit_rate([], []) is None


async def test_sample_sync_score_degrades_without_ffmpeg(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sync, "_ffmpeg", lambda: None)
    score = await sync.sample_sync_score(tmp_path / "x.mkv", _events(100), 6000)
    assert score is None  # 无法检测=未知,不误判为不同步


# ---------------------------------------------------------------------------
# 加载层与分层守护
# ---------------------------------------------------------------------------


def test_parse_events_and_sdh_strip(tmp_path: Path) -> None:
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n[door slams] Hello\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n[music]\n"
    )
    events = extract.parse_events(srt, "test")
    assert len(events) == 2
    cleaned = extract.strip_sdh_markers(events)
    assert cleaned == [(1000, 2000, "Hello")]  # 纯音效行整条剔除


def test_gbk_external_sidecar_decoded(tmp_path: Path) -> None:
    sub = tmp_path / "Movie.chs.srt"
    sub.write_bytes("1\n00:00:01,000 --> 00:00:02,000\n中文字幕\n".encode("gbk"))
    text = extract._decode_text(sub.read_bytes(), str(sub))
    assert "中文字幕" in text


def test_subtitle_gen_never_imports_playback_layers() -> None:
    import ast

    pkg = (
        Path(__file__).resolve().parents[2]
        / "src/movieclaw_api/services/subtitle_gen"
    )
    for py in pkg.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(("movieclaw_jellyfin", "movieclaw_playback")), (
                    f"生产端文件 {py.name} import 了播放层 {name}"
                    "（subtitle-ai-translate.md §7 分层守护）"
                )


# ---------------------------------------------------------------------------
# L2 全局校准（§5.2：已知偏移/加速可被恢复）
# ---------------------------------------------------------------------------


def _talk_pattern(n: int = 120, period_ms: int = 9000, dur_ms: int = 2200):
    """不均匀的说话模式（带抖动,避免周期性自相关歧义）。"""
    rng = np.random.default_rng(3)
    out = []
    t = 1000
    for _ in range(n):
        out.append((t, t + dur_ms + int(rng.integers(-500, 500))))
        t += period_ms + int(rng.integers(-2500, 2500))
    return out


def test_calibration_recovers_pure_offset() -> None:
    reference = _talk_pattern()
    shifted = [(s - 3000, e - 3000) for s, e in reference]  # 字幕早了 3 秒
    cal = sync.estimate_calibration(reference, shifted)
    assert cal is not None
    assert cal.scale == 1.0
    assert abs(cal.offset_ms - 3000) <= 50  # 栅格粒度内
    assert cal.score > 0.5


def test_calibration_recovers_pal_speedup() -> None:
    reference = _talk_pattern()
    a, b = 25 / 23.976, -2000
    # 参考 t' = a*s + b → 字幕 s = (t' - b)/a
    subtitle = [(int((s - b) / a), int((e - b) / a)) for s, e in reference]
    cal = sync.estimate_calibration(reference, subtitle)
    assert cal is not None
    assert abs(cal.scale - a) < 1e-6  # 假设集里选中了 25/23.976
    assert abs(cal.offset_ms - b) <= 60


def test_calibration_apply_roundtrip() -> None:
    cal = sync.Calibration(scale=1.0, offset_ms=-1500, score=1.0)
    events = [(2000, 3000, "a"), (500, 900, "b")]
    out = sync.apply_calibration(events, cal)
    assert out[0] == (500, 1500, "a")
    assert out[1] == (0, 0, "b")  # 负值截 0,end 不早于 start


def test_calibration_none_on_empty() -> None:
    assert sync.estimate_calibration([], [(0, 1000)]) is None


# ---------------------------------------------------------------------------
# G2 自动生成护栏（额度/幂等判定）
# ---------------------------------------------------------------------------


def test_auto_quota_and_skip_logic() -> None:
    from movieclaw_api.services.subtitle_gen import auto

    auto._quota_day = None
    auto._quota_used = 0
    assert auto._quota_take(2) and auto._quota_take(2)
    assert not auto._quota_take(2)  # 第三次熔断

    row = _file(
        [{"codec": "subrip", "language": "eng"}],
        [{"filename": "Movie.chs.ai.srt", "format": "srt", "language": "chi"}],
    )
    assert auto._has_target_subtitle(row, "chi")  # 已有中文（AI 产物）→ 跳过
    assert auto._has_reference(row, "chs")
    bare = _file([{"codec": "hdmv_pgs_subtitle", "language": "eng"}], [])
    assert not auto._has_target_subtitle(bare, "chi")
    assert not auto._has_reference(bare, "chs")  # 只有图形轨 → 无参考


# ---------------------------------------------------------------------------
# 自查改进项（终检轮）：抽取缓存 / 语言 token 校验 / 校准文件名防护
# ---------------------------------------------------------------------------


def test_extract_cache_skips_when_fresh(tmp_path: Path, monkeypatch) -> None:
    """抽取产物比视频新且非空 → 直接复用（预检/发起/执行三连不重复通读容器）。"""
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")
    out = tmp_path / "cached.srt"
    out.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n")
    import os

    os.utime(out, ns=(video.stat().st_mtime_ns + 10**9,) * 2)
    monkeypatch.setattr(extract, "ffmpeg_available", lambda: False)  # 命中缓存就不该走到这
    extract._extract_embedded_sync(video, 0, out)  # 不抛 = 缓存生效

    # 视频比产物新（洗版替换）→ 缓存失效,走抽取路径（此处 ffmpeg 缺失即抛）
    os.utime(video, ns=(out.stat().st_mtime_ns + 10**9,) * 2)
    with pytest.raises(extract.SourceLoadError):
        extract._extract_embedded_sync(video, 0, out)


def test_language_token_validation() -> None:
    from movieclaw_api.exceptions import BadRequestException
    from movieclaw_api.services.subtitle_gen.tasks import ensure_language_token

    assert ensure_language_token(" CHS ") == "chs"
    for bad in ("../../etc", "a", "含中文", "x" * 20, "a/b"):
        with pytest.raises(BadRequestException):
            ensure_language_token(bad)


async def test_calibrate_rejects_path_traversal_filename() -> None:
    from movieclaw_api.exceptions import BadRequestException
    from movieclaw_api.services.subtitle_gen.tasks import calibrate_external_subtitle

    for bad in ("../x.srt", "a/b.srt", ".hidden.srt"):
        with pytest.raises(BadRequestException):
            await calibrate_external_subtitle(None, 1, bad)  # 文件名先于一切校验


async def test_compress_overruns_refine_pass() -> None:
    """超读速二次压缩（§3.3 浓缩优先闭环）：只回炉超标条,失败/变长不采纳。"""

    async def chat(system: str, user: str) -> str:
        batch = json.loads(user[user.index("[") :])
        out = []
        for item in batch:
            if item["i"] == 0:
                out.append({"i": 0, "t": "压缩后的短句"})
            else:
                out.append({"i": item["i"], "t": item["t"] + "反而更长了"})  # 不该被采纳
        return json.dumps(out, ensure_ascii=False)

    events = [
        (0, 1000, "这一条在一秒之内绝对读不完所以必然超标"),  # i=0 待压缩
        (2000, 3000, "这条也超标但模型给了更长的结果不能采纳啊"),  # i=1
        (4000, 10000, "正常条"),  # 不超标,不该进请求
    ]
    from movieclaw_api.services.subtitle_gen.validate import overrun_indices

    indices = overrun_indices(events)
    assert indices == [0, 1]
    out, compressed = await translate.compress_overruns(chat, events, indices, CTX, "chs")
    assert compressed == 1
    assert out[0] == (0, 1000, "压缩后的短句")
    assert out[1][2].startswith("这条也超标")  # 原译文保留
    assert out[2] == events[2]
