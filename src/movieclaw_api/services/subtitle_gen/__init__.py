"""AI 外挂字幕生成（docs/design/subtitle-ai-translate.md，生产端 A 层）。

只造文件：选源 → 同步质检 → 抽取 → LLM 翻译 → 机检 → 落盘 sidecar。
消费端（发现/台账/输出/播放控制）零改动自动生效。

分层守护：本包不 import movieclaw_jellyfin / movieclaw_playback——
它与播放两层互不感知（tests 有 AST 断言）。
"""
