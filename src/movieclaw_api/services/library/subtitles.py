"""外挂字幕的发现与命名解析（媒体库层，docs/design/jellyfin-subtitle.md §2）。

这里回答的是"库里有哪些外挂字幕"这个**库存事实**，与谁来播放无关：
扫描/监控在入库与秒过时调用发现函数落台账（library_file.external_subtitles），
控制台详情与播放领域层只读台账、不做文件系统调用。

匹配与解析规则对照 Jellyfin 的 MediaInfoResolver/ExternalPathParser 定稿
（两处有意差异见 parse_subtitle_tokens 注释）：

- 同目录、文件名以视频 stem 为前缀、紧跟 ``.`` 分隔或恰好等于 stem；
- stem 之后的 token 从右往左判定：default/forced/sdh 旗标 → 语言映射 →
  其余拼回 title；
- 不 ffprobe 字幕文件本体（信任扩展名，SUBTITLE_EXTS 已排除歧义格式）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from movieclaw_api.services.library.layout import SUBTITLE_EXTS

logger = logging.getLogger("movieclaw_api.library_subtitles")

# 语言 token → ISO 639-2/B 三字码。比 Jellyfin 多收中文命名别名（它的
# FindLanguageInfo 只认 ISO 码与英文名，认不了字幕组的命名习惯）。
# 刻意不收 "hi"：印地语与听障旗标撞名，Jellyfin 为此写了特判，我们直接
# 规避（听障旗标见 _SDH_FLAGS，印地语用 hin/hindi 仍可命中）。
LANGUAGE_TOKENS: dict[str, str] = {
    # 中文（含字幕组常用中文命名）
    "chs": "chi", "cht": "chi", "zh": "chi", "zh-cn": "chi", "zh-tw": "chi",
    "zh-hans": "chi", "zh-hant": "chi", "chi": "chi", "zho": "chi",
    "简中": "chi", "简体": "chi", "繁中": "chi", "繁体": "chi",
    "中字": "chi", "中英": "chi", "双语": "chi", "简繁": "chi",
    # 常见外语
    "en": "eng", "eng": "eng", "english": "eng",
    "ja": "jpn", "jp": "jpn", "jpn": "jpn",
    "ko": "kor", "kor": "kor",
    "fr": "fre", "fre": "fre", "fra": "fre",
    "de": "ger", "ger": "ger", "deu": "ger",
    "es": "spa", "spa": "spa",
    "ru": "rus", "rus": "rus",
    "it": "ita", "ita": "ita",
    "pt": "por", "por": "por",
    "th": "tha", "tha": "tha",
    "hin": "hin", "hindi": "hin",
    "ar": "ara", "ara": "ara",
    "yue": "yue", "粤语": "yue", "粤": "yue",
}

_DEFAULT_FLAGS = {"default"}
_FORCED_FLAGS = {"forced", "foreign"}
_SDH_FLAGS = {"cc", "hi", "sdh"}


def parse_subtitle_tokens(extra: str) -> dict[str, Any]:
    """解析 stem 之后的 token 段（不含扩展名），返回语言/旗标/标题。

    从右往左逐段判定（对照 ExternalPathParser 的扫描方向）；与 Jellyfin
    的有意差异：旗标用**整段相等**而非 Contains——中文命名里巧合子串
    误判的风险大于宽松匹配的收益。全部判定大小写不敏感。
    """
    result: dict[str, Any] = {
        "language": None,
        "title": None,
        "default": False,
        "forced": False,
        "sdh": False,
    }
    tokens = [t for t in extra.split(".") if t]
    title_parts: list[str] = []
    for token in reversed(tokens):
        lowered = token.lower()
        if lowered in _DEFAULT_FLAGS:
            result["default"] = True
            continue
        if lowered in _FORCED_FLAGS:
            result["forced"] = True
            continue
        if lowered in _SDH_FLAGS:
            result["sdh"] = True
            continue
        lang = LANGUAGE_TOKENS.get(lowered)
        # 多个语言 token 时首个命中生效（从右往左，越靠右越具体）
        if lang is not None and result["language"] is None:
            result["language"] = lang
            continue
        title_parts.append(token)
    if title_parts:
        # 判定从右往左，title 要还原原始顺序
        result["title"] = ".".join(reversed(title_parts))
    return result


def match_subtitle_filename(video_stem: str, filename: str) -> str | None:
    """文件名是否是该视频的外挂字幕；是则返回 stem 之后的 token 段。

    规则（对照 MediaInfoResolver.GetExternalFiles）：去扩展名后以视频
    stem 为前缀（大小写不敏感），且要么恰好等于 stem、要么紧跟 ``.``。
    返回 None = 不匹配；返回 "" = 无 token 的裸字幕（Movie.srt）。
    """
    path = Path(filename)
    if path.suffix.lower() not in SUBTITLE_EXTS:
        return None
    stem = path.stem
    if len(stem) < len(video_stem):
        return None
    if stem[: len(video_stem)].lower() != video_stem.lower():
        return None
    rest = stem[len(video_stem):]
    if rest == "":
        return ""
    if not rest.startswith("."):
        return None
    return rest[1:]


def _entry_for(directory: Path, filename: str, extra: str) -> dict[str, Any] | None:
    """单个匹配文件 → 台账元素；stat 失败（竞态删除）返回 None 剔除。"""
    try:
        st = (directory / filename).stat()
    except OSError:
        return None
    parsed = parse_subtitle_tokens(extra)
    return {
        "filename": filename,
        "format": Path(filename).suffix.lstrip(".").lower(),
        "language": parsed["language"],
        "title": parsed["title"],
        "default": parsed["default"],
        "forced": parsed["forced"],
        "sdh": parsed["sdh"],
        "size_bytes": st.st_size,
        "file_mtime_ns": st.st_mtime_ns,
    }


def discover_external_subtitles(
    video_path: Path, dir_names: list[str] | None = None
) -> list[dict[str, Any]]:
    """发现视频同目录的外挂字幕，返回台账清单（文件名序，决定性）。

    ``dir_names``：调用方已持有的目录文件名列表（扫描遍历时截获），传入
    则匹配零额外目录 IO；缺省自行 iterdir（watchdog 单行重发现场景）。
    只对匹配命中的少数字幕文件 stat（size/mtime 落台账供服务期新鲜度
    探测）——不触碰视频文件本体。
    """
    if dir_names is None:
        try:
            dir_names = [p.name for p in video_path.parent.iterdir() if p.is_file()]
        except OSError:
            logger.warning("外挂字幕发现失败（目录不可读）：%s", video_path.parent)
            return []
    stem = video_path.stem
    entries: list[dict[str, Any]] = []
    for name in sorted(dir_names):
        extra = match_subtitle_filename(stem, name)
        if extra is None:
            continue
        entry = _entry_for(video_path.parent, name, extra)
        if entry is not None:
            entries.append(entry)
    return entries


async def discover_external_subtitles_async(
    video_path: Path, dir_names: list[str] | None = None
) -> list[dict[str, Any]]:
    """线程池版（扫描主循环用：stat/iterdir 在网络挂载上可能阻塞）。"""
    return await asyncio.to_thread(discover_external_subtitles, video_path, dir_names)
