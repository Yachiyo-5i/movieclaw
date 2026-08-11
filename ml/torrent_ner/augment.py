"""结构化合成增强：教模型"某类文本变换不改变（或确定性改变）标签"。

四种模式（全部是机械变换，标签绝对正确，零标注成本）：
- decoration     副标题前拼装饰前缀（*活動置頂N* 等），span 整体平移
- trailing_stop  中文片名 span 之后插入句号"。"，span 不含句号——治"你的名字。"
- version_suffix 片名 span 之后插入" IMAX版/加长版/…"，span 不含版本词
- chapter        片名 span 之后插入" 第N章"，并为其添加 EPISODE 标签
另有 collection 过采样：真实 collection 标注太稀有（<100 条），复制并施加
decoration 变换扩充分类头的监督信号。

产物写独立文件，训练时经 train.py --extra-train 只进训练集、绝不进 dev/test。

    python ml/torrent_ner/augment.py          # 零依赖
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from torrent_ner.dataio import read_jsonl

DECORATIONS = [
    "*活動置頂{n}*", "*活动置顶{n}*", "【活动】", "【置顶】", "置顶 ",
    "*杜比专区*", "【官方活动】", "*限时优惠*", "[应求] ", "【首发】",
]
VERSION_WORDS = [" IMAX版", " 加长版", " 导演剪辑版", " 未删减版", " 修复版", " 完整版", " 典藏版"]
CN_NUMS = "一二三四五六七八九十"


def shift_spans(spans: list[dict], source: str, pos: int, delta: int) -> list[dict]:
    """指定来源中 pos 之后的 span 整体平移 delta。"""
    out = []
    for s in spans:
        if s["source"] == source and s["start"] >= pos:
            out.append({**s, "start": s["start"] + delta, "end": s["end"] + delta})
        else:
            out.append(dict(s))
    return out


def pick_zh_span(record: dict) -> dict | None:
    """选一个副标题里的中文片名 span 作为插入锚点。"""
    candidates = [
        s for s in record["spans"]
        if s["source"] == "subtitle" and s["field"] == "TITLE_ZH" and s["end"] - s["start"] >= 2
    ]
    return candidates[0] if candidates else None


def make_decoration(record: dict, rng: random.Random) -> dict:
    deco = rng.choice(DECORATIONS).format(n=rng.randrange(100, 300))
    return {
        **record,
        "subtitle": deco + record["subtitle"],
        "spans": shift_spans(record["spans"], "subtitle", 0, len(deco)),
    }


def make_insertion(record: dict, rng: random.Random, mode: str) -> dict | None:
    """在中文片名 span 之后插入文本；span 不吞插入内容。"""
    anchor = pick_zh_span(record)
    if anchor is None:
        return None
    pos = anchor["end"]
    if mode == "trailing_stop":
        ins = "。"
        extra = None
    elif mode == "version_suffix":
        ins = rng.choice(VERSION_WORDS)
        extra = None
    elif mode == "variant_inject":
        # 在干净别名后注入"含类型标记的变体别名"且**不打任何标签**——教模型
        # "有干净兄弟别名时跳过标记变体"（v10 择干净规则的合成监督）
        title_text = record["subtitle"][anchor["start"]:anchor["end"]]
        marker = rng.choice(["剧场版：", " 剧场版 ", " 总集篇 ", " 特别篇 "])
        suffix = rng.choice(["最终章", "完结篇", ""])
        ins = f" / {title_text}{marker}{suffix}".rstrip()
        extra = None
    else:  # chapter
        expr = f"第{rng.choice(CN_NUMS)}章"
        ins = " " + expr
        # 章节表达按 v10 规范入 EPISODE
        extra = {"source": "subtitle", "field": "EPISODE", "start": pos + 1, "end": pos + 1 + len(expr)}
    spans = shift_spans(record["spans"], "subtitle", pos, len(ins))
    if extra:
        spans.append(extra)
        spans.sort(key=lambda s: (s["source"], s["start"]))
    return {**record, "subtitle": record["subtitle"][:pos] + ins + record["subtitle"][pos:], "spans": spans}


# —— 字幕/音轨两轴的对抗合成（v13 新增，见 docs/design/subtitle-audio-ner.md）
# 尾词定轴对：同一语言词 + 不同尾词 → 不同轴，强迫模型只能靠尾词判别
AXIS_PAIRS = [
    ("中文字幕", "SUBTITLE", "中文配音", "AUDIO"),
    ("粤语字幕", "SUBTITLE", "粤语配音", "AUDIO"),
    ("日语字幕", "SUBTITLE", "日语配音", "AUDIO"),
    ("简繁字幕", "SUBTITLE", "国语音轨", "AUDIO"),
]
NEGATIONS = ["无字幕", "字幕：无", "無字幕", "NO SUBS"]
AUDIO_TAGS = ["[国语]", "[粤语]", "*日语*", "[英语]"]  # 括号里的语言词打 AUDIO
COMMENTARY = ["评论音轨", "多规格音轨", "2Audios", "双音轨"]  # 无语言信息，不打标签


def _append_subtitle(record: dict, text: str, label_ranges: list[tuple[int, int, str]]) -> dict:
    """在副标题尾部追加 " <text>"，并按相对区间打标签（不影响既有 span）。"""
    base = record["subtitle"]
    sep = " " if base and not base.endswith(" ") else ""
    pos = len(base) + len(sep)
    spans = [dict(s) for s in record["spans"]]
    for rel_start, rel_end, field in label_ranges:
        spans.append({"source": "subtitle", "field": field,
                      "start": pos + rel_start, "end": pos + rel_end})
    spans.sort(key=lambda s: (s["source"], s["start"]))
    return {**record, "subtitle": base + sep + text, "spans": spans}


def make_decl_injection(record: dict, rng: random.Random, mode: str) -> list[dict] | None:
    has_axis = {s["field"] for s in record["spans"] if s["field"] in ("SUBTITLE", "AUDIO")}
    if mode == "dub_vs_sub":
        # 成对产出：偏好本就无两轴声明的样本，对比信号最干净
        if has_axis:
            return None
        sub_text, sub_field, dub_text, dub_field = rng.choice(AXIS_PAIRS)
        return [
            _append_subtitle(record, sub_text, [(0, len(sub_text), sub_field)]),
            _append_subtitle(record, dub_text, [(0, len(dub_text), dub_field)]),
        ]
    if mode == "sub_negation":
        if "SUBTITLE" in has_axis:
            return None
        neg = rng.choice(NEGATIONS)
        return [_append_subtitle(record, neg, [(0, len(neg), "SUBTITLE")])]
    if mode == "audio_tag":
        if "AUDIO" in has_axis:
            return None
        tag = rng.choice(AUDIO_TAGS)
        # 只给括号内的语言词打标（规范：独立成标签的语言词抽词本身）
        return [_append_subtitle(record, tag, [(1, len(tag) - 1, "AUDIO")])]
    if mode == "commentary":
        word = rng.choice(COMMENTARY)
        return [_append_subtitle(record, word, [])]  # 负监督：不打任何标签
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="结构化合成增强")
    parser.add_argument("--data", default="ml/data/labeled/annotations.jsonl")
    parser.add_argument("--out", default="ml/data/labeled/augmented.jsonl")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    records = [r for r in read_jsonl(args.data) if r["spans"] and not r.get("review")]
    base = [r for r in records if r.get("subtitle", "").strip()]
    collections = [r for r in records if r.get("media_type") == "collection"]

    plan = [("decoration", 300), ("trailing_stop", 250), ("version_suffix", 250), ("chapter", 200),
            ("variant_inject", 250),
            # 字幕/音轨两轴对抗合成（v13）：尾词定轴对/否定式/独立标签/无语言负监督
            ("dub_vs_sub", 300), ("sub_negation", 250), ("audio_tag", 250), ("commentary", 200)]
    decl_modes = {"dub_vs_sub", "sub_negation", "audio_tag", "commentary"}
    rows = []
    for mode, quota in plan:
        made = 0
        for record in rng.sample(base, len(base)):
            if made >= quota:
                break
            if mode in decl_modes:
                outs = make_decl_injection(record, rng, mode)
            elif mode == "decoration":
                outs = [make_decoration(record, rng)]
            else:
                one = make_insertion(record, rng, mode)
                outs = [one] if one else None
            if not outs:
                continue
            for j, out in enumerate(outs):
                rows.append({**out, "id": f"aug-{mode}{j}:{record['id']}",
                             "annotator": f"synthetic:{mode}"})
                made += 1
        print(f"{mode}: 合成 {made} 条")

    # collection 过采样 ×5（真实 collection 太稀有，分类头需要更强监督；
    # 施加 decoration 变换避免完全重复）
    for i in range(5):
        for record in collections:
            rows.append({**make_decoration(record, rng), "id": f"aug-collection{i}:{record['id']}",
                         "annotator": "synthetic:collection-oversample"})
    print(f"collection 过采样: {len(collections) * 5} 条")

    with open(args.out, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"共 {len(rows)} 条 → {args.out}")


if __name__ == "__main__":
    main()
