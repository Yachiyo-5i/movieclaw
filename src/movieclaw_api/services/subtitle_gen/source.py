"""参考字幕选源：规则打分，不用 AI（subtitle-ai-translate.md §2）。

决定参考质量的因素全部是结构化事实——排除项（forced/目标语言/图形轨）、
语言优先级（原语言 > 英语 > 其他）、完整度（加载后评估）、形态平分决胜
（内封优先，v2.1 评审修正：内封多为发行方官方字幕）。打分明细随任务
日志输出，用户要能看懂"为什么选了这条"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from movieclaw_api.services.library.subtitles import LANGUAGE_TOKENS
from movieclaw_api.services.subtitle_gen import pgs
from movieclaw_db.models import LibraryFile

# 内封文本字幕 codec（可抽取为 srt 做翻译源）；图形轨（PGS/VobSub）排除
_TEXT_CODECS = {"srt", "subrip", "ass", "ssa", "vtt", "webvtt", "mov_text", "text"}


def normalize_language(raw: str | None) -> str | None:
    """任意语言写法 → ISO 639-2/B 三字码（复用 A 层 token 表）；认不出原样返回。"""
    if not raw:
        return None
    return LANGUAGE_TOKENS.get(raw.lower(), raw.lower())


@dataclass(frozen=True)
class SourceCandidate:
    """一个可做翻译参考的字幕轨（内封或外挂）。"""

    kind: str  # "embedded" | "external"
    key: str  # embedded: 字幕轨数组下标的字符串；external: 台账文件名
    language: str | None  # ISO 639-2/B
    forced: bool
    sdh: bool
    format: str | None  # 外挂扩展名 / 内封 codec（小写）
    provenance: str = "original"  # original / pgs_ocr / ai / ai_bilingual


@dataclass
class RankedCandidate:
    candidate: SourceCandidate
    excluded: str | None = None  # 非空 = 被排除的中文原因
    exclusion_code: str | None = None  # 稳定原因码，供预检生成可执行的用户提示
    reasons: list[str] = field(default_factory=list)  # 打分明细（任务日志）
    rank_key: tuple = ()  # 越大越优


def _language_rank(lang: str | None, original: str | None) -> int:
    """原语言=2（第一手台词），英语=1（通用中间语言），其余=0。"""
    if lang is None:
        return 0
    if original is not None and lang == original:
        return 2
    return 1 if lang == "eng" else 0


def _external_provenance(entry: dict) -> str:
    """从规范 title/文件名识别字幕世代，避免把 AI 成品继续当默认翻译源。"""
    marker = f"{entry.get('title') or ''}.{entry.get('filename') or ''}".lower()
    if "ai-bilingual-" in marker:
        return "ai_bilingual"
    if ".ai-" in marker or marker.startswith("ai-") or ".ai." in marker:
        return "ai"
    if "pgs-ocr" in marker:
        return "pgs_ocr"
    return "original"


def rank_candidates(
    file: LibraryFile,
    *,
    original_language: str | None,
    target_language: str,
) -> list[RankedCandidate]:
    """元数据级排序（完整度/同步度在加载后另行评估，tasks 按序逐个尝试）。

    排序键（越大越优）：语言优先级 → 非 sdh（听障标记要清理，同分让路）
    → 来源世代（发行原文 > OCR 文本 > AI 单语 > AI 双语）→ 内封优先。
    """
    original = normalize_language(original_language)
    target = normalize_language(target_language)
    ranked: list[RankedCandidate] = []

    for k, raw in enumerate(file.subtitle_streams or []):
        codec = str(raw.get("codec") or "").lower()
        cand = SourceCandidate(
            kind="embedded",
            key=str(k),
            language=normalize_language(raw.get("language")),
            forced=bool(raw.get("forced")),
            sdh=False,
            format=codec,
            provenance="original",
        )
        ranked.append(_rank_one(cand, original, target, is_text=codec in _TEXT_CODECS))

    for entry in file.external_subtitles or []:
        filename = entry.get("filename")
        if not filename:
            continue
        cand = SourceCandidate(
            kind="external",
            key=str(filename),
            language=normalize_language(entry.get("language")),
            forced=bool(entry.get("forced")),
            sdh=bool(entry.get("sdh")),
            format=str(entry.get("format") or "").lower(),
            provenance=_external_provenance(entry),
        )
        # 外挂台账只收文本格式（SUBTITLE_EXTS 保证），恒可用
        ranked.append(_rank_one(cand, original, target, is_text=True))

    # 稳定排序：rank_key 降序，同分保持进入顺序（内封在前=决胜时内封优先）
    order = {c.candidate.key + c.candidate.kind: i for i, c in enumerate(ranked)}
    ranked.sort(
        key=lambda c: (
            [-x for x in c.rank_key],
            order[c.candidate.key + c.candidate.kind],
        )
    )
    return ranked


def _rank_one(
    cand: SourceCandidate, original: str | None, target: str | None, *, is_text: bool
) -> RankedCandidate:
    r = RankedCandidate(candidate=cand)
    if cand.forced:
        r.exclusion_code = "forced"
        r.excluded = "forced 轨是外语片段部分字幕，对白严重缺失，不能当翻译源"
        return r
    if target is not None and cand.language == target:
        r.exclusion_code = "target_language"
        r.excluded = "已是目标语言，无需翻译"
        return r

    lang_rank = _language_rank(cand.language, original)
    lang_label = cand.language or "未知语言"
    if lang_rank == 2:
        r.reasons.append(f"语言 {lang_label} = 影片原语言（第一手台词，最优）")
    elif lang_rank == 1:
        r.reasons.append(f"语言 {lang_label} = 英语（通用中间语言，次优）")
    else:
        r.reasons.append(f"语言 {lang_label}（非原语言/英语，二次转译误差叠加）")
    if cand.sdh:
        r.reasons.append("听障字幕（翻译前清理音效标记，同分让路）")
    embedded = cand.kind == "embedded"
    provenance_rank = {
        "original": 3,
        "pgs_ocr": 2,
        "ai": 1,
        "ai_bilingual": 0,
    }.get(cand.provenance, 1)
    if embedded:
        r.reasons.append("内封轨（多为发行方官方字幕，平分决胜优先）")
        provenance_rank = 4
    elif cand.provenance == "pgs_ocr":
        r.reasons.append("由原始图片字幕识别得到（优先于二次 AI 成品）")
    elif cand.provenance == "ai":
        r.reasons.append("AI 生成字幕（二次转译可能叠加误差）")
    elif cand.provenance == "ai_bilingual":
        r.reasons.append("AI 双语成品（默认最后使用，避免重复翻译）")
    else:
        r.reasons.append("普通外挂字幕（未检测到二次生成标记）")
    r.rank_key = (
        lang_rank,
        0 if cand.sdh else 1,
        provenance_rank,
        1 if embedded else 0,
    )
    if not is_text:
        r.exclusion_code = "pgs" if pgs.is_pgs_codec(cand.format) else "graphic"
        if r.exclusion_code == "pgs":
            r.excluded = "PGS 图形字幕需先经 OCR 转为 SRT，不能直接做翻译源"
        else:
            r.excluded = f"图形字幕轨（{cand.format}）暂不支持自动 OCR"
    return r


@dataclass(frozen=True)
class Assessment:
    """候选加载后的完整度评估（§2.3：条数 + 覆盖时长比）。"""

    event_count: int
    coverage: float  # 首末事件跨度 / 影片时长；无时长数据 = 1.0（不惩罚）
    ok: bool
    reason: str


# 完整度阈值：条数低于此值多半是残缺轨/纯标题轨；覆盖率低于此值疑似片段
_MIN_EVENTS = 50
_MIN_COVERAGE = 0.5


def assess_events(events: list[tuple[int, int, str]], runtime_seconds: int | None) -> Assessment:
    """events 为 (start_ms, end_ms, text) 序列（加载层产物）。"""
    count = len(events)
    if count == 0:
        return Assessment(0, 0.0, False, "解析后没有任何对白事件")
    if runtime_seconds and runtime_seconds > 0:
        span_ms = max(e[1] for e in events) - min(e[0] for e in events)
        coverage = min(1.0, span_ms / (runtime_seconds * 1000))
    else:
        coverage = 1.0
    if count < _MIN_EVENTS:
        return Assessment(count, coverage, False, f"对白仅 {count} 条，疑似残缺轨")
    if coverage < _MIN_COVERAGE:
        return Assessment(count, coverage, False, f"时间覆盖率仅 {coverage:.0%}，疑似片段字幕")
    return Assessment(count, coverage, True, f"{count} 条对白，覆盖率 {coverage:.0%}")
