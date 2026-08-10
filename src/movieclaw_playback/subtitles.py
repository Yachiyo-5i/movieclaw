"""字幕播放领域服务（协议无关，docs/design/jellyfin-subtitle.md §3）。

三块能力，Jellyfin 兼容层与未来的网页播放器共用：

1. **轨引用中性模型**：``embedded:<k>`` / ``external:<文件名>`` / ``off``
   ——记忆与选择的通用语言。Jellyfin 的合成流序号（Index）是协议方言，
   **禁止出现在本层**，序号↔引用的换算收敛在协议层做；
2. **字幕内容服务**：读外挂字幕文件 → 编码归一（GBK/BIG5 → UTF-8）→
   按需格式转换（srt↔vtt）。乱码字幕比没字幕更劝退，编码归一是中文
   用户的核心价值，同格式直出也要过这一步（对齐真 Jellyfin 行为）；
3. **默认字幕轨选择**：外挂优先 → default 旗标 → 非 forced 完整字幕，
   forced 沉底（它是"只在说外语片段显示"的部分字幕，仅当只有它可选时
   才选中）——Jellyfin Default 模式在通配语言下的化简。

本层不 import movieclaw_jellyfin（架构守护测试有断言）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from movieclaw_db.models import LibraryFile

logger = logging.getLogger("movieclaw_playback.subtitles")

# -- 轨引用中性模型 ---------------------------------------------------------

EMBEDDED_PREFIX = "embedded:"
EXTERNAL_PREFIX = "external:"
SUBTITLE_OFF = "off"  # 用户明确关闭字幕（音轨无此值）


def embedded_track(index: int) -> str:
    """内封轨引用；index 是 audio_streams / subtitle_streams 的数组下标。"""
    return f"{EMBEDDED_PREFIX}{index}"


def external_track(filename: str) -> str:
    """外挂字幕引用；filename 是台账 external_subtitles 里的文件名。"""
    return f"{EXTERNAL_PREFIX}{filename}"


def parse_embedded_track(track: str) -> int | None:
    """内封轨引用 → 数组下标；不是内封引用或格式非法返回 None。"""
    if not track.startswith(EMBEDDED_PREFIX):
        return None
    try:
        index = int(track[len(EMBEDDED_PREFIX):])
    except ValueError:
        return None
    return index if index >= 0 else None


def parse_external_track(track: str) -> str | None:
    """外挂字幕引用 → 台账文件名；不是外挂引用返回 None。"""
    if not track.startswith(EXTERNAL_PREFIX):
        return None
    filename = track[len(EXTERNAL_PREFIX):]
    return filename or None


# -- 字幕内容服务 -----------------------------------------------------------

# 输出 MIME（对齐 Jellyfin MimeTypes 表：ass/ssa 是 text/x-ssa 不是 x-ass）
SUBTITLE_MIME = {
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
    "ass": "text/x-ssa",
    "ssa": "text/x-ssa",
}

# 可互转矩阵：v1 仅 srt↔vtt。ass/ssa 不做跨格式转换（样式会整体丢失，
# 对齐真 Jellyfin"无该格式转换器即失败"的语义）
_CONVERTIBLE = {("srt", "vtt"), ("vtt", "srt")}


class SubtitleServeError(Exception):
    """字幕无法服务（文件不在/格式不支持/解析失败）。message 面向日志，
    协议层统一按 404 应答。"""


@dataclass(frozen=True)
class SubtitleRef:
    """一次可服务的外挂字幕定位（resolve 的产物，serve 的输入）。"""

    path: Path
    format: str  # srt/ass/ssa/vtt（小写）


def resolve_external_subtitle(file: LibraryFile, track: str) -> SubtitleRef | None:
    """中性引用 → 可服务定位：校验台账存在并拼出绝对路径。

    只认外挂引用——内封轨在 DirectPlay 下由播放器自行解封装，服务端
    v1 不抽取（返回 None，协议层按 404 处理）。
    """
    filename = parse_external_track(track)
    if filename is None:
        return None
    for entry in file.external_subtitles or []:
        if entry.get("filename") == filename:
            fmt = str(entry.get("format") or "").lower()
            if fmt not in SUBTITLE_MIME:
                return None
            return SubtitleRef(
                path=Path(file.file_path).parent / filename, format=fmt
            )
    return None


_CHINESE_ENCODINGS = ("gb18030", "gbk", "gb2312", "big5", "big5hkscs")


def _decode_to_utf8_text(raw: bytes, path: Path) -> str:
    """编码归一：非 UTF-8/ASCII（GBK/GB18030/BIG5 常见）统一解码为文本。

    **中文优先偏置**（有意决策）：GBK 与 CP949（韩）/EUC-JP（日）在短样本
    下高度歧义，charset-normalizer 的首选常判成韩文编码、中文字幕全成乱码。
    候选列表里出现中文系编码时优先取用——本项目面向中文用户，中文字幕
    判对是硬需求；真正的韩/日字幕如今几乎都是 UTF-8，走不到这个分支。
    GBK/GB2312 按 gb18030 严格超集解码、big5 按 big5hkscs（向前兼容），
    全部失败退 errors="replace" 保底出字——出错的字幕也比 404 强，但要
    留下日志。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes

    results = from_bytes(raw)
    chosen = results.best()
    if chosen is not None and chosen.encoding.lower() not in _CHINESE_ENCODINGS:
        for match in results:
            if match.encoding.lower() in _CHINESE_ENCODINGS:
                chosen = match
                break
    encoding = (chosen.encoding if chosen is not None else "") or ""
    if encoding.lower() in ("gbk", "gb2312"):
        encoding = "gb18030"
    elif encoding.lower() == "big5":
        encoding = "big5hkscs"
    if encoding:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    logger.warning("字幕文件编码无法确定，已按 UTF-8 宽容解码（可能出现乱码字符）：%s", path)
    return raw.decode("utf-8", errors="replace")


def serve_subtitle(ref: SubtitleRef, out_format: str | None) -> tuple[bytes, str]:
    """读取 → 编码归一 → 按需转格式，返回 (UTF-8 字节, Content-Type)。

    ``out_format`` 为 None/空/与源相同 → 原格式直出（仍经编码归一）；
    srt↔vtt 互转；其余组合抛 SubtitleServeError（协议层 404）。
    不做磁盘缓存：文本字幕 <1MB、解析毫秒级，现读现转（有意简化，§3.2）。
    """
    fmt = (out_format or "").lower() or ref.format
    if fmt != ref.format and (ref.format, fmt) not in _CONVERTIBLE:
        raise SubtitleServeError(
            f"不支持的字幕格式转换：{ref.format} → {fmt}（{ref.path.name}）"
        )
    try:
        raw = ref.path.read_bytes()
    except OSError as exc:
        raise SubtitleServeError(f"字幕文件无法读取：{ref.path}（{exc}）") from exc

    text = _decode_to_utf8_text(raw, ref.path)
    if fmt != ref.format:
        import pysubs2

        try:
            subs = pysubs2.SSAFile.from_string(text)
            text = subs.to_string(fmt)
        except Exception as exc:  # noqa: BLE001 -- pysubs2 异常类型不穷举
            raise SubtitleServeError(
                f"字幕解析失败，无法转换为 {fmt}：{ref.path}（{exc}）"
            ) from exc
    return text.encode("utf-8"), SUBTITLE_MIME[fmt]


async def serve_subtitle_async(
    ref: SubtitleRef, out_format: str | None
) -> tuple[bytes, str]:
    """线程池版（文件读取与解析都是阻塞调用）。"""
    return await asyncio.to_thread(serve_subtitle, ref, out_format)


# -- 默认字幕轨选择 ---------------------------------------------------------


def pick_default_subtitle(file: LibraryFile) -> str | None:
    """默认字幕轨（Jellyfin Default 模式在通配语言下的化简）。

    候选排序：外挂 ↓ → default 旗标 ↓ → 非 forced ↓ → 稳定序；过滤条件
    ``外挂 || default || forced``，取首个；全不命中 → None（不自动开）。
    效果：装了外挂字幕就预选外挂（装了就是要看的），否则尊重内封
    default 旗标，仅当只有 forced 轨可选时才选 forced。
    """
    candidates: list[tuple[bool, bool, bool, int, str]] = []
    order = 0
    for k, raw in enumerate(file.subtitle_streams or []):
        track = embedded_track(k)
        candidates.append(
            (False, bool(raw.get("default")), bool(raw.get("forced")), order, track)
        )
        order += 1
    for entry in file.external_subtitles or []:
        filename = entry.get("filename")
        if not filename:
            continue
        track = external_track(filename)
        candidates.append(
            (True, bool(entry.get("default")), bool(entry.get("forced")), order, track)
        )
        order += 1

    eligible = [c for c in candidates if c[0] or c[1] or c[2]]
    if not eligible:
        return None
    eligible.sort(key=lambda c: (not c[0], not c[1], c[2], c[3]))
    return eligible[0][4]


def _track_exists(file: LibraryFile, track: str) -> bool:
    """中性引用当前是否仍有效（重扫/换文件后可能悬空）。"""
    k = parse_embedded_track(track)
    if k is not None:
        return k < len(file.subtitle_streams or [])
    filename = parse_external_track(track)
    if filename is not None:
        return any(
            e.get("filename") == filename for e in file.external_subtitles or []
        )
    return False


def resolve_default_subtitle(file: LibraryFile, remembered: str | None) -> str | None:
    """默认字幕轨的最终裁决：记忆优先（含 off），失效回落选择策略。"""
    if remembered == SUBTITLE_OFF:
        return SUBTITLE_OFF
    if remembered is not None and _track_exists(file, remembered):
        return remembered
    return pick_default_subtitle(file)


def resolve_default_audio(file: LibraryFile, remembered: str | None) -> int | None:
    """记忆的音轨 → audio_streams 下标；无记忆/失效返回 None
    （调用方维持现状算法：default 旗标优先）。"""
    if remembered is None:
        return None
    k = parse_embedded_track(remembered)
    if k is not None and k < len(file.audio_streams or []):
        return k
    return None
