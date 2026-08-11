"""成品语料配比审计：合并后、训练前的最后一道门。

队列层配比 ≠ 成品配比——review 隔离非均匀（难域被隔离更多）、去重、标注
失败都会让实际分布漂移。本工具对最终语料出实际分布报告，对照混合配方
（docs/design/subtitle-audio-ner.md）标记违约项；违约 → 定向补采后再训练。

    python ml/torrent_ner/corpus_report.py --data ml/data/labeled/annotations.jsonl

检查项与目标（配方版本 2026-08）：
- 类别占比（τ=2 落点 ±5pt）：series+movie 各 ~20%、anime ~15%、documentary ~7%、
  variety ~6%、music+other ~5%
- 声明词汇域 floor：简日/字幕组/外挂/配音/无字幕/粤语/双语 等每域 ≥300（clean 层）
- 负样本（无 SUBTITLE/AUDIO span）占比 15-35%
- 金标层（dual_agreed）≥ 语料 15%；review 隔离率 ≤10% 且无单桶 >30%
- 站点覆盖 ≥12 站，单站 ≤25%
只依赖标准库。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from torrent_ner.dataio import read_jsonl, split_of

DOMAIN_PROBES = {
    "简日/繁日": ("简日", "簡日", "繁日"),
    "字幕组": ("字幕组", "字幕組", "字幕社"),
    "外挂": ("外挂", "外掛"),
    "配音/音轨": ("配音", "音轨", "音軌"),
    "无字幕": ("无字幕", "無字幕"),
    "粤语": ("粤语", "粵語"),
    "双语": ("双语", "雙語"),
    "硬字幕": ("硬字幕",),
    "内封/内嵌": ("内封", "內封", "内嵌", "內嵌"),
}
FAIL = "❌"
OK = "✓"


def pct(n: int, total: int) -> str:
    return f"{n / max(1, total):.1%}"


def check(name: str, value: str, ok: bool, problems: list[str]) -> None:
    print(f"  [{OK if ok else FAIL}] {name}: {value}")
    if not ok:
        problems.append(name)


def main() -> None:
    ap = argparse.ArgumentParser(description="成品语料配比审计")
    ap.add_argument("--data", default="ml/data/labeled/annotations.jsonl")
    ap.add_argument("--strict", action="store_true", help="违约时退出码 1（接入流水线用）")
    args = ap.parse_args()

    items = read_jsonl(args.data)
    total = len(items)
    clean = [i for i in items if not i.get("review")]
    problems: list[str] = []
    print(f"语料 {total} 条（clean {len(clean)}，review 隔离 {total - len(clean)}"
          f" = {pct(total - len(clean), total)}）\n")

    print("— 切分与质量分层")
    splits = Counter(split_of(i["id"]) for i in items)
    gold = sum(1 for i in items if i.get("dual_agreed"))
    check("review 隔离率 ≤10%", pct(total - len(clean), total),
          (total - len(clean)) / max(1, total) <= 0.10, problems)
    check("金标层(dual_agreed) ≥15%", pct(gold, total), gold / max(1, total) >= 0.15, problems)
    print(f"      切分: {dict(splits)}")

    print("— 站点覆盖（clean 层）")
    sites = Counter(i["id"].split(":", 1)[0] for i in clean)
    top_site, top_n = sites.most_common(1)[0]
    check("站点数 ≥12", str(len(sites)), len(sites) >= 12, problems)
    check(f"单站上限 ≤25%（最大 {top_site}）", pct(top_n, len(clean)),
          top_n / max(1, len(clean)) <= 0.25, problems)

    print("— 类别占比（clean 层，media/content 标签联合口径）")
    cat = Counter()
    for i in clean:
        c = i.get("content_type")
        cat[c if c and c != "other" else i.get("media_type", "?")] += 1
    for name, lo, hi in (("anime", 0.10, 0.25), ("documentary", 0.03, 0.12),
                         ("variety", 0.02, 0.12), ("music", 0.01, 0.10),
                         ("movie", 0.12, 0.35), ("series", 0.15, 0.45)):
        n = cat.get(name, 0)
        check(f"{name} 占比", pct(n, len(clean)),
              lo <= n / max(1, len(clean)) <= hi, problems)

    print("— 声明词汇域 floor（clean 层，文本探针）")
    for name, kws in DOMAIN_PROBES.items():
        n = sum(1 for i in clean
                if any(k in i["title"] or k in i.get("subtitle", "") for k in kws))
        check(f"{name} ≥300", str(n), n >= 300, problems)

    print("— 负样本分层（无 SUBTITLE/AUDIO span 按用途拆分，见训练前审查 6.2）")
    HARD_PROBE = ("字幕组", "字幕組", "水印", "水印", "评论音轨", "Super", "SUP")
    neg = hard_neg = nonmedia_neg = plain_neg = sub_only = audio_only = 0
    for i in clean:
        fields = {s["field"] for s in i.get("spans", [])}
        has_sub, has_audio = "SUBTITLE" in fields, "AUDIO" in fields
        if has_sub and not has_audio:
            sub_only += 1
        elif has_audio and not has_sub:
            audio_only += 1
        if has_sub or has_audio:
            continue
        neg += 1
        text = i["title"] + i.get("subtitle", "")
        if any(k in text for k in HARD_PROBE):
            hard_neg += 1
        elif i.get("media_type") == "other":
            nonmedia_neg += 1
        else:
            plain_neg += 1
    print(f"      硬负例（字幕组/水印/SUP词形/评论音轨语境）: {hard_neg}（{pct(hard_neg, len(clean))}）")
    print(f"      非影视域外负例: {nonmedia_neg}（{pct(nonmedia_neg, len(clean))}）")
    print(f"      普通影视无声明负例: {plain_neg}（{pct(plain_neg, len(clean))}）")
    print(f"      单轴样本：仅字幕 {sub_only}，仅音轨 {audio_only}")
    check("负样本总量 15-40%（报警区间，分层达标可放宽）", pct(neg, len(clean)),
          0.15 <= neg / max(1, len(clean)) <= 0.40, problems)
    check("硬负例 ≥300", str(hard_neg), hard_neg >= 300, problems)

    print("— 分桶 review 率（若样本带 bucket 字段，>30% 标红）")
    bucket_total: Counter = Counter()
    bucket_review: Counter = Counter()
    for i in items:
        b = i.get("bucket")
        if b:
            bucket_total[b] += 1
            if i.get("review"):
                bucket_review[b] += 1
    for b, n in bucket_total.most_common():
        rate = bucket_review[b] / n
        flag = FAIL if rate > 0.30 else OK
        print(f"  [{flag}] {b}: review {bucket_review[b]}/{n} = {rate:.0%}")
        if rate > 0.30:
            problems.append(f"bucket:{b}")

    print(f"\n{'违约项: ' + ', '.join(problems) if problems else '全部达标 ✓'}")
    if problems and args.strict:
        sys.exit(1)


if __name__ == "__main__":
    main()
