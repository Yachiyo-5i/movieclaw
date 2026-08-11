"""AI 字幕生成接口的响应形态（docs/design/subtitle-ai-translate.md §6）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SourceCandidateView(BaseModel):
    kind: str  # embedded/external
    key: str
    language: str | None
    format: str | None
    excluded: str | None  # 非空 = 被排除原因
    reasons: list[str]  # 打分明细（用户要能看懂"为什么选了这条"）


class GenPreviewView(BaseModel):
    """发起前的确认素材：选源结果 + 成本估算。"""

    candidates: list[SourceCandidateView]
    chosen_key: str | None  # kind:key；None=无可用参考
    event_count: int
    estimated_tokens: int
    already_generated: bool  # 目标语言的 AI 字幕已存在（再生成=覆盖）
    warnings: list[str]


class GenQualityView(BaseModel):
    """机检报告（§3.5）。"""

    event_count: int
    cps_overrun: int
    overlong: int
    kept_original: int
    glossary_usage: dict[str, int]


class GenResultView(BaseModel):
    ok: bool
    message: str
    filename: str | None
    sync_score: float | None
    source_desc: str | None
    report: GenQualityView | None
    finished_at: datetime | None


class GenProgressView(BaseModel):
    """任务状态查询：running 时带实时进度，否则带最近一次结论。"""

    running: bool
    phase: str | None = None
    message: str | None = None
    done_blocks: int = 0
    total_blocks: int = 0
    last_result: GenResultView | None = None


class GenStartPayload(BaseModel):
    target_language: str = "chs"  # 文件名语言 token（默认简体中文）


class GenStartView(BaseModel):
    """已入队的确认回执（含预估，前端展示）。"""

    preview: GenPreviewView


class CalibratePayload(BaseModel):
    filename: str  # 台账里的外挂字幕文件名


class CalibrateResultView(BaseModel):
    ok: bool
    message: str
    scale: float | None
    offset_ms: int | None
    score: float | None
