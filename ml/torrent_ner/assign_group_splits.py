"""内容指纹分组切分：防止跨站转载的同一 release 泄漏进不同切分。

问题（训练前审查 5.1）：不同站点转载同一资源，(site_id, torrent_id) 哈希切分
无法阻止近重复文本跨 train/dev/test。方案：对归一化后的 (title, subtitle)
计算指纹，同指纹的全部记录作为一个 group，按**指纹哈希**统一分配切分，
以显式 ``split`` 字段写入记录（dataio.load_split 优先读取）。原始文本不变，
指纹只用于分组。

    python ml/torrent_ner/assign_group_splits.py <标注文件...>

对传入的全部文件做**全局**分组（跨文件同指纹也统一），就地写回。
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from torrent_ner.dataio import split_of

_WS_RE = re.compile(r"[\s._\-|/]+")


def fingerprint(title: str, subtitle: str) -> str:
    """NFKC + 大小写折叠 + 折叠空白与常见场景分隔符。仅用于分组，不改原文。"""
    text = unicodedata.normalize("NFKC", f"{title}\n{subtitle}").casefold()
    return _WS_RE.sub(" ", text).strip()


def main() -> None:
    paths = sys.argv[1:]
    all_recs: list[tuple[str, dict]] = []
    for path in paths:
        for line in open(path, encoding="utf-8"):
            all_recs.append((path, json.loads(line)))

    groups: dict[str, list[dict]] = {}
    for _, r in all_recs:
        groups.setdefault(fingerprint(r["title"], r.get("subtitle", "")), []).append(r)

    stats = Counter()
    for fp, members in groups.items():
        # 同一样本会出现在多个标注文件里——多成员按**不同 id** 数判定；
        # 只有不同 id 按各自哈希落进不同切分（真泄漏）的组才强制归一到
        # split_of(指纹)；其余一律清除显式 split 回落 id 哈希（保持历史切分稳定）
        id_splits = {split_of(rid) for rid in {r["id"] for r in members}}
        if len(id_splits) > 1:
            target = split_of(fp)
            stats["leak_groups_fixed"] += 1
            for r in members:
                if split_of(r["id"]) != target:
                    stats["records_moved"] += 1
                r["split"] = target
        else:
            for r in members:
                r.pop("split", None)

    by_path: dict[str, list[dict]] = {}
    for path, r in all_recs:
        by_path.setdefault(path, []).append(r)
    for path, recs in by_path.items():
        with open(path, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    splits = Counter((r.get("split") or split_of(r["id"])) for _, r in all_recs)
    print(f"分组 {len(groups)} 个（记录 {len(all_recs)}）；"
          f"修复跨切分泄漏组 {stats['leak_groups_fixed']} 个，移动记录 {stats['records_moved']} 条")
    print(f"切分分布: {dict(splits)}")


if __name__ == "__main__":
    main()
