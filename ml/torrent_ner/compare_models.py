"""两个已导出模型（ONNX 三件套目录）的对称对比评估。

设计目标（docs/design/subtitle-audio-ner.md「转正门禁」）：R9 现役模型与
候选模型走**同一条**编码/解码路径、同一把字符 span 精确匹配的尺子，排除
运行时后处理差异，只度量模型本身的变化。两种模式：

1. eval：在标注文件的指定切分上出逐字段 P/R/F1 + 分类轴准确率（每模型
   只评它 labels.json 里声明的字段——R9 评六字段，候选评八字段）：
     python ml/torrent_ner/compare_models.py eval \\
         --model-a data/models/torrent-ner --model-b ml/artifacts/torrent-ner-onnx \\
         --data ml/data/labeled/annotations.jsonl --split test
2. diff：在无标注池上跑双模型，统计共同字段输出差异率并抽样举例
   （"大量数据对比两个模型变化"的工具化）：
     python ml/torrent_ner/compare_models.py diff \\
         --model-a ... --model-b ... --pool ml/data/raw/nas-bulk.codex.jsonl

解码逻辑与 src/movieclaw_enrich/inference.py 保持一致（BIO 连续段解码、
非法 I- 当新实体、span 平均概率 ≥0.5、分类头 ≥0.5），需 ml/.venv
（onnxruntime + tokenizers）。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from torrent_ner.dataio import read_jsonl, split_of

MIN_SPAN_PROB = 0.5
MIN_CLS_PROB = 0.5


class OnnxModel:
    """导出目录（model.int8.onnx + tokenizer.json + labels.json）的推理封装。"""

    def __init__(self, model_dir: str | Path) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = Path(model_dir)
        self.meta = json.loads((model_dir / "labels.json").read_text(encoding="utf-8"))
        self.labels: list[str] = list(self.meta["labels"])
        self.fields: list[str] = sorted({l[2:] for l in self.labels if l != "O"})
        self.media_types: list[str] = list(self.meta["media_types"])
        self.content_types: list[str] = list(self.meta["content_types"])
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=int(self.meta.get("max_length", 256)))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_dir / "model.int8.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.name = str(model_dir)

    def predict(self, title: str, subtitle: str) -> dict:
        enc = self.tokenizer.encode(title, subtitle or " ")
        inputs = {
            "input_ids": np.array([enc.ids], dtype=np.int64),
            "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
            "token_type_ids": np.array([enc.type_ids], dtype=np.int64),
        }
        span_logits, media_logits, content_logits = self.session.run(None, inputs)
        probs = _softmax(span_logits[0])
        ids = probs.argmax(axis=-1)

        spans = []
        current = None
        for i, seq_id in enumerate(enc.sequence_ids):
            start, end = enc.offsets[i]
            if seq_id is None or start == end:
                current = None
                continue
            tag = self.labels[ids[i]]
            if tag == "O":
                current = None
                continue
            field = tag[2:]
            prob = float(probs[i, ids[i]])
            if tag.startswith("I-") and current and current[0] == seq_id and current[1] == field:
                current[3] = end
                current[4].append(prob)
            else:
                current = [seq_id, field, start, end, [prob]]
                spans.append(current)
        source_names = ("title", "subtitle")
        kept = {
            (source_names[seq_id], field, start, end)
            for seq_id, field, start, end, p in spans
            if sum(p) / len(p) >= MIN_SPAN_PROB
        }

        media_probs = _softmax(media_logits[0])
        content_probs = _softmax(content_logits[0])
        media = self.media_types[int(media_probs.argmax())] if media_probs.max() >= MIN_CLS_PROB else None
        content = self.content_types[int(content_probs.argmax())] if content_probs.max() >= MIN_CLS_PROB else None
        return {"spans": kept, "media_type": media, "content_type": content}


def _softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    e = np.exp(x - x.max(axis=axis, keepdims=True))
    return e / e.sum(axis=axis, keepdims=True)


def gold_spans(item: dict, field: str) -> set:
    return {
        ("title" if s["source"] == "title" else "subtitle", field, s["start"], s["end"])
        for s in item.get("spans", [])
        if s["field"] == field
    }


def cmd_eval(args) -> None:
    models = {"A": OnnxModel(args.model_a), "B": OnnxModel(args.model_b)}
    items = [
        i for i in read_jsonl(args.data)
        if (i.get("split") or split_of(i["id"])) == args.split and not i.get("review")
    ]
    if args.gold_only:
        items = [i for i in items if i.get("dual_agreed")]
    print(f"评估切分 {args.split}：{len(items)} 条（review 已剔除"
          f"{'，仅金标' if args.gold_only else ''}）\n")

    stats: dict[str, dict[str, Counter]] = {k: defaultdict(Counter) for k in models}
    cls_hits: dict[str, Counter] = {k: Counter() for k in models}
    for item in items:
        for key, model in models.items():
            pred = model.predict(item["title"], item.get("subtitle", ""))
            for field in model.fields:
                p = {s for s in pred["spans"] if s[1] == field}
                g = gold_spans(item, field)
                stats[key][field]["tp"] += len(p & g)
                stats[key][field]["fp"] += len(p - g)
                stats[key][field]["fn"] += len(g - p)
            for axis in ("media_type", "content_type"):
                cls_hits[key][axis + "_total"] += 1
                if pred[axis] == item.get(axis):
                    cls_hits[key][axis + "_hit"] += 1

    all_fields = sorted(set(models["A"].fields) | set(models["B"].fields))
    print(f"{'字段':<14} {'A(P/R/F1)':<24} {'B(P/R/F1)':<24} ΔF1")
    for field in all_fields:
        cells = {}
        for key, model in models.items():
            if field not in model.fields:
                cells[key] = ("—", None)
                continue
            c = stats[key][field]
            p = c["tp"] / max(1, c["tp"] + c["fp"])
            r = c["tp"] / max(1, c["tp"] + c["fn"])
            f1 = 2 * p * r / max(1e-9, p + r)
            cells[key] = (f"{p:.3f}/{r:.3f}/{f1:.3f}", f1)
        delta = ""
        if cells["A"][1] is not None and cells["B"][1] is not None:
            delta = f"{cells['B'][1] - cells['A'][1]:+.3f}"
        print(f"{field:<14} {cells['A'][0]:<24} {cells['B'][0]:<24} {delta}")
    for axis in ("media_type", "content_type"):
        row = []
        for key in models:
            hit, total = cls_hits[key][axis + "_hit"], cls_hits[key][axis + "_total"]
            row.append(f"{hit / max(1, total):.3f}")
        print(f"{axis:<14} {row[0]:<24} {row[1]:<24}")


def cmd_diff(args) -> None:
    models = {"A": OnnxModel(args.model_a), "B": OnnxModel(args.model_b)}
    shared_fields = sorted(set(models["A"].fields) & set(models["B"].fields))
    pool = read_jsonl(args.pool)
    if args.limit:
        pool = pool[: args.limit]
    print(f"输出 diff：{len(pool)} 条，共同字段 {shared_fields}\n")

    diff_count: Counter = Counter()
    examples: dict[str, list] = defaultdict(list)
    rng = random.Random(42)
    for item in pool:
        pa = models["A"].predict(item["title"], item.get("subtitle", ""))
        pb = models["B"].predict(item["title"], item.get("subtitle", ""))
        for field in shared_fields:
            sa = {s for s in pa["spans"] if s[1] == field}
            sb = {s for s in pb["spans"] if s[1] == field}
            if sa != sb:
                diff_count[field] += 1
                if len(examples[field]) < 5:
                    texts = {"title": item["title"], "subtitle": item.get("subtitle", "")}
                    render = lambda ss: sorted(texts[src][a:b] for src, _f, a, b in ss)
                    examples[field].append((item["id"], render(sa), render(sb)))
        for axis in ("media_type", "content_type"):
            if pa[axis] != pb[axis]:
                diff_count[axis] += 1

    total = len(pool)
    print(f"{'维度':<14} 差异条数  差异率")
    for key in shared_fields + ["media_type", "content_type"]:
        print(f"{key:<14} {diff_count[key]:<8} {diff_count[key] / max(1, total):.1%}")
    print("\n各字段差异抽样（A → B）：")
    for field, rows in examples.items():
        print(f"[{field}]")
        for rid, a, b in rows:
            print(f"  {rid}: {a} → {b}")
    _ = rng


def main() -> None:
    parser = argparse.ArgumentParser(description="双模型对称对比")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("eval", cmd_eval), ("diff", cmd_diff)):
        p = sub.add_parser(name)
        p.add_argument("--model-a", required=True, help="基线模型目录（如现役 R9）")
        p.add_argument("--model-b", required=True, help="候选模型目录")
        if name == "eval":
            p.add_argument("--data", default="ml/data/labeled/annotations.jsonl")
            p.add_argument("--split", default="test",
                           choices=("train", "dev", "test", "holdout"))
            p.add_argument("--gold-only", action="store_true", help="只用双引擎一致的金标样本")
        else:
            p.add_argument("--pool", required=True, help="无标注池 JSONL（id/title/subtitle）")
            p.add_argument("--limit", type=int, default=0)
        p.set_defaults(fn=fn)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
