"""双引擎分歧的 fable5 双盲批量裁决：两轮一致自动定案，其余留 review。

分歧消解金字塔的 Tier 1（设计见 docs/design/subtitle-audio-ner.md）：
- 输入两份标注文件（跑过 normalize_labels.py 之后），找出逐字段 span 分歧；
- 按批送终审模型（默认 claude-fable-5）双盲裁决：候选随机匿名为 A/B，
  附规范条款；独立跑两轮；
- 两轮一致 → 败方文件该字段的 spans 改写为胜方结果（两文件就此一致，
  后续 merge_dual 自然转正），记录 adjudication 日志；
- verdict=N（两候选皆错）或两轮不一致 → 不动，留给 merge_dual 打 review。

    python ml/torrent_ner/adjudicate.py \\
        --a ml/data/labeled/expand-ann.codex.jsonl \\
        --b ml/data/labeled/expand-ann.claude.jsonl \\
        --log ml/data/labeled/adjudications.jsonl

幂等：已在日志中定案的 (id, field) 跳过，可中断重跑。
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from torrent_ner.dataio import read_jsonl, split_of
from torrent_ner.labels import FIELDS
from torrent_ner.merge_dual import span_set

SPEC = """\
裁决依据（标注规范 v13 条款摘录）：
- 片名/别名：原文逐字子串；别名逐个列出；变体（续集编号/标点/大小写/点号场景名）各自完整
  抽出不裁不并；片名尾部地区括注不入；频道前缀（Jade/ViuTV 等）不入；含"剧场版/續編/
  总集篇"等类型标记的变体不标只标干净别名——但官方名本身含类型词时按官方名整体抽；
  章节小标题与内容描述串（"案件A+案件B"）不是别名；翻拍/原作引用（"X(1987) 翻拍版"）的
  原作名与原作年份都不抽；明显被截断的残名（尾部 ".."）不抽；
- 日文/韩文名一律归 title_en（含纯汉字/假名写法）；
- YEAR：发行年份字样；年份仅作片名一部分（Reply 1988）不算；8 位日期串与档期表达不抽；
  独立年份 token（含点号命名内）抽；纯英文完结词 Complete/Fin 不属于 EPISODE_TOTAL；
- SUBTITLE/AUDIO：声明整段抽内部不拆；连写标签拆轴（"国语中字"、"国配简繁字幕"的"国配"）；
  独立成标签的语言词每次出现都抽；无语言信息的音轨数量词不抽；
- 零编造：值必须能在原文原样定位；漏抽可接受，编造不可接受。
- media_type（结构轴）：movie 单部 / series 分集（同一作品多季全集打包也算）/
  collection 多部独立作品打包 / other 非影视；content_type（题材轴）：anime/
  documentary/variety/music/other，两轴正交（动漫剧场版 = movie+anime），
  儿童向卡通、国漫、日漫都算 anime；普通真人影视归 other。
"""

HEADER = ("你是影视种子命名标注的终审裁判。以下每个 case 给出种子原文、争议字段与两个"
          "标注候选 A/B。逐 case 裁决：\"A\"、\"B\" 或 \"N\"（都不对）。只依据规范与原文。\n\n"
          + SPEC +
          "\n只输出 JSON 数组，元素形如 {\"case\": 1, \"verdict\": \"A\", \"reason\": \"一句话\"}。\n\n待裁决 cases：\n")


def call_judge(prompt: str, model: str) -> list[dict]:
    r = subprocess.run(["claude", "-p", "--model", model],
                       input=prompt, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"裁决调用失败: {r.stderr[:300]}")
    text = r.stdout
    start = text.find("[")
    if start < 0:
        raise ValueError(f"裁决输出无 JSON: {text[:200]}")
    # raw_decode 只吃第一个完整数组，容忍数组后的任何杂尾（说明文字/围栏）
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(data, list):
        raise ValueError("裁决输出不是 JSON 数组")
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description="双引擎分歧批量裁决")
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--log", default="ml/data/labeled/adjudications.jsonl")
    ap.add_argument("--model", default="claude-fable-5")
    ap.add_argument("--batch", type=int, default=15)
    ap.add_argument("--limit", type=int, default=0, help="本次最多裁决多少个分歧（0=不限）")
    ap.add_argument("--workers", type=int, default=1, help="并行裁决批次数")
    ap.add_argument("--rounds", type=int, default=2, choices=(1, 2),
                    help="每批裁决轮数：2=双盲双轮（金标层）；1=单轮直接定案"
                         "（train 层，先用双轮抽样确认一致率 ≥95% 再降级）")
    ap.add_argument("--splits", default="",
                    help="只裁指定切分的样本（逗号分隔，如 dev,test）；空=全部")
    ap.add_argument("--fields", default="",
                    help="只裁指定字段（逗号分隔，如 SUBTITLE,AUDIO）；空=全部")
    args = ap.parse_args()
    split_filter = set(args.splits.split(",")) if args.splits else None
    field_filter = set(args.fields.split(",")) if args.fields else None

    a_recs = {r["id"]: r for r in read_jsonl(args.a)}
    b_recs = {r["id"]: r for r in read_jsonl(args.b)}
    log_path = Path(args.log)
    done = set()
    replayed = 0
    if log_path.exists():
        # 日志是裁决的事实源：启动时把已定案条目重放到内存记录，
        # 保证任何中断（超时/熔断）后续跑不丢判决——文件写回只是缓存
        for r in read_jsonl(log_path):
            done.add((r["id"], r["field"]))
            if r.get("verdict") in ("a", "b") and r["id"] in a_recs and r["id"] in b_recs:
                for target in (a_recs, b_recs):
                    rec = target[r["id"]]
                    if r["field"] in ("media_type", "content_type"):
                        if "win_value" in r:
                            rec[r["field"]] = r["win_value"]
                    elif "win_spans" in r:
                        rec["spans"] = [s for s in rec["spans"]
                                        if s["field"] != r["field"]] + [dict(s) for s in r["win_spans"]]
                        rec["spans"].sort(key=lambda s: (s["source"], s["start"]))
                replayed += 1
    if replayed:
        print(f"重放历史判决 {replayed} 条")

    rng = random.Random(7)
    disputes = []
    for rid in sorted(set(a_recs) & set(b_recs)):
        if split_filter and split_of(rid) not in split_filter:
            continue
        ra, rb = a_recs[rid], b_recs[rid]
        texts = {"title": ra["title"], "subtitle": ra.get("subtitle", "")}
        for f in FIELDS:
            if field_filter and f not in field_filter:
                continue
            if (rid, f) in done or span_set(ra, f) == span_set(rb, f):
                continue
            va = sorted(texts[s][st:en] for s, st, en in span_set(ra, f))
            vb = sorted(texts[s][st:en] for s, st, en in span_set(rb, f))
            disputes.append({"id": rid, "field": f, "va": va, "vb": vb,
                             "title": ra["title"], "subtitle": ra.get("subtitle", "")})
        # 两条分类轴同样裁决（值为字符串而非 span 列表）
        for f in ("media_type", "content_type"):
            if field_filter and f not in field_filter:
                continue
            if (rid, f) in done or ra.get(f) == rb.get(f):
                continue
            disputes.append({"id": rid, "field": f, "va": ra.get(f), "vb": rb.get(f),
                             "title": ra["title"], "subtitle": ra.get("subtitle", "")})
    if args.limit:
        disputes = disputes[: args.limit]
    print(f"待裁决分歧 {len(disputes)} 个（已定案 {len(done)} 个）")

    settled = unsettled = 0
    batches = []
    for i in range(0, len(disputes), args.batch):
        batch = disputes[i:i + args.batch]
        flips = [rng.random() < 0.5 for _ in batch]
        batches.append((batch, flips))

    def run_batch(batch, flips):
        """两轮独立裁决：第二轮 A/B 互换 + case 顺序重排，避免只测模型自我一致性。

        返回 [round1, round2]，每轮为 {批内原始位置: {"verdict": a/b/N, "reason": ...}}，
        verdict 已按该轮的排列/翻转还原到 a=主标注器、b=副标注器。"""
        rng2 = random.Random(sum(ord(ch) for ch in batch[0]["id"]))
        order2 = list(range(len(batch)))
        rng2.shuffle(order2)
        plans = [
            (list(range(len(batch))), flips),
            (order2, [not f for f in flips]),
        ][: args.rounds]
        results = []
        for order, fl in plans:
            payload = "\n".join(
                f"case {n + 1} [{batch[i]['field']}]\n"
                f"  title: {batch[i]['title']}\n  subtitle: {batch[i]['subtitle']}\n"
                f"  A: {json.dumps(batch[i]['vb'] if fl[i] else batch[i]['va'], ensure_ascii=False)}\n"
                f"  B: {json.dumps(batch[i]['va'] if fl[i] else batch[i]['vb'], ensure_ascii=False)}"
                for n, i in enumerate(order)
            )
            by_case = {int(e["case"]): e for e in call_judge(HEADER + payload, args.model)}
            norm = {}
            for n, i in enumerate(order):
                e = by_case.get(n + 1, {})
                v = e.get("verdict")
                if v in ("A", "B"):
                    v = ("b" if v == "A" else "a") if fl[i] else ("a" if v == "A" else "b")
                norm[i] = {"verdict": v, "reason": e.get("reason", "")}
            results.append(norm)
        return results

    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    futures = {pool.submit(run_batch, b, f): (b, f) for b, f in batches}
    done_batches = 0
    for future in as_completed(futures):
        batch, flips = futures[future]
        done_batches += 1
        try:
            rounds = future.result()
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"批失败跳过: {str(exc)[:200]}")
            continue
        if args.rounds == 1:
            rounds = rounds + rounds  # 单轮模式：r2 视同 r1，直接定案
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as logf:
            for n, c in enumerate(batch):
                r1 = rounds[0].get(n, {}).get("verdict")
                r2 = rounds[1].get(n, {}).get("verdict")
                entry = {"id": c["id"], "field": c["field"], "r1": r1, "r2": r2,
                         "reason": rounds[0].get(n, {}).get("reason", ""),
                         "adjudicated_by": f"{args.model}-{args.rounds}r-indep",
                         "adjudicated_at": now}
                if r1 == r2 and r1 in ("a", "b"):
                    winner, loser = (a_recs, b_recs) if r1 == "a" else (b_recs, a_recs)
                    lose_rec = loser[c["id"]]
                    if c["field"] in ("media_type", "content_type"):
                        win_value = winner[c["id"]].get(c["field"])
                        lose_rec[c["field"]] = win_value
                        entry["win_value"] = win_value
                    else:
                        win_spans = [s for s in winner[c["id"]]["spans"]
                                     if s["field"] == c["field"]]
                        lose_rec["spans"] = [s for s in lose_rec["spans"]
                                             if s["field"] != c["field"]] + win_spans
                        lose_rec["spans"].sort(key=lambda s: (s["source"], s["start"]))
                        entry["win_spans"] = win_spans
                    entry["verdict"] = r1
                    settled += 1
                else:
                    entry["verdict"] = "unsettled"
                    unsettled += 1
                logf.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"批 {done_batches}/{len(batches)} 完成（累计定案 {settled}，未决 {unsettled}）")
    pool.shutdown()

    for path, recs in ((args.a, a_recs), (args.b, b_recs)):
        with open(path, "w", encoding="utf-8") as f:
            for r in recs.values():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"完成：定案 {settled}（已写回两份文件），未决 {unsettled}（留给 merge review）")


if __name__ == "__main__":
    main()
