"""Tier 0.5：变体超集分歧的确定性自动裁决（分歧金字塔的免裁判层）。

背景：v13 规范明确"别名变体（点号/空格/大小写/标点）各自完整抽出、两段重复
表达都列出"。v12 主标经常漏变体，v13 互标抽得更全——由此产生的分歧是
**一方为另一方严格超集**的形态。fable5 终审判例（2026-08-10，16/19 判超集方胜）
支持将满足以下全部条件的分歧机器判给超集方：

1. 一方 span 值集合是另一方的严格超集（span 字段逐字段判断）；
2. 超集多出的每个值，满足其一：
   a. 是共识值的**写法变体**——去掉 [.\\s:：!！?？'’-] 并折叠大小写后与某个
      共识值相等（"Tunnel.Warfare" ≈ "Tunnel Warfare"）；
   b. 是共识值在**另一来源段**的重复出现（同文本再现，如 "S07" 与 "第七季"
      场景外的双段重复、大小写异写重现）。
3. 仅适用于 TITLE_ZH/TITLE_EN/SEASON/EPISODE/SUBTITLE/AUDIO——YEAR 与
   EPISODE_TOTAL 的超集多为真错误（多抽年份/Complete），不适用。

不满足条件的分歧原样留给 adjudicate.py。就地写回两份文件，打印计数。

    python ml/torrent_ner/resolve_superset.py --a <主标> --b <互标>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from torrent_ner.dataio import read_jsonl
from torrent_ner.merge_dual import span_set

APPLICABLE = ("TITLE_ZH", "TITLE_EN", "SEASON", "EPISODE", "SUBTITLE", "AUDIO")
_NORM_RE = re.compile(r"[.\s:：!！?？'’\-]+")
# 点号场景名（"Tunnel.Warfare"、"Zhan.Du.Xing.Dong"）：v13 规则"场景点号名
# 始终抽"+ fable 终审判例支持，超集方新增此形态的 TITLE_EN 值可直接放行
_DOTTED_SCENE_RE = re.compile(r"^[A-Za-z0-9][\w&'!,()\-]*(?:\.[\w&'!,()\-]+){1,}$")


def norm(v: str) -> str:
    return _NORM_RE.sub("", v).lower()


def resolve_pair(ra: dict, rb: dict, stats: Counter) -> None:
    texts = {"title": ra["title"], "subtitle": ra.get("subtitle", "")}

    def values(rec, field):
        return {(s, st, en, texts[s][st:en]) for s, st, en in span_set(rec, field)}

    for field in APPLICABLE:
        va, vb = values(ra, field), values(rb, field)
        if va == vb:
            continue
        if va < vb:
            small_rec, big_rec, small, big = ra, rb, va, vb
        elif vb < va:
            small_rec, big_rec, small, big = rb, ra, vb, va
        else:
            stats[f"{field}:非超集"] += 1
            continue
        consensus_norms = {norm(t) for _, _, _, t in small}
        extras = big - small

        def extra_ok(source: str, text_val: str) -> bool:
            if norm(text_val) in consensus_norms:
                return True  # 共识值的写法变体/另一段重复
            # title 段的点号场景名：v13 规则始终抽，超集方补上的是对的
            return (field == "TITLE_EN" and source == "title"
                    and _DOTTED_SCENE_RE.match(text_val) is not None)

        if all(extra_ok(s, t) for s, _, _, t in extras):
            # 超集胜：败方该字段 spans 替换为超集方的
            win_spans = [s for s in big_rec["spans"] if s["field"] == field]
            small_rec["spans"] = [s for s in small_rec["spans"]
                                  if s["field"] != field] + [dict(s) for s in win_spans]
            small_rec["spans"].sort(key=lambda s: (s["source"], s["start"]))
            stats[f"{field}:超集自动定案"] += 1
        else:
            stats[f"{field}:超集但含新值(留裁判)"] += 1


def main() -> None:
    ap = argparse.ArgumentParser(description="变体超集分歧自动裁决")
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    args = ap.parse_args()
    a_recs = {r["id"]: r for r in read_jsonl(args.a)}
    b_recs = {r["id"]: r for r in read_jsonl(args.b)}
    stats: Counter = Counter()
    for rid in sorted(set(a_recs) & set(b_recs)):
        resolve_pair(a_recs[rid], b_recs[rid], stats)
    for path, recs in ((args.a, a_recs), (args.b, b_recs)):
        with open(path, "w", encoding="utf-8") as f:
            for r in recs.values():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    settled = sum(n for k, n in stats.items() if k.endswith("自动定案"))
    print(f"超集自动定案 {settled} 个字段分歧；明细 {dict(stats.most_common())}")


if __name__ == "__main__":
    main()
