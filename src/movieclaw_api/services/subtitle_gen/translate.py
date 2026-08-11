"""LLM 翻译管线：分块 + 术语表两遍法 + 结构化校验 + 断点续传
（subtitle-ai-translate.md §3）。

时间轴完全不经过模型：请求只给带序号的原文清单，译文按同序号 JSON 返回，
时间轴由管线原样保留——从机制上杜绝时间漂移。

本模块与 LLM 接入解耦：入参是 ``chat(system, user) -> str`` 异步函数，
tasks 层用 movieclaw_llm 包装真实现，测试注入假实现。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from movieclaw_api.services.subtitle_gen.extract import SubEvent, cache_dir
from movieclaw_api.services.subtitle_gen.validate import looks_translated

logger = logging.getLogger("movieclaw_api.subtitle_gen")

ChatFn = Callable[[str, str], Awaitable[str]]

BLOCK_SIZE = 50  # 每块事件数（§3.1）
_CONTEXT_LINES = 3  # 滚动上下文条数
_BLOCK_RETRIES = 2  # 结构不符/漏翻的重试次数
FAILED_BLOCK_ABORT_RATE = 0.2  # 失败块超 20% 中止任务（§3.6 熔断）

_LANG_NAMES = {"chs": "简体中文", "chi": "简体中文", "cht": "繁体中文"}


@dataclass(frozen=True)
class FilmContext:
    """风格 prompt 的影片素材（媒体库元数据现成，§3.1）。"""

    title: str
    year: int | None
    genres: list[str]
    overview: str | None


@dataclass
class TranslationStats:
    total_blocks: int = 0
    failed_blocks: int = 0
    kept_original: int = 0
    glossary: dict[str, str] = field(default_factory=dict)


class TranslationAborted(Exception):
    """失败块超熔断阈值 / 用户取消（中文信息面向任务日志）。"""


def _film_intro(ctx: FilmContext) -> str:
    parts = [f"《{ctx.title}》"]
    if ctx.year:
        parts.append(f"{ctx.year} 年")
    if ctx.genres:
        parts.append("/".join(ctx.genres[:3]))
    intro = "、".join(parts)
    if ctx.overview:
        intro += f"。剧情简介：{ctx.overview[:200]}"
    return intro


def system_prompt(ctx: FilmContext, glossary: dict[str, str], target_language: str) -> str:
    """§3.4 的七条铁律（Netflix 简中 TTSG 与 FAR 模型调研蒸馏）。"""
    lang = _LANG_NAMES.get(target_language, target_language)
    lines = [
        f"你是资深影视字幕翻译，正在为影片翻译{lang}字幕。影片：{_film_intro(ctx)}",
        "",
        "翻译铁律：",
        "1. 语气忠实：语域、正式度、阶层感、年代感对齐原文；粗口保持同等强度，"
        "不消毒不审查；角色语气差异要能听出是谁在说话",
        "2. 口语自然：译文必须“说得出口”，禁书面翻译腔；称谓全片一致",
        "3. 浓缩优先：每行尽量不超过 16 字、单条不超过 32 字；超长时压缩表达"
        "而非直译堆字——删冗余语气词、保核心语义；绝不截断句意",
        "4. 标点：句尾不用句号和逗号（直接结束）；停顿和中断用省略号；"
        "问号感叹号保留；禁斜体标记",
        "5. 数字：一到十用汉字，其余用半角阿拉伯数字",
        "6. 文化专有项：给观众可直接理解的对应说法，禁加括号注释或译者注",
        "7. 结构：逐条对应翻译，禁合并、拆分、增删条目；只输出译文不带原文；"
        "[音效] 类标记按原格式保留并翻译内容",
    ]
    if glossary:
        pairs = "；".join(f"{k}→{v}" for k, v in list(glossary.items())[:40])
        lines.append(f"术语表（人名地名译名必须全片一致）：{pairs}")
    lines.append(
        '输出格式：JSON 数组，每个元素形如 {"i": 序号, "t": "译文"}，'
        "序号与输入一一对应，不输出任何其他内容"
    )
    return "\n".join(lines)


_GLOSSARY_PROMPT = (
    "下面是影片对白抽样。找出其中反复出现的人名、地名、组织与专有名词，"
    "给出适合该影片的统一{lang}译名。只输出 JSON 数组，元素形如 "
    '{{"src": "原文", "dst": "译名"}}，最多 30 条，没有则输出 []'
)


def _parse_json_block(raw: str):
    """剥掉 markdown 代码栅栏后解析 JSON；失败抛 ValueError。"""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


async def build_glossary(
    chat: ChatFn, events: list[SubEvent], ctx: FilmContext, target_language: str
) -> dict[str, str]:
    """第一遍：抽样建术语表（失败不阻断任务——空表照样翻，§3.1）。"""
    step = max(1, len(events) // 300)
    sample = "\n".join(t for _, _, t in events[::step][:300])
    lang = _LANG_NAMES.get(target_language, target_language)
    try:
        raw = await chat(
            f"你是影视字幕翻译助理。影片：{_film_intro(ctx)}",
            _GLOSSARY_PROMPT.format(lang=lang) + "\n\n" + sample,
        )
        entries = _parse_json_block(raw)
        glossary = {
            str(e["src"]): str(e["dst"])
            for e in entries
            if isinstance(e, dict) and e.get("src") and e.get("dst")
        }
        logger.info("术语表建立完成：%d 条", len(glossary))
        return glossary
    except Exception as exc:  # noqa: BLE001 -- 术语表失败不值得废整个任务
        logger.warning("术语表抽取失败（继续无术语表翻译）：%s", exc)
        return {}


# ---------------------------------------------------------------------------
# 断点暂存（§3.6：LLM 调用是真金白银，已花的钱不能作废）
# ---------------------------------------------------------------------------


class Checkpoint:
    """逐块追加的译文暂存。指纹 = 源文本 + 目标语言，源变了暂存作废。"""

    def __init__(self, file_id: int, target_language: str, fingerprint: str) -> None:
        self.path = cache_dir() / f"{file_id}.{target_language}.checkpoint.json"
        self.fingerprint = fingerprint
        self.blocks: dict[int, list[str]] = {}
        self.glossary: dict[str, str] = {}

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("fingerprint") != self.fingerprint:
            return  # 源字幕/目标语言变了：旧暂存不可续
        self.blocks = {int(k): v for k, v in data.get("blocks", {}).items()}
        self.glossary = dict(data.get("glossary", {}))
        if self.blocks:
            logger.info("发现字幕翻译断点，续传 %d 个已完成块", len(self.blocks))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "fingerprint": self.fingerprint,
                    "glossary": self.glossary,
                    "blocks": {str(k): v for k, v in self.blocks.items()},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def discard(self) -> None:
        import contextlib

        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


def source_fingerprint(events: list[SubEvent], target_language: str) -> str:
    payload = target_language + "\x00" + "\x1f".join(t for _, _, t in events)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 主翻译循环
# ---------------------------------------------------------------------------


def _block_user_prompt(block: list[tuple[int, str]], context: list[tuple[str, str]]) -> str:
    lines = []
    if context:
        ctx_lines = "\n".join(f"{src} → {dst}" for src, dst in context)
        lines.append(f"上文衔接（前一段的原文与译文，保持连贯）：\n{ctx_lines}\n")
    payload = json.dumps(
        [{"i": i, "s": text} for i, text in block], ensure_ascii=False
    )
    lines.append(f"翻译下列对白：\n{payload}")
    return "\n".join(lines)


def _validate_block(
    raw: str, block: list[tuple[int, str]], target_language: str
) -> list[str]:
    """结构校验（§3.2/§3.5）：条数、序号、漏翻。不符抛 ValueError 触发重试。"""
    parsed = _parse_json_block(raw)
    if not isinstance(parsed, list) or len(parsed) != len(block):
        got = len(parsed) if isinstance(parsed, list) else "非数组"
        raise ValueError(f"返回 {got} 条，应为 {len(block)} 条")
    by_index = {}
    for item in parsed:
        if not isinstance(item, dict) or "i" not in item or "t" not in item:
            raise ValueError("元素缺少 i/t 字段")
        by_index[int(item["i"])] = str(item["t"]).strip()
    out: list[str] = []
    untranslated = 0
    for i, src in block:
        if i not in by_index:
            raise ValueError(f"缺少序号 {i} 的译文")
        text = by_index[i] or src
        if not looks_translated(text, target_language):
            untranslated += 1
        out.append(text)
    if untranslated > len(block) // 2:
        raise ValueError(f"{untranslated}/{len(block)} 条疑似未翻译（仍是源语言）")
    return out


async def translate_events(
    chat: ChatFn,
    events: list[SubEvent],
    ctx: FilmContext,
    target_language: str,
    *,
    file_id: int,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[list[SubEvent], TranslationStats]:
    """整片翻译：术语表 → 分块翻译（带断点/重试/熔断）→ 保时间轴回填。"""
    stats = TranslationStats()
    checkpoint = Checkpoint(
        file_id, target_language, source_fingerprint(events, target_language)
    )
    checkpoint.load()

    if checkpoint.glossary:
        glossary = checkpoint.glossary
    else:
        glossary = await build_glossary(chat, events, ctx, target_language)
        checkpoint.glossary = glossary
        checkpoint.save()
    stats.glossary = glossary
    system = system_prompt(ctx, glossary, target_language)

    numbered = list(enumerate((t for _, _, t in events), start=0))
    blocks = [numbered[i : i + BLOCK_SIZE] for i in range(0, len(numbered), BLOCK_SIZE)]
    stats.total_blocks = len(blocks)
    translated: dict[int, list[str]] = dict(checkpoint.blocks)

    for bi, block in enumerate(blocks):
        if bi in translated:
            continue
        if cancelled is not None and cancelled():
            raise TranslationAborted("任务被用户取消（已完成块已暂存，可续传）")
        context: list[tuple[str, str]] = []
        if bi > 0 and (bi - 1) in translated:
            prev_src = [t for _, t in blocks[bi - 1][-_CONTEXT_LINES:]]
            prev_dst = translated[bi - 1][-_CONTEXT_LINES:]
            context = list(zip(prev_src, prev_dst, strict=False))
        user = _block_user_prompt(block, context)

        result: list[str] | None = None
        for attempt in range(_BLOCK_RETRIES + 1):
            try:
                raw = await chat(system, user)
                result = _validate_block(raw, block, target_language)
                break
            except (ValueError, json.JSONDecodeError) as exc:
                logger.warning(
                    "字幕翻译第 %d 块校验失败（第 %d 次尝试）：%s", bi + 1, attempt + 1, exc
                )
        if result is None:
            # 失败块保留原文——出错的块保留原文也比整任务失败强（§3.2）
            result = [t for _, t in block]
            stats.failed_blocks += 1
            stats.kept_original += len(block)
            if stats.failed_blocks / stats.total_blocks > FAILED_BLOCK_ABORT_RATE:
                raise TranslationAborted(
                    f"翻译失败块已达 {stats.failed_blocks}/{stats.total_blocks}，"
                    "超过熔断阈值，任务中止（已完成块已暂存，修复模型配置后可续传）"
                )
        translated[bi] = result
        checkpoint.blocks[bi] = result
        checkpoint.save()
        if progress is not None:
            progress(len(translated), stats.total_blocks)

    texts = [t for bi in range(len(blocks)) for t in translated[bi]]
    out = [(s, e, texts[i]) for i, (s, e, _) in enumerate(events)]
    return out, stats


def write_srt(events: list[SubEvent], path: Path) -> None:
    """UTF-8 srt 落盘（v1 恒 srt，§3.2）。"""
    import pysubs2

    subs = pysubs2.SSAFile()
    for start, end, text in events:
        subs.append(pysubs2.SSAEvent(start=start, end=end, text=text.replace("\n", "\\N")))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(subs.to_string("srt"), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# CPS 超标条目的二次压缩（§3.3"浓缩优先"的闭环，终检轮补）
# ---------------------------------------------------------------------------

_COMPRESS_BATCH = 30
_COMPRESS_PROMPT = (
    "下列译文在字幕显示时长内读不完，请在**不丢核心语义**的前提下压缩改写：\n"
    "每条给出目标字数上限，删冗余语气词、化长句为短句；禁增删条目、禁截断句意。\n"
    '只输出 JSON 数组，元素形如 {{"i": 序号, "t": "压缩后译文"}}：\n{payload}'
)


async def compress_overruns(
    chat: ChatFn,
    events: list[SubEvent],
    indices: list[int],
    ctx: FilmContext,
    target_language: str,
) -> tuple[list[SubEvent], int]:
    """对超读速事件做限字数压缩重写；失败保留原译文。返回 (事件, 成功数)。

    上限字数按事件时长 × 9 字/秒（Netflix 简中成人标准）计算；单条低于
    4 字不再压（短句压无可压）。
    """
    todo = []
    for i in indices:
        start, end, text = events[i]
        limit = max(4, int((end - start) / 1000 * 9))
        if len(text.replace("\n", "").replace(" ", "")) > limit:
            todo.append((i, text.replace("\n", " "), limit))
    if not todo:
        return events, 0

    out = list(events)
    compressed = 0
    system = system_prompt(ctx, {}, target_language)
    for batch_start in range(0, len(todo), _COMPRESS_BATCH):
        batch = todo[batch_start : batch_start + _COMPRESS_BATCH]
        payload = json.dumps(
            [{"i": i, "t": t, "max_chars": limit} for i, t, limit in batch],
            ensure_ascii=False,
        )
        try:
            raw = await chat(system, _COMPRESS_PROMPT.format(payload=payload))
            parsed = _parse_json_block(raw)
            by_index = {
                int(item["i"]): str(item["t"]).strip()
                for item in parsed
                if isinstance(item, dict) and "i" in item and "t" in item
            }
        except Exception as exc:  # noqa: BLE001 -- 压缩是增益项,失败保留原译文
            logger.warning("超读速条目压缩失败（保留原译文）：%s", exc)
            continue
        for i, original, _limit in batch:
            text = by_index.get(i, "").strip()
            # 压缩结果必须真的更短且仍是目标语言,否则不采纳
            if text and len(text) < len(original) and looks_translated(text, target_language):
                start, end, _ = out[i]
                out[i] = (start, end, text)
                compressed += 1
    return out, compressed
