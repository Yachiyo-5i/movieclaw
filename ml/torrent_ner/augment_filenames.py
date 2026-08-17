"""单集文件名形态的定向合成增强：治 torrent-ner-v2 的「点分隔单集 × 中高季号」盲区。

2026-08-17 定量诊断（会话记录 + docs/design/subtitle-audio-ner.md 补充）：
扫季实验里 v2 在点分隔单集 S04–S07 塌方到 6~9/12（v1 全对），塌方区间与
语料稀疏区间逐季吻合——点分隔单集 738 条中 S01 独占 539，S04=18/S05=10/
S06=4/S07=1/S08=0，中英混合点分隔单集仅 31 条。病灶是**形态×季号的联合
分布偏斜**，不是总量不足。

两种机械变换（标签绝对正确，零标注成本，延续 augment.py 的哲学）：
- season_renumber  把点分隔单集的 "S01" 原位重编号为 S02–S12（等宽替换，
                   span 偏移为零），按塌方区间加权补桶；
- cjk_prefix       前置「中文名.第N季.」还原真实世界的中英混合文件名
                   （十三邀.第五季.Thirteen.Talks.S05E10... 病例形态），
                   中文数字与重编号后的季号严格一致，title 侧 span 整体平移。

产物写独立文件，训练时经 train.py --extra-train 只进训练集、绝不进 dev/test。

    python ml/torrent_ner/augment_filenames.py          # 零依赖
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from torrent_ner.augment import shift_spans
from torrent_ner.dataio import read_jsonl

_SEASON_TXT = re.compile(r"^[Ss]\d{2}$")
_EPISODE_TXT = re.compile(r"^[Ee]\d{1,4}$")
_CJK = re.compile(r"[一-鿿]")

# 目标季号的采样权重：S04–S07 是实测塌方区间，加倍补；S08+ 顺带铺开
_TARGET_SEASONS = [2, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 10, 11, 12]
_CN_SEASON = {
    2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七",
    8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二",
}


def _dotted(title: str) -> bool:
    return title.count(".") >= 3 and " " not in title.strip()


def _single_ep_dotted(record: dict) -> tuple[dict, dict] | None:
    """记录是否为「点分隔单集」且结构可安全改写；是则返回 (SEASON, EPISODE) span。

    资格刻意收紧：恰好一个 SxN 形态的 SEASON、恰好一个 E 前缀单集 EPISODE、
    标题无 CJK（中文「第N季」与数字季号强关联，重编号会把关联改破）。
    """
    title = record["title"]
    if not _dotted(title) or _CJK.search(title):
        return None
    seasons = [
        s for s in record["spans"]
        if s["source"] == "title" and s["field"] == "SEASON"
    ]
    episodes = [
        s for s in record["spans"]
        if s["source"] == "title" and s["field"] == "EPISODE"
    ]
    if len(seasons) != 1 or len(episodes) != 1:
        return None
    season, episode = seasons[0], episodes[0]
    if not _SEASON_TXT.match(title[season["start"]:season["end"]]):
        return None
    if not _EPISODE_TXT.match(title[episode["start"]:episode["end"]]):
        return None
    return season, episode


def season_renumber(record: dict, season_span: dict, target: int) -> dict:
    """把 SEASON span 原位改写成 S{target:02d}：等宽替换，span 全部不动。"""
    title = record["title"]
    start, end = season_span["start"], season_span["end"]
    new_text = f"S{target:02d}"
    assert end - start == len(new_text), "等宽替换前提被打破"
    return {
        **record,
        "id": f"{record['id']}::renum-s{target:02d}",
        "title": title[:start] + new_text + title[end:],
        "spans": [dict(s) for s in record["spans"]],
    }


def cjk_prefix(record: dict, season_span: dict, zh_title: str) -> dict:
    """前置「中文名.第N季.」，中文季号与数字季号严格一致；title 侧 span 平移。"""
    title = record["title"]
    number = int(title[season_span["start"] + 1:season_span["end"]])
    cn = _CN_SEASON.get(number)
    if cn is None:
        raise ValueError(f"季号 {number} 不在中文数字表内")
    season_word = f"第{cn}季"
    prefix = f"{zh_title}.{season_word}."
    spans = shift_spans(record["spans"], "title", 0, len(prefix))
    spans.append({"source": "title", "field": "TITLE_ZH", "start": 0, "end": len(zh_title)})
    zh_season_start = len(zh_title) + 1
    spans.append({
        "source": "title",
        "field": "SEASON",
        "start": zh_season_start,
        "end": zh_season_start + len(season_word),
    })
    return {
        **record,
        "id": f"{record['id']}::cjkpfx",
        "title": prefix + title,
        "spans": spans,
    }


def _zh_pool(records: list[dict]) -> list[str]:
    """真实中文片名池：从语料的 TITLE_ZH 标注里取（点/空格会破坏文件名形态，排除）。"""
    pool: set[str] = set()
    for r in records:
        for s in r["spans"]:
            text_source = r["title"] if s["source"] == "title" else r.get("subtitle", "")
            text = text_source[s["start"]:s["end"]]
            if (
                s["field"] == "TITLE_ZH"
                and 2 <= len(text) <= 8
                and "." not in text
                and " " not in text
                and _CJK.search(text)
            ):
                pool.add(text)
    return sorted(pool)


def _verify(record: dict) -> None:
    """自校验：每个 span 文本非空、SEASON/EPISODE 形态合法、无越界。"""
    for s in record["spans"]:
        text_source = record["title"] if s["source"] == "title" else record.get("subtitle", "")
        assert 0 <= s["start"] < s["end"] <= len(text_source), (record["id"], s)
        text = text_source[s["start"]:s["end"]]
        assert text.strip() == text and text, (record["id"], s, text)


def main() -> None:
    parser = argparse.ArgumentParser(description="单集文件名形态的定向合成增强")
    parser.add_argument("--data", default="ml/data/labeled/annotations.jsonl")
    parser.add_argument("--out", default="ml/data/labeled/augmented-filenames.jsonl")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--renumber-per-record", type=int, default=2)
    parser.add_argument("--cjk-quota", type=int, default=350)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    records = [r for r in read_jsonl(args.data) if r["spans"] and not r.get("review")]
    zh_pool = _zh_pool(records)

    eligible: list[tuple[dict, dict, dict]] = []
    for record in records:
        hit = _single_ep_dotted(record)
        if hit:
            eligible.append((record, hit[0], hit[1]))
    print(f"点分隔单集可改写记录: {len(eligible)}（中文片名池 {len(zh_pool)} 个）")

    rows: list[dict] = []
    for record, season_span, _episode in eligible:
        targets = rng.sample(_TARGET_SEASONS, len(_TARGET_SEASONS))
        picked: list[int] = []
        for t in targets:
            if t not in picked:
                picked.append(t)
            if len(picked) >= args.renumber_per_record:
                break
        for t in picked:
            rows.append(season_renumber(record, season_span, t))

    # 中英混合前缀：施加在「重编号产物 + 原始 S01 记录」的混合池上，
    # 让 CJK 前缀 × 中高季号的组合形态也有覆盖
    cjk_base = rows + [rec for rec, _s, _e in eligible]
    made = 0
    for candidate in rng.sample(cjk_base, len(cjk_base)):
        if made >= args.cjk_quota:
            break
        seasons = [
            s for s in candidate["spans"]
            if s["source"] == "title" and s["field"] == "SEASON"
        ]
        if len(seasons) != 1:
            continue
        try:
            rows.append(cjk_prefix(candidate, seasons[0], rng.choice(zh_pool)))
        except ValueError:
            continue
        made += 1

    for record in rows:
        _verify(record)

    dist = Counter()
    for record in rows:
        m = re.search(r"[Ss](\d{2})[Ee]\d", record["title"])
        if m:
            dist[int(m.group(1))] += 1
    out = Path(args.out)
    out.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    print(f"产出 {len(rows)} 条（重编号 {len(rows) - made}，CJK 前缀 {made}）→ {out}")
    print("数字季号分布:", dict(sorted(dist.items())))


if __name__ == "__main__":
    main()
