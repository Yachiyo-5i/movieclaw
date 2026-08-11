"""字幕/音轨声明的微解析器：模型圈 span，这里把 span 文本解析成结构化事实。

设计（docs/design/subtitle-audio-ner.md「总体设计」）：NER 模型只负责判断
"这段文字在谈论字幕/音轨"（含否定式与泛称），本模块负责全部**语义与政策**：

- 闭词表语言映射（BCP 47：简→zh-Hans、国语→cmn、粤语→yue …）；
- 载体解析（内封→embedded、内嵌/硬字幕→hardcoded、外挂→external）；
- 否定裁决：**裸否定**（"无字幕"、"字幕：无"、NO SUBS）全局清空字幕语言，
  跨段生效；**载体限定否定**（"无内嵌字幕"）只压制该载体，不影响其它
  span 声明的语言（"无内嵌字幕，外挂简中" → 仍有 zh-Hans）；
- 泛称政策："中字/中文字幕"解析为泛称 zh，是否对外输出由
  ``EMIT_GENERIC_ZH`` 一行控制（当前与既有行为对齐：不输出，不猜简繁）。

政策变更只改这里，不需要重训模型；每个结论都可从输入 span 文本复现。
"""

from __future__ import annotations

import re

# 泛称"中字/中文"输出为 "zh"（不猜简繁，泛称就是泛称）。订阅选种的
# "必须中文字幕"规则按 BCP 47 前缀匹配（要求 zh 命中 zh/zh-Hans/zh-Hant），
# "中字"是 PT 最高频写法，不输出泛称会让选种规则拒掉海量正常资源。
# 政策在代码不在模型：改动此开关须 bump ENRICH_VERSION。
EMIT_GENERIC_ZH = True

# —— 字幕轴语言词表（长词优先匹配；命中即消费，避免"简体中文"再被"中文"重复吃）
_SUB_LANG_TOKENS: tuple[tuple[str, str | None], ...] = (
    ("简体中文", "zh-Hans"), ("簡體中文", "zh-Hans"), ("繁体中文", "zh-Hant"),
    ("繁體中文", "zh-Hant"), ("简中", "zh-Hans"), ("簡中", "zh-Hans"),
    ("繁中", "zh-Hant"), ("简体", "zh-Hans"), ("簡體", "zh-Hans"),
    ("繁体", "zh-Hant"), ("繁體", "zh-Hant"),
    ("ZH-HANS", "zh-Hans"), ("ZH_HANS", "zh-Hans"), ("ZHHANS", "zh-Hans"),
    ("CHS", "zh-Hans"), ("ZHS", "zh-Hans"), ("CHT", "zh-Hant"), ("ZHT", "zh-Hant"),
    ("中文字幕", "zh"), ("中字", "zh"), ("中文", "zh"),
    # 单字字形/语种（"简繁英字幕"、"简日双语"的连写形态）
    ("简", "zh-Hans"), ("簡", "zh-Hans"), ("繁", "zh-Hant"), ("英", "en"),
    ("日", "ja"), ("韩", "ko"), ("韓", "ko"), ("粤", "yue"), ("粵", "yue"),
    ("意", "it"), ("俄", "ru"), ("法", "fr"), ("德", "de"), ("西", "es"),
    ("泰", "th"), ("中", "zh"),
)

# —— 音轨轴语言词表（原声语言与配音语言同口径）
_AUDIO_LANG_TOKENS: tuple[tuple[str, str], ...] = (
    ("汉语普通话", "cmn"), ("普通话", "cmn"), ("意大利", "it"),
    ("国语", "cmn"), ("國語", "cmn"), ("国配", "cmn"), ("台配", "cmn"),
    ("粤语", "yue"), ("粵語", "yue"), ("英语", "en"), ("日语", "ja"),
    ("韩语", "ko"), ("韓語", "ko"), ("法语", "fr"), ("德语", "de"),
    ("俄语", "ru"), ("西班牙语", "es"), ("泰语", "th"), ("Cantonese", "yue"),
    ("Mandarin", "cmn"),
    # 连写段单字（"国|英音轨"、"国粤英三语"、"国日双语"）
    ("国", "cmn"), ("國", "cmn"), ("粤", "yue"), ("粵", "yue"), ("英", "en"),
    ("日", "ja"), ("韩", "ko"), ("韓", "ko"), ("中", "cmn"),
)

_CARRIER_TOKENS: tuple[tuple[str, str], ...] = (
    ("内封", "embedded"), ("內封", "embedded"), ("软字幕", "embedded"),
    ("軟字幕", "embedded"), ("内嵌", "hardcoded"), ("內嵌", "hardcoded"),
    ("硬字幕", "hardcoded"), ("外挂", "external"), ("外掛", "external"),
)

_NEG_WORD_RE = re.compile(r"无|沒有|没有|不含|無")
# 裸否定：否定词与"字幕"直接相邻（允许范围限定词），或字段值式"字幕：无"，
# 或英文 NO SUBS——语义是"本资源没有字幕"，全局生效
_BARE_NEG_RE = re.compile(
    r"(?:无|沒有|没有|不含|無)\s*(?:任何|全部|中文|简体中文|簡體中文)?字幕|"
    r"字幕(?:语言|語言)?\s*[:：]\s*(?:无|無|没有|沒有)|"
    r"(?<![A-Za-z])NO[\s._-]*SUBS?(?![A-Za-z])",
    re.I,
)


def _scan(text: str, tokens: tuple[tuple[str, str | None], ...]) -> list[str]:
    """长词优先扫描：命中的片段以占位符消费，防止子串重复计入。

    ASCII 词大小写不敏感——逐字符 upper 保证长度不变（str.upper() 整串
    转换在 ß 等字符上会变长导致索引错位）。"""
    found: list[str] = []
    consumed = list(text)
    for token, lang in tokens:
        needle = token.upper()
        while True:
            probe = "".join(ch.upper() if ch.isascii() else ch for ch in consumed)
            i = probe.find(needle)
            if i == -1:
                break
            for j in range(i, i + len(token)):
                consumed[j] = "\x00"
            if lang and lang not in found:
                found.append(lang)
    return found


def parse_declarations(
    subtitle_decls: list[str], audio_decls: list[str]
) -> dict[str, list[str]]:
    """把模型抽出的声明 span 文本解析成结构化语言/载体事实。

    返回 ``subtitle_languages`` / ``subtitle_carriers`` / ``audio_languages``
    三个列表（保持首次出现顺序）。空列表 = 无观测（三态铁律）。
    """
    sub_langs: list[str] = []
    carriers: list[str] = []
    bare_negated = False
    suppressed_carriers: set[str] = set()

    for decl in subtitle_decls:
        if _BARE_NEG_RE.search(decl):
            bare_negated = True
            continue
        span_carriers = [c for token, c in _CARRIER_TOKENS if token in decl]
        if _NEG_WORD_RE.search(decl) and span_carriers:
            # 载体限定否定（"无内嵌字幕"）：只压制该载体，不产出语言
            suppressed_carriers.update(span_carriers)
            continue
        if _NEG_WORD_RE.search(decl):
            # 含否定词但既非裸否定也无载体（罕见），保守视为全局否定
            bare_negated = True
            continue
        for lang in _scan(decl, _SUB_LANG_TOKENS):
            if lang == "zh" and not EMIT_GENERIC_ZH:
                continue  # 泛称政策：不猜简繁
            if lang not in sub_langs:
                sub_langs.append(lang)
        for c in span_carriers:
            if c not in carriers and c not in suppressed_carriers:
                carriers.append(c)

    if bare_negated:
        # 显式"无字幕"压倒一切声明（跨段生效），与既有正则语义一致
        sub_langs = []
        carriers = []

    audio_langs: list[str] = []
    for decl in audio_decls:
        for lang in _scan(decl, _AUDIO_LANG_TOKENS):
            if lang not in audio_langs:
                audio_langs.append(lang)

    return {
        "subtitle_languages": sub_langs,
        "subtitle_carriers": [c for c in carriers if c not in suppressed_carriers],
        "audio_languages": audio_langs,
    }
