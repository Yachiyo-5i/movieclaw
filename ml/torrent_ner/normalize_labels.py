"""v12 → v13 标签确定性归一：把规范增量中可机器判定的部分直接改写存量标签。

规则（v13 裁决，见 docs/design/subtitle-audio-ner.md）：
1. EPISODE_TOTAL 中文本恰为纯英文完结词（Complete/COMPLETE/Fin）的 span 删除
   ——闭词表归运行时正则通道；
2. YEAR span 嵌在更长数字串内（紧邻字符是数字，如 "20260724" 里的 "2026"）删除
   ——8 位日期串是播出日期不是发行年份；
3. YEAR span 后紧跟"年<数字>月"档期模式（"2026年7月新番"）删除；
4. TITLE_ZH/TITLE_EN span 尾部的地区括注（"(港)"、"(台)"、"(美版)" 等）截掉
   ——地区标记不入片名。

就地重写文件；逐规则打印计数。分歧裁决（adjudicate.py）前先跑本工具，
可消掉 v12 主标 vs v13 互标之间的系统性假分歧。只依赖标准库。

    python ml/torrent_ner/normalize_labels.py <标注文件...>
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter

COMPLETE_WORDS = {"Complete", "COMPLETE", "complete", "Fin", "FIN"}
# 地区括注白名单（只截确定性的，存疑的留给裁决）
_REGION_RE = re.compile(r"[（(](?:港|台|美版|日版|韩版|韩国版|港台|美|英|日|韩)[)）]\s*$")
_SCHEDULE_RE = re.compile(r"^年\d{1,2}月")


def normalize_record(r: dict, stats: Counter) -> None:
    texts = {"title": r.get("title", ""), "subtitle": r.get("subtitle", "")}
    kept = []
    seen: set[tuple] = set()
    for s in r.get("spans", []):
        text = texts[s["source"]]
        frag = text[s["start"]:s["end"]]
        if s["field"] == "EPISODE_TOTAL" and frag in COMPLETE_WORDS:
            stats["complete_dropped"] += 1
            continue
        if s["field"] == "YEAR":
            before = text[s["start"] - 1] if s["start"] > 0 else ""
            after = text[s["end"]] if s["end"] < len(text) else ""
            if before.isdigit() or after.isdigit():
                stats["year_in_datestring_dropped"] += 1
                continue
            if _SCHEDULE_RE.match(text[s["end"]:s["end"] + 4]):
                stats["year_schedule_dropped"] += 1
                continue
        if s["field"] in ("TITLE_ZH", "TITLE_EN"):
            m = _REGION_RE.search(frag)
            if m:
                s = {**s, "end": s["start"] + m.start()}
                # 截尾后再去掉尾部空白
                while s["end"] > s["start"] and text[s["end"] - 1].isspace():
                    s["end"] -= 1
                if s["end"] <= s["start"]:
                    stats["title_region_dropped_empty"] += 1
                    continue
                stats["title_region_trimmed"] += 1
        key = (s["source"], s["field"], s["start"], s["end"])
        if key in seen:  # 截尾可能与既有 span 重合
            stats["dedup_after_trim"] += 1
            continue
        seen.add(key)
        kept.append(s)
    r["spans"] = kept


def main() -> None:
    for path in sys.argv[1:]:
        stats: Counter = Counter()
        lines = []
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            normalize_record(r, stats)
            lines.append(json.dumps(r, ensure_ascii=False))
        open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        print(f"{path}: {dict(stats) or '无改动'}")


if __name__ == "__main__":
    main()
