"""双引擎标注合并：一致样本直入语料，分歧样本打 review 隔离待裁决。

质量分层策略（docs/design/subtitle-audio-ner.md「混合配方」第 4 条）的工具化：
- 两引擎都标过的样本：八字段 span 集合 + 两个分类轴逐一比对——
  全一致 → 取主引擎记录并标记 `dual_agreed`（金标层素材）；
  有分歧 → 取主引擎记录 + `review` 列出分歧字段（clean_only 训练自动隔离，
  人工/裁决通道后续处理）；
- 只有单引擎标过的样本：原样直入。

    python ml/torrent_ner/merge_dual.py \\
        --primary ml/data/labeled/nas-pilot.codex.jsonl \\
        --secondary ml/data/labeled/nas-pilot.claude.jsonl \\
        --out ml/data/labeled/annotations.jsonl

主引擎选 span 质量更全的一方（门控 1 裁决：codex 在开头标签完整性上更优）。
只依赖标准库。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from torrent_ner.dataio import read_jsonl, split_of
from torrent_ner.labels import FIELDS


def span_set(item: dict, field: str) -> frozenset:
    return frozenset(
        (s["source"], s["start"], s["end"]) for s in item.get("spans", []) if s["field"] == field
    )


def diff_fields(a: dict, b: dict) -> list[str]:
    """返回两条标注在八字段 + 两分类轴上的分歧字段名列表。"""
    diffs = [f for f in FIELDS if span_set(a, f) != span_set(b, f)]
    for key in ("media_type", "content_type"):
        if a.get(key) != b.get(key):
            diffs.append(key)
    return diffs


def main() -> None:
    parser = argparse.ArgumentParser(description="双引擎标注合并")
    parser.add_argument("--primary", required=True, help="主引擎标注文件（分歧时保留其标签）")
    parser.add_argument("--secondary", required=True, help="副引擎标注文件")
    parser.add_argument("--out", default="ml/data/labeled/annotations.jsonl")
    parser.add_argument("--adjudications", nargs="*", default=[],
                        help="裁决日志（可多个），用于标记 llm_adjudicated 溯源")
    args = parser.parse_args()

    adjudicated_ids: set[str] = set()
    for log in args.adjudications:
        for entry in read_jsonl(log):
            if entry.get("verdict") in ("a", "b"):
                adjudicated_ids.add(entry["id"])

    primary = {i["id"]: i for i in read_jsonl(args.primary)}
    secondary = {i["id"]: i for i in read_jsonl(args.secondary)}

    merged: list[dict] = []
    stats = Counter()
    field_diffs: Counter = Counter()
    for sample_id in sorted(primary | secondary):
        p, s = primary.get(sample_id), secondary.get(sample_id)
        if p and s:
            diffs = diff_fields(p, s)
            record = dict(p)
            record["dual_annotated"] = True
            if diffs:
                stats["dual_disagree"] += 1
                field_diffs.update(diffs)
                record["review"] = (record.get("review") or []) + [
                    f"双引擎分歧: {','.join(diffs)}（副引擎 {s.get('annotator', '?')}）"
                ]
            else:
                stats["dual_agree"] += 1
                record["dual_agreed"] = True
            # 质量溯源四级（训练前审查 4.1）：llm_adjudicated > dual_agreed > single；
            # human_adjudicated 由人工终审工具另行盖章
            if sample_id in adjudicated_ids:
                record["label_status"] = "llm_adjudicated"
            elif record.get("dual_agreed"):
                record["label_status"] = "dual_agreed"
            else:
                record["label_status"] = "single"
        else:
            stats["single"] += 1
            record = dict(p or s)
            record["label_status"] = "single"
        merged.append(record)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for record in merged:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    total = len(merged)
    dual = stats["dual_agree"] + stats["dual_disagree"]
    splits = Counter(split_of(r["id"]) for r in merged)
    print(f"共 {total} 条 → {out}")
    print(f"双引擎 {dual} 条：一致 {stats['dual_agree']}"
          f"（{stats['dual_agree'] / max(1, dual):.1%}），分歧 {stats['dual_disagree']} 条已打 review")
    print(f"单引擎直入 {stats['single']} 条；切分分布 {dict(splits)}")
    if field_diffs:
        print("分歧字段分布:", dict(field_diffs.most_common()))


if __name__ == "__main__":
    main()
