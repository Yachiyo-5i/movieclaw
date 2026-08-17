"""扫季确定性回归门禁：点分隔单集文件名的季集抽取必须 100% 全对。

2026-08-17 教训的直接固化：torrent-ner-v2 在「点分隔单集 × S04–S07」上
塌方（同模板扫季 6~9/12，v1 全对），但既有门禁（test 集 F1 / holdout /
130 例回归）全绿放行——因为语料与回归集在这个联合分布上同样稀疏，
指标对盲区不敏感。确定性模板矩阵按构造即真值、成本为零、永不漂移，
任何一格失败即拒绝转正。

    python ml/torrent_ner/sweep_gate.py --model data/models/torrent-ner
    # 退出码 0 = 全对；1 = 存在失败格（打印失败清单）
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

# 模板矩阵：真实世界四种主要形态（纯英文点分隔 / 中文前缀混合 /
# AAC.2.0 式多段音轨尾巴 / 年份前置）。占位符 {s}=季 {e}=集。
_TEMPLATES = [
    "Thirteen.Talks.S{s:02d}E{e:02d}.2021.2160p.WEB-DL.H265.AAC-HHWEB",
    "十三邀.第{cn}季.Thirteen.Talks.S{s:02d}E{e:02d}.2020.2160p.WEB-DL.HEVC.AAC.2.0-HDKWeb",
    "Night.Tales.S{s:02d}E{e:02d}.2026.1080p.WEB-DL.H264.DDP.5.1.Atmos.2Audios-CMCTV",
    "Show.Name.2016.S{s:02d}E{e:02d}.2160p.WEB-DL.H265.DDP2.0-ADWeb",
]
_CN = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六",
       7: "七", 8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二"}
_SEASONS = range(1, 13)
_EPISODES = (1, 2, 5, 9, 10, 11, 12, 24)


def main() -> int:
    parser = argparse.ArgumentParser(description="点分隔单集扫季门禁")
    parser.add_argument("--model", required=True, help="模型目录（MOVIECLAW_NER_DIR）")
    parser.add_argument("--max-failures-shown", type=int, default=12)
    args = parser.parse_args()

    os.environ["MOVIECLAW_NER_DIR"] = str(Path(args.model).resolve())
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    for name in list(sys.modules):
        if name.startswith("movieclaw_enrich"):
            del sys.modules[name]
    enrich_mod = importlib.import_module("movieclaw_enrich")

    total = 0
    failures: list[tuple[str, list, list]] = []
    per_season_fail: dict[int, int] = {}
    for template in _TEMPLATES:
        for s in _SEASONS:
            for e in _EPISODES:
                title = template.format(s=s, e=e, cn=_CN[s])
                attrs = enrich_mod.enrich(title)
                total += 1
                # 判据与消费方一致：集号必须恰好命中；季号候选必须包含真值
                # 且不包含把集号误吸成的假季号
                ok = attrs.episodes == [e] and s in attrs.seasons and e not in [
                    x for x in attrs.seasons if x != s
                ]
                if not ok:
                    failures.append((title, attrs.seasons, attrs.episodes))
                    per_season_fail[s] = per_season_fail.get(s, 0) + 1

    passed = total - len(failures)
    print(f"扫季门禁：{passed}/{total} 通过（模板 {len(_TEMPLATES)} × 季 12 × 集 {len(_EPISODES)}）")
    if failures:
        print("逐季失败数:", dict(sorted(per_season_fail.items())))
        for title, seasons, episodes in failures[: args.max_failures_shown]:
            print(f"  FAIL {title[:64]}  seasons={seasons} episodes={episodes}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
