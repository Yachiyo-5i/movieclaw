"""用大模型 CLI 无头模式批量蒸馏标注，产出字符级 span 标注数据。

支持两个引擎（同一提示词、同一解析逻辑，标注规范保持一致）：
- claude：调 `claude -p`（默认 claude-sonnet-5）
- codex： 调 `codex exec`（默认走 ~/.codex/config.toml 的 model，当前 gpt-5.6-sol）

设计：让大模型只负责"抽取原文子串"（它擅长），字符偏移由本脚本用字符串
定位计算（代码擅长）——绝不让模型报数字偏移。定位失败的样本带 review
标记落盘，后续人工复核；这批"模型和定位器意见不合"的样本恰是标注价值
最高的病例。

断点续标：输出文件里已有的 id 自动跳过，可随时中断重跑；两个引擎写同一
输出文件，也可各标一部分（双引擎交叉验证见 README 迭代守则）。

    python ml/torrent_ner/annotate.py                        # claude 全量
    python ml/torrent_ner/annotate.py --engine codex --limit 50   # codex 小批试跑
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from torrent_ner.dataio import append_jsonl, read_jsonl
from torrent_ner.encoding import resolve_overlaps
from torrent_ner.labels import CONTENT_TYPES, MEDIA_TYPES

# 标注规范版本：随每条记录落盘。改提示词必须 +1，训练前可按版本过滤/重标。
# v2：新增"零编造"强约束（强化既有的逐字子串要求，未改判定规则，故 v1 数据仍兼容）
# v3：episode 拆成 episode（当前集）+ episode_total（总集数）——标签集变了，
#     v1/v2 的 episode 标注属旧 schema，全量标注前应清空重标（见 README）
# v4：新增整条分类 media_type（movie/series/other）——document 级判定，非 span
# v5：新增内容轴 content_type（真人/动漫/综艺/纪录片/其他），与 media_type 正交
# v6：content_type 精简为 4 类，去掉 live_action（普通真人影视归 other，靠 media_type 识别）
# v7：content_type 增加 music（专辑/MV/演唱会）——PT 无损音乐区是大类，从 other 分出
# v8：交叉质检暴露的两个规范空白（增量澄清，v7 未定义的行为，存量 v7 数据无需重标）：
#     ①成人影片：视为影视，番号作 title_en 抽出，演员名不抽，movie/other；
#     ②综艺子栏目/特辑名（"训练日记"等）不算别名，不入 title_zh
# v9：线上错例暴露的三条边界规则（增量澄清）：①发布标签词绝不入片名；
#     ②合集名整体抽取不截断（含人名）；③花絮/EXTRAS 只抽正片名
# v10：①media_type 新增 collection（多部独立作品打包）——schema 变更，存量
#     合集类样本须重标（其余样本的 movie/series/other 语义不变）；
#     ②版本描述词（IMAX版/加长版/导演剪辑…）不入片名
# v11：外文轴语义澄清（增量）：title_en = 一切非中文原名——日文名（含纯汉字
#     写法如"劇場版「鬼滅の刃」"）、韩文名都归 title_en，不入 title_zh
# v12：新增 subtitle_decl / audio_decl 两个 span 字段（字幕/音轨声明两轴）——
#     schema 变更；六个老字段语义零改动，存量数据只需 --delta 补标两个新字段。
#     圈定十一则经 13 桶 66 条真实样本四轮双引擎实测收敛（终验一致率 100%），
#     迭代过程与设计见 docs/design/subtitle-audio-ner.md
# v13：分歧驱动的增量澄清（130 条 v12 双引擎分歧样本复标验证，收敛 92.6%；
#     终审判例经 fable5 双盲两轮裁决）：①英文完结词 Complete/Fin 不抽（归正则
#     通道）；②8 位日期串与档期表达里的年份不抽，独立年份 token 抽，合集多部
#     年份全抽；③季表达两段双写都列出，Season N 也算；④别名变体不裁不并、
#     斜杠列表逐个全抽；⑤场景点号名始终抽、频道前缀不入；⑥地区括注不入片名；
#     ⑦翻拍/原作引用的名与年份不抽；⑧截断残名不抽；⑨内容描述串不是别名；
#     ⑩類型标记词表补録 続編/続篇；⑪开头标签堆叠的声明每次都抽（新字段强调）。
#     存量 v12 数据经 normalize_labels.py 确定性归一 + adjudicate.py 裁决收敛。
PROMPT_VERSION = 13

# 字幕/音轨声明两轴的规范文本：全量提示词与 --delta 补标提示词共用这一份，
# 避免两处维护漂移。改动此文本 = 改动标注标准，须 PROMPT_VERSION+1。
DECL_SPEC = """\
- subtitle_decl: **字幕声明片段**列表——原文中谈论字幕的连续片段，每段**整体**抽出、
  内部不拆（"内封简繁英SUP特效字幕" 整段一个值）。三类都要抽：
  ①肯定声明（"简体中文字幕"、"简繁字幕"、"硬字幕"、"SUP字幕"）；
  ②否定声明（"无字幕"、"字幕：无"——含否定词整段抽出）；
  ③泛称（"中字"、"中文字幕"——不确定简繁也照抽，判断简繁不是你的任务）
- audio_decl: **音轨/配音语言声明片段**列表——两种形态：
  ①语言词+配音/音轨/语音字样（"国语配音"、"粤语音轨" 整段抽出）；
  ②独立成标签的语言词（"[国语]" 抽 "国语"、"*日语*" 抽 "日语"、
  "官方 国语 中字" 抽 "国语"、"粤语中字" 抽 "粤语"）

字幕/音轨圈定十一则（**开头标签堆叠里的独立声明每次都要抽**，即使正文另有更完整声明——
"官方 国语 中字 …… 国粤双语/简繁中字" 要抽 "国语"、"中字"、"国粤双语"、"简繁中字" 四个值；
"国配简繁双语四字幕" 按拆轴规则抽 audio_decl "国配"）：
- 这两个字段只圈**语言/载体/有无**，绝不圈音频编码与声道：DDP5.1、DTS-HD MA、Atmos、
  AAC、FLAC、2.0、5.1 等技术词一个都不能入，即使它们紧贴语言词（"国语 DDP5.1" 只抽 "国语"）；
- 连写标签拆轴："国语中字" → audio_decl 抽 "国语"、subtitle_decl 抽 "中字"；
  斜杠混排段按语义切："国语/简繁英等多国软字幕" → "国语" 入 audio_decl、
  "简繁英等多国软字幕" 入 subtitle_decl；
- "双语/双字幕"归轴看伴随词：含简/繁等**字形**词的是字幕（"简英双语"、"简繁双语四字幕"
  → subtitle_decl）；含国/粤或语种对且无字幕尾词的是音轨（"国粤双语"、"中英双语"、
  "国日双音轨" → audio_decl）；两轴证据同时出现时各归各轴分开抽；
- 字幕组/发布组名不是字幕声明："XX字幕组"、压制组后缀不入 subtitle_decl
  （"包含字幕组水印"谈的是水印也不入）；
- **不含语言信息**的音轨数量/规格词不抽："评论音轨"、"多规格音轨"、"双音轨"、
  "2Audios/3Audios" 都没有语言，无法归入语言声明；语言词在旁边时只抽语言部分
  （"英语+杜比全景声·多规格音轨" 只抽 "英语"）；
- 信息栏的语言元数据列表视为**原声语言声明**，逐项抽入 audio_decl
  （"| 日语 / 英语 / 粤语 |" 抽 "日语"、"英语"、"粤语" 三项）；
  紧邻的制片国家/地区（"日本 / 马来西亚"）是国家不是语言，绝不能抽；
- 语言词必须是对**本资源音轨或字幕**的声明才抽：片名里的语言字样（"粤语残片修复计划"）、
  剧情简介里的语言描述不抽；
- 竖线/斜杠等分隔符连写的多语言段**整段抽出**，绝不拆成单字或单项
  （"国|英音轨" 整段一个值，不拆 "国" 和 "英音轨"；"意大利|英音轨" 同理；
  "国粤英三语" 整段）——只有独立分隔的语言列表（"| 日语 / 英语 |"）才逐项抽；
- 发行流程/来源词不作 span 开头：原盘、DIY、官方、新增、独家、首发等词不入值，
  值从第一个**语言词或载体形态词**（内封/内嵌/外挂/硬/软/特效/SUP）开始
  （"原盘DIY简繁双语四字幕" 抽 "简繁双语四字幕"、"新增简繁双语特效字幕"
  抽 "简繁双语特效字幕"）；
- **孤立的形态词不抽**：特效/内封/内嵌/外挂/硬/软 单独出现（如标签堆叠
  "官方 中字 特效 …"里的裸"特效"）时不是完整声明，不抽；它们只有与字幕词
  或语言词**连写**时才随整段入值（"特效字幕"、"内封简中"可抽）；
- 这两个字段同样受零编造铁律约束：值必须能在原文逐字定位，抽不出就给空列表。
"""

# 标注规范：改动此提示词 = 改动标注标准，须 PROMPT_VERSION+1 并全量重标或版本隔离
# （六字段语义不变、只新增字段时，存量可走 --delta 增量补标，见 v12）
PROMPT_HEADER = """\
你是影视种子命名解析专家。下面是若干 PT 站种子，每条有英文种子名 title 和中文副标题 subtitle。
对每条抽取以下信息，全部必须是 title 或 subtitle 中**逐字连续出现的子串**（保留点号、空格等原始写法），抽不到就给空值：

- title_en: **一切非中文**片名及别名列表——英文、日文（含纯汉字写法："劇場版 鬼滅の刃"、
  "無限列車編" 是日文名不是中文名，靠假名/「」/日式用字判断）、韩文、其它语言都归这里
  （不含年份、分辨率等技术词、压制组；"Working.Girls.1986.1080p..." 取 "Working.Girls"）
- title_zh: 简体/繁体**中文**片名及别名列表（含港台译名，逐个列出；不含季/集字样，
  "神墓 第三季" 只取 "神墓"）
- year: 发行年份字样（如 "2025"）。年份仅作为片名一部分出现时（如 Reply 1988、"Cold War 1994"
  的 1994）给 null；**8 位日期串（"20260724"）是播出日期不从中抽年份**；"2023年9月"、
  "2026年7月新番"等档期表达里的年份不抽；翻拍/原作引用（"黑寡妇(1987) 翻拍版"）的原作年份
  不抽；独立出现的 4 位年份 token（含点号命名里的 ".1963."）照抽；多部合集每部片名后的年份全抽
- season: 季数表达列表（如 "S01"、"第三季"、"Season 7"；"S01E10" 拆出 "S01"）。
  **title 和 subtitle 各自出现的季表达都要列出**（"S07" 与 "第七季" 是两个值）
- episode: **当前集号 / 集号区间**列表（如 "E10"、"第50集"、"EP12"、"E01-E12"；"S01E10" 拆出 "E10"）——指这个种子是第几集
- episode_total: **总集数 / 完结合集**列表（如 "全12集"、"12集全"、"全26话"）——指这是共 N 集的合集。判断依据是"全/共/…全"等完结语，与 episode 语义不同，别混。
  **纯英文完结词 Complete/COMPLETE/Fin 不抽**（固定词表由下游正则识别，不是本任务范围）
""" + DECL_SPEC + """
另外给整条种子两个类别（这不是子串，是对整体的判断；两者相互独立，各判各的）：
- media_type（结构轴）: "movie" | "series" | "collection" | "other" 四选一
    · movie      单部影片：电影、剧场版、纪录片单片
    · series     分集连续作品：电视剧、剧集、综艺、番剧；**同一作品**的多季/全集打包也算
    · collection **多部独立作品的打包**："六部合集"、导演电影合集、系列电影全集
      （X战警六部曲）、混装作品包——判据是"包含多部各自独立的作品"
    · other      非影视：软件、音乐专辑、体育赛事、游戏、电子书、MV
  判定依据是**作品结构**，不是题材。collection 的片名 = 合集名整体。
- content_type（内容轴）: "anime" | "documentary" | "variety" | "music" | "other" 五选一
    · anime       动画/动漫（番、剧场版、OVA、字幕社、日本动画等线索）
    · documentary 纪录片（纪录、探索、BBC、NHK 等）
    · variety     综艺/真人秀（"第N期"用"期"、综艺、主持等）
    · music       音乐：专辑、单曲、MV、演唱会/Live（FLAC/APE/DSD/24bit 等无损音频线索）
    · other       其余全部：普通真人电影/电视剧、软件/体育/游戏/电子书等
  只标出上述特殊题材，其它一律 other（是不是影视由 media_type 区分）。
  music 的 media_type 按结构判：纯音频专辑 → other，演唱会/MV 影像 → 按结构（通常 movie）。
  两轴正交：动漫剧场版 = movie + anime，别把题材塞进 media_type。

【零编造铁律】只能从给定字符串里**原样复制**子串，绝不允许翻译、补全缩写、
纠正拼写、把别名改成规范名、或凭常识补充原文没写的信息。某字段原文没明确写出
就留空——漏抽可接受，编造绝不可接受。你输出的每个值都必须能在 title 或
subtitle 里用「查找」原封不动定位到，否则该值会被判为无效丢弃。

注意：种子可能不是影视内容（软件、音乐专辑、游戏、电子书等）。非影视内容
片名等字段一律给空值——教模型对这类输入保持沉默正是训练目的之一；但
subtitle_decl / audio_decl 例外：演唱会/MV 等只要原文声明了就照抽。
片名边界十二铁律：
- **别名变体各自完整抽出，绝不裁剪或归并**：含续集编号的变体按原文抽（"VIVANT 2" 不裁成
  "VIVANT"；"二龙湖·村暖花开3" 整体抽）；标点/大小写不同的写法各算一个值（"Draw This,
  Then Die" 与 "Draw This, Then Die!" 都抽）；官方全名含副题的整体抽（"暴走千金立誓复仇。
  ～用魔导书之力碾碎祖国～"）；斜杠别名列表**逐个全部**列出，一个都不能漏；
- **title 段的场景点号名始终抽为 title_en**（含拼音写法 "Zhan.Du.Xing.Dong"，即使副标题
  已有其它写法；点号版与空格版是两个变体各自抽）；频道/平台前缀（Jade/ViuTV/HOY/RTHK 等）
  不是片名的一部分（"Jade Deadly Sins" 只抽 "Deadly Sins"）；
- **片名尾部的地区/版本括注不入片名**："(港)"、"(台)"、"(美版)"、"(韩国版)"等括注是地区
  标记不是名称——"黑衣淑女(台)" 只抽 "黑衣淑女"；
- **翻拍/原作引用不是本作别名**："/黑寡妇(1987) 翻拍版/" 说的是被翻拍的原作，原作名与
  原作年份都不抽；
- **明显被截断的残名不抽**（站点截断导致尾部残缺如 "神探白朗2：抽丝.."）；
- **别名位置的内容描述串不是别名**："珠宝行连环劫案+卞记旅馆灭门案" 是案件内容描述，
  与章节小标题同理不入片名；
- **片名末尾的句号/点号不入片名**（。．.）——即使官方名含句号（"你的名字。"），
  检索用名不带；点号命名里中文贴写同理（"你的名字.Your.Name" 抽 "你的名字"）。
  注意：感叹号/问号是风格的一部分要保留（"Do It Yourself!!" 全抽）；
- **别名变体择干净**：同一作品多个别名中，含"剧场版/總集篇/总集篇/完结篇/特别篇/
  OVA/続編/続篇"等类型标记词的变体**不标**，只标不含标记的干净别名（"进击的巨人剧场版：
  完结篇·最后的进击 / 进击的巨人：最后的进击" 只标后者；"GTO 続編" 只抽 "GTO"）；
  若所有别名都含标记，标最短的那个；官方名**本身**含类型词时按官方名整体抽
  （"鬼灭之刃 剧场版 无限列车篇"）；
- **章节小标题不入片名**："第X章/第X部/第X夜"及其后的小标题是分章信息
  （"鬼灭之刃：无限城篇 第一章 猗窝座再袭" 只抽 "鬼灭之刃：无限城篇"），
  章节表达抽入 episode；
- **版本描述词不入片名**：IMAX版/加长版/导演剪辑版/未删减版/修复版/重制版/3D版/
  杜比视界版/剧场版重映 等版本字样是发行属性不是名称——"星际穿越 IMAX版"只抽
  "星际穿越"（注意"剧场版"作为动漫电影类型词时同理不入片名，除非官方名就含它）；
- 发布标签词（官方/国语/中字/限转/禁转/独占/特效/应求/首发/DIY/粤语/双语/高清/杜比/
  活动置顶 等，常堆叠在副标题开头或片名两侧）**永远不是片名的一部分**：片名从真实
  名称的第一个字开始、最后一个字结束，一个标签字都不能带，也绝不能因剔除标签而
  误切片名本身的字（这些标签词该入 subtitle_decl / audio_decl 的另行抽入）；
- 合集资源（"XX执导电影合集"、"Collection"）：合集名称**整体**作为片名抽出，其中的
  人名是名称的一部分不要截断；电影合集 media_type=movie；
- 花絮/特典（EXTRAS/幕后花絮/特典/SP）：只抽正片名称（"星际穿越 幕后花絮"只抽
  "星际穿越"），描述词不入片名。

两个特殊规则：
- 成人影片（如 "IPZZ-034 ..."）视为影视：番号本身作为 title_en 抽出（如 "IPZZ-034"），
  有作品名也一并抽，演员名不抽；media_type=movie，content_type=other；
- 综艺的子栏目/特辑名（如 "训练日记"、"会员版"、"纯享版"）不是别名，不要抽入
  title_zh；只抽节目主名和"又名"类真正的别名。

只输出 JSON 数组，不要任何其他文字。每个元素形如：
{"id": "...", "title_en": [], "title_zh": [], "year": null, "season": [], "episode": [], "episode_total": [], "subtitle_decl": [], "audio_decl": [], "media_type": "movie", "content_type": "other"}

待标注数据：
"""

# --delta 补标提示词：存量数据只补 subtitle_decl / audio_decl 两个新字段。
# 规范正文与全量提示词共用 DECL_SPEC，版本同随 PROMPT_VERSION。
DELTA_PROMPT_HEADER = """\
你是影视种子命名解析专家。下面是若干 PT 站种子，每条有英文种子名 title 和中文副标题 subtitle。
对每条**只**抽取字幕与音轨两类声明，值必须是 title 或 subtitle 中**逐字连续出现的子串**
（保留点号、空格等原始写法），抽不到就给空列表：

""" + DECL_SPEC + """
【零编造铁律】只能从给定字符串里**原样复制**子串，绝不允许翻译、补全缩写、
纠正拼写、或凭常识补充原文没写的信息。抽不到就给空列表——漏抽可接受，
编造绝不可接受。你输出的每个值都必须能在 title 或 subtitle 里用「查找」
原封不动定位到，否则该值会被判为无效丢弃。

注意：种子可能不是影视内容（软件、音乐专辑、游戏、电子书等）——不影响本任务，
演唱会/MV 等只要原文声明了字幕/音轨就照抽。

只输出 JSON 数组，不要任何其他文字。每个元素形如：
{"id": "...", "subtitle_decl": [], "audio_decl": []}

待标注数据：
"""

# 各字段在两个来源里的检索顺序（先命中主要来源，其余来源也全部标注）
FIELD_KEYS = (
    "title_en", "title_zh", "year", "season", "episode", "episode_total",
    "subtitle_decl", "audio_decl",
)
FIELD_LABEL = {
    "title_en": "TITLE_EN",
    "title_zh": "TITLE_ZH",
    "year": "YEAR",
    "season": "SEASON",
    "episode": "EPISODE",
    "episode_total": "EPISODE_TOTAL",
    "subtitle_decl": "SUBTITLE",
    "audio_decl": "AUDIO",
}
# --delta 模式只定位这两个新字段
DELTA_FIELD_KEYS = ("subtitle_decl", "audio_decl")


def find_codex() -> str:
    """定位 codex 二进制：先查 PATH，再兜底 homebrew node 的全局 npm bin。"""
    import glob
    import shutil

    path = shutil.which("codex")
    if path:
        return path
    for candidate in sorted(glob.glob("/opt/homebrew/Cellar/node/*/bin/codex"), reverse=True):
        return candidate
    raise RuntimeError("找不到 codex CLI，请先 npm install -g @openai/codex")


def call_claude(prompt: str, model: str | None) -> str:
    """调用 claude CLI 无头模式，返回原始文本输出。"""
    result = subprocess.run(
        ["claude", "-p", "--model", model or "claude-sonnet-5"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI 调用失败: {result.stderr.strip()[:500]}")
    return result.stdout


def call_codex(prompt: str, model: str | None) -> str:
    """调用 codex exec 无头模式。最终回复经 -o 文件取回（stdout 混有进度噪音）。
    不传 --model 时用 ~/.codex/config.toml 里的默认模型。"""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="r", suffix=".txt", delete=False) as tmp:
        out_path = tmp.name
    cmd = [
        find_codex(),
        "exec",
        "--ephemeral",           # 标注调用不留会话记录
        "--skip-git-repo-check",
        "-s", "read-only",       # 纯文本任务，禁写盘
        "-o", out_path,
        "-",                     # 提示词走 stdin，避免参数长度限制
    ]
    if model:
        cmd[2:2] = ["--model", model]
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600)
    try:
        output = Path(out_path).read_text(encoding="utf-8")
    finally:
        Path(out_path).unlink(missing_ok=True)
    if result.returncode != 0 or not output.strip():
        raise RuntimeError(
            f"codex CLI 调用失败(exit={result.returncode}): {result.stderr.strip()[:500]}"
        )
    return output


ENGINES = {"claude": call_claude, "codex": call_codex}


def codex_default_model() -> str:
    """读 ~/.codex/config.toml 的 model 字段——只为溯源记录准确，读不到不碍事。"""
    try:
        text = (Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")
        m = re.search(r'^model\s*=\s*"([^"]+)"', text, re.MULTILINE)
        return m.group(1) if m else "config-default"
    except OSError:
        return "config-default"


DEFAULT_MODEL = {"claude": "claude-sonnet-5", "codex": codex_default_model()}


def parse_json_array(text: str) -> list[dict]:
    """从模型输出中提取 JSON 数组（容忍 ```json 围栏）。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"输出中找不到 JSON 数组: {text[:200]}")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("输出不是 JSON 数组")
    return data


def find_all(haystack: str, needle: str, word_boundary: bool = False) -> list[tuple[int, int]]:
    """返回 needle 在 haystack 中所有出现位置。

    word_boundary=True 时要求命中处两侧不是 ASCII 字母数字——短英文片名
    （It / Up / Her）不加这个守卫会无声命中 "Criterion" 之类单词内部。
    """
    hits, pos = [], 0
    while True:
        i = haystack.find(needle, pos)
        if i == -1:
            return hits
        end = i + len(needle)
        if word_boundary:
            # 只在 needle 端点本身是字母数字的一侧查边界："Ba Ba Ba!" 以标点
            # 结尾，右侧紧贴年份（"Ba Ba Ba!2022"）不算词内命中
            before = haystack[i - 1] if i > 0 else ""
            after = haystack[end] if end < len(haystack) else ""
            head_alnum = needle[0].isascii() and needle[0].isalnum()
            tail_alnum = needle[-1].isascii() and needle[-1].isalnum()
            if (head_alnum and before.isascii() and before.isalnum()) or (
                tail_alnum and after.isascii() and after.isalnum()
            ):
                pos = i + 1
                continue
        hits.append((i, end))
        pos = i + 1


def resolve_media_type(value: object, spans: list[dict]) -> str:
    """把模型给的 media_type 归一到 MEDIA_TYPES；非法/缺失时按 span 兜底。

    兜底规则（仅在模型没给合法值时触发，正常极少走到）：有季/集类 span → series，
    完全无 span（非影视负样本）→ other，其余 → movie。"""
    if isinstance(value, str) and value.strip().lower() in MEDIA_TYPES:
        return value.strip().lower()
    episodic = any(s["field"] in ("SEASON", "EPISODE", "EPISODE_TOTAL") for s in spans)
    if episodic:
        return "series"
    return "movie" if spans else "other"


def resolve_content_type(value: object, media_type: str) -> str:
    """把模型给的 content_type 归一到 CONTENT_TYPES；非法/缺失时兜底。

    兜底（仅在模型没给合法值时）：一律归 other——它本就是"动漫/纪录片/综艺
    之外全部"的残差项，缺失时归此最安全（media_type 参数保留以备将来细分）。"""
    _ = media_type
    if isinstance(value, str) and value.strip().lower() in CONTENT_TYPES:
        return value.strip().lower()
    return "other"


def locate_spans(
    sample: dict, extraction: dict, keys: tuple[str, ...] = FIELD_KEYS
) -> tuple[list[dict], list[str]]:
    """把模型抽出的子串定位成字符 span；返回 (spans, 待复核原因列表)。"""
    spans: list[dict] = []
    review: list[str] = []
    sources = {"title": sample["title"], "subtitle": sample.get("subtitle", "")}

    for key in keys:
        raw = extraction.get(key)
        values = raw if isinstance(raw, list) else ([raw] if raw else [])
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            # 词边界守卫对英文片名和纯 ASCII 的字幕/音轨声明开：
            # season/episode 常紧贴字母数字（"S01E10"），year 由提示词规则兜底，
            # 中文没有词边界概念；"CHS"/"SUP" 类声明值不加守卫会无声命中
            # "Super"/"MyTVSuper" 之类单词内部（66 条实测队列的真实病例）
            boundary = key in ("title_en", "subtitle_decl", "audio_decl") and value.isascii()
            found = False
            for source, text in sources.items():
                for start, end in find_all(text, value, word_boundary=boundary):
                    spans.append(
                        {"source": source, "field": FIELD_LABEL[key], "start": start, "end": end}
                    )
                    found = True
            if not found:
                review.append(f"{key} 子串未在原文找到: {value!r}")

    # 重叠消解按来源分开做（title 和 subtitle 的坐标系独立）
    resolved = []
    for source in sources:
        resolved.extend(
            {**s, "source": source}
            for s in resolve_overlaps([s for s in spans if s["source"] == source])
        )
    return resolved, review


def load_done_ids(path: Path) -> set[str]:
    """读取已完成 id。发现残行（进程被杀时写了半行）就地修复：
    好行原样保留、坏行剔除并原子重写，被剔除的样本会自动重标。"""
    if not path.exists():
        return set()
    good_lines, ids, bad = [], set(), 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                ids.add(json.loads(stripped)["id"])
                good_lines.append(stripped)
            except (json.JSONDecodeError, KeyError):
                bad += 1
    if bad:
        tmp = path.with_suffix(".repair.tmp")
        tmp.write_text("".join(line + "\n" for line in good_lines), encoding="utf-8")
        os.replace(tmp, path)
        print(f"检测到 {bad} 个残行（上次中断所致），已修复文件，对应样本将重标")
    return ids


def acquire_lock(out_path: Path) -> None:
    """输出文件级进程锁：防止两个标注进程同时追加同一文件互相踩踏。
    锁文件记录 pid；持有进程已不存在则视为陈锁自动接管。"""
    lock = out_path.with_suffix(out_path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            atexit.register(lambda: lock.unlink(missing_ok=True))
            return
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip() or "0")
                os.kill(pid, 0)  # 探活，不发信号
            except (ValueError, ProcessLookupError, FileNotFoundError):
                lock.unlink(missing_ok=True)  # 陈锁，清掉重试
                continue
            except PermissionError:
                pass  # 进程存在但无权限探测，按存活处理
            sys.exit(f"已有标注进程（pid={pid}）在写 {out_path}，同一输出文件只允许一个进程")


def main() -> None:
    parser = argparse.ArgumentParser(description="大模型蒸馏标注")
    parser.add_argument("--in", dest="input", default="ml/data/raw/samples.jsonl")
    parser.add_argument("--out", default="ml/data/labeled/annotations.jsonl")
    parser.add_argument("--batch", type=int, default=10, help="每次 CLI 调用标注的样本数")
    parser.add_argument("--limit", type=int, default=0, help="本次最多标注多少条（0=不限）")
    parser.add_argument("--engine", choices=sorted(ENGINES), default="claude", help="标注引擎")
    parser.add_argument("--model", default=None, help="模型覆写（默认: claude→sonnet-5, codex→config.toml）")
    parser.add_argument("--max-consecutive-failures", type=int, default=3,
                        help="连续失败多少个批次后熔断退出（认证过期/额度耗尽时快速止损）")
    parser.add_argument("--workers", type=int, default=1,
                        help="并行批次数（codex 建议 4；claude 订阅额度紧建议保持 1）")
    parser.add_argument("--delta", action="store_true",
                        help="存量补标模式：--in 是已标注 JSONL，只补 subtitle_decl/audio_decl "
                             "两个新字段并与原 spans 合并（六字段原标签与溯源保持不动）")
    args = parser.parse_args()
    call_model = ENGINES[args.engine]
    annotator = f"{args.engine}:{args.model or DEFAULT_MODEL[args.engine]}"
    prompt_header = DELTA_PROMPT_HEADER if args.delta else PROMPT_HEADER
    locate_keys = DELTA_FIELD_KEYS if args.delta else FIELD_KEYS

    out_path = Path(args.out)
    acquire_lock(out_path)
    samples = read_jsonl(args.input)
    done = load_done_ids(out_path)
    todo = [s for s in samples if s["id"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    batches = [todo[i : i + args.batch] for i in range(0, len(todo), args.batch)]
    print(f"待标注 {len(todo)} 条（已完成 {len(done)} 条），标注器 {annotator}，"
          f"规范 v{PROMPT_VERSION}，并行 {args.workers}")

    # 并行共享状态：写锁保证 JSONL 行不互相穿插；熔断计数在并行下是
    # "近期连续失败"的近似（成功即清零），语义与串行一致
    write_lock = threading.Lock()
    stop = threading.Event()
    state = {"review": 0, "done": 0, "fail_streak": 0}

    def process_batch(batch: list[dict]) -> None:
        if stop.is_set():
            return
        payload = json.dumps(
            [{"id": s["id"], "title": s["title"], "subtitle": s.get("subtitle", "")} for s in batch],
            ensure_ascii=False,
            indent=1,
        )
        extractions = None
        for attempt in (1, 2):  # 每批次内重试一次（模型输出偶发不合法 JSON）
            try:
                extractions = parse_json_array(call_model(prompt_header + payload, args.model))
                break
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                print(f"批次(首样本 {batch[0]['id']}) 第 {attempt} 次尝试失败: {str(exc)[:300]}")
        with write_lock:
            if extractions is None:
                state["fail_streak"] += 1
                if state["fail_streak"] >= args.max_consecutive_failures:
                    stop.set()
                return
            state["fail_streak"] = 0

            by_id = {e.get("id"): e for e in extractions if isinstance(e, dict)}
            results = []
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for sample in batch:
                extraction = by_id.get(sample["id"])
                if extraction is None:
                    print(f"样本 {sample['id']} 无返回，跳过（重跑时自动补）")
                    continue
                spans, review = locate_spans(sample, extraction, keys=locate_keys)
                if args.delta:
                    # 增量合并。不能用 resolve_overlaps 做新老仲裁——它长度优先，
                    # 会让"老片名 span 误含标签词"的脏标签（更长）压掉新声明 span，
                    # 与规范相反。规则显式化：新字段 span 必留；与之重叠的老 span
                    # 是 v12 视角下的脏标签，丢弃并打 review 送人工裁决。
                    # 原六字段标签与溯源（annotator/prompt_version）保持不动，
                    # 新字段溯源记入 delta_* 前缀。
                    old_spans = sample.get("spans", [])
                    merged = list(spans)
                    for old in old_spans:
                        clash = any(
                            old["source"] == new["source"]
                            and old["start"] < new["end"] and old["end"] > new["start"]
                            for new in spans
                        )
                        if clash:
                            review.append(f"delta 合并冲突：原 {old['field']} span "
                                          f"[{old['start']},{old['end']}) 与新声明重叠，已丢弃待裁决")
                        else:
                            merged.append(old)
                    merged.sort(key=lambda s: (s["source"], s["start"]))
                    record = {
                        **sample,
                        "spans": merged,
                        "delta_annotator": annotator,
                        "delta_prompt_version": PROMPT_VERSION,
                        "delta_annotated_at": now,
                    }
                    review = (sample.get("review") or []) + review
                else:
                    media_type = resolve_media_type(extraction.get("media_type"), spans)
                    record = {
                        **sample,
                        "spans": spans,
                        "media_type": media_type,
                        "content_type": resolve_content_type(extraction.get("content_type"), media_type),
                        "annotator": annotator,
                        "prompt_version": PROMPT_VERSION,
                        "annotated_at": now,
                    }
                if review:
                    record["review"] = review
                    state["review"] += 1
                results.append(record)
            append_jsonl(out_path, results)
            state["done"] += len(batch)
            print(f"进度 {state['done']}/{len(todo)}")

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(process_batch, b) for b in batches]
        for future in as_completed(futures):
            future.result()  # 让线程内的意外异常浮出来而不是被吞掉

    if stop.is_set():
        sys.exit(
            f"连续 {args.max_consecutive_failures} 个批次失败，熔断退出。"
            "请检查 CLI 登录态/额度后重跑（已完成部分自动续接）"
        )
    print(f"完成。待人工复核 {state['review']} 条（grep '\"review\"' {args.out}）")


if __name__ == "__main__":
    main()
