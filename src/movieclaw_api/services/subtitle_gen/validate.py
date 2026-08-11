"""译文机检：Netflix 简中规范的可机检部分（subtitle-ai-translate.md §3.3/§3.5）。

折行/标点/数字是规则活，不劳模型；CPS 超标率与术语一致性只统计入报告
（时长是参考字幕定的，翻译只能靠压缩表达，超标率是"浓缩力"的量化）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from movieclaw_api.services.subtitle_gen.extract import SubEvent

LINE_LIMIT = 16  # Netflix 简中：每行 ≤16 字
CPS_LIMIT = 9.0  # 成人内容 ≤9 字/秒

# 折行断点偏好：标点/空格之后断，读起来才不别扭
_BREAK_CHARS = " ，。！？…、；：,!?"
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def clean_punctuation(text: str) -> str:
    """简中字幕标点铁律的机械清理（§3.3）：

    - 句尾句号/逗号删除（含半角），省略号/问叹号保留；
    - 句中全角逗号/句号 → 空格（Netflix：停顿用空格）；
    - 全角数字 → 半角；禁 !?/?? 组合收敛为单个。
    """
    text = text.translate(_FULLWIDTH_DIGITS)
    text = re.sub(r"[！!？?]{2,}", lambda m: m.group(0)[0], text)
    text = re.sub(r"[，。,]+(?=\s|$)", "", text)  # 行尾（含折行前）
    text = re.sub(r"[，。](?=.)", " ", text)
    return re.sub(r" {2,}", " ", text).strip()


def _visible_len(text: str) -> int:
    return len(text.replace(" ", "").replace("\n", ""))


def fold_line(text: str, limit: int = LINE_LIMIT) -> str:
    """超过行长的单行文本折成至多两行（断点找标点/空格，找不到取中点）。

    两行仍超长不截断（宁超不丢，§3.4 铁律），由 CPS/超长统计呈现问题。
    """
    text = text.strip()
    if len(text) <= limit or "\n" in text:
        return text
    # 在 [limit//2, limit] 里找最靠右的断点字符；没有就硬切中点附近
    cut = -1
    for i in range(min(limit, len(text) - 1), max(limit // 2 - 1, 0), -1):
        if text[i - 1] in _BREAK_CHARS:
            cut = i
            break
    if cut <= 0:
        cut = min(limit, (len(text) + 1) // 2)
    first, rest = text[:cut].rstrip(), text[cut:].lstrip()
    if not rest:
        return first
    return f"{first}\n{rest}"


@dataclass
class QualityReport:
    """机检结论（任务报告呈现，§3.5）。"""

    event_count: int = 0
    cps_overrun: int = 0  # 超 9 字/秒的事件数
    overlong: int = 0  # 折行后仍超两行容量（>32 字）的事件数
    kept_original: int = 0  # 翻译失败保留原文的事件数
    glossary_usage: dict[str, int] = field(default_factory=dict)  # 译名 → 出现次数

    @property
    def cps_overrun_rate(self) -> float:
        return self.cps_overrun / self.event_count if self.event_count else 0.0


def finalize_events(
    events: list[SubEvent], glossary: dict[str, str] | None = None
) -> tuple[list[SubEvent], QualityReport]:
    """标点清理 + 折行 + 统计，产出可落盘事件与机检报告。"""
    report = QualityReport(event_count=len(events))
    out: list[SubEvent] = []
    for start, end, text in events:
        cleaned = fold_line(clean_punctuation(text))
        duration_s = max((end - start) / 1000, 0.001)
        if _visible_len(cleaned) / duration_s > CPS_LIMIT:
            report.cps_overrun += 1
        if _visible_len(cleaned) > LINE_LIMIT * 2:
            report.overlong += 1
        out.append((start, end, cleaned))
    for dst in (glossary or {}).values():
        if not dst:
            continue
        report.glossary_usage[dst] = sum(t.count(dst) for _, _, t in out)
    return out, report


_CJK = re.compile(r"[一-鿿]")


def looks_translated(text: str, target_language: str) -> bool:
    """漏翻检测（§3.5 机检）：目标是中文时，译文行必须含 CJK 字符。

    纯数字/纯标点/专有名词行放过（无 ASCII 字母才要求 CJK）。
    """
    if not target_language.startswith("ch") and target_language != "chi":
        return True  # 非中文目标 v1 不做语言判定
    if _CJK.search(text):
        return True
    return not re.search(r"[A-Za-z]{4,}", text)  # 长英文串且无中文 = 疑似漏翻
