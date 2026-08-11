"""AI 字幕生成接口的响应形态（docs/design/subtitle-ai-translate.md §6）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SourceCandidateView(BaseModel):
    kind: str  # embedded/external
    key: str
    language: str | None
    format: str | None
    excluded: str | None  # 非空 = 被排除原因
    reasons: list[str]  # 打分明细（用户要能看懂"为什么选了这条"）
    selectable: bool  # 文本可直接选；PGS 可选后进入 OCR
    requires_ocr: bool = False


class GenPreviewBlockerView(BaseModel):
    """无法生成时的原因与下一步；前端据此渲染指导弹窗。"""

    code: str
    title: str
    message: str
    suggestions: list[str]


class PgsOcrLanguageOptionView(BaseModel):
    """当前设备可用的一种 PGS 图片语言。"""

    code: str
    label: str


class PgsConversionView(BaseModel):
    """PGS 自动转 SRT 的候选轨道与当前设备能力。"""

    candidate_key: str  # embedded:<字幕轨序号>
    language: str | None
    available: bool
    engine: str | None
    platform: str
    architecture: str
    cached: bool
    message: str
    suggestions: list[str]
    ocr_language: str | None
    ocr_language_label: str | None
    language_confirmation_required: bool
    language_reason: str
    language_options: list[PgsOcrLanguageOptionView]


class GenPreviewView(BaseModel):
    """发起前的确认素材：选源结果 + 成本估算。"""

    candidates: list[SourceCandidateView]
    chosen_key: str | None  # kind:key；None=无可用参考
    selected_source_key: str | None  # 用户指定或英语优先策略实际选中的候选
    event_count: int
    estimated_tokens: int
    already_generated: bool  # 目标语言的 AI 字幕已存在（再生成=覆盖）
    warnings: list[str]
    pgs_conversion: PgsConversionView | None = None
    blocker: GenPreviewBlockerView | None = None
    output_filename: str | None = None


class GenQualityView(BaseModel):
    """机检报告（§3.5）。"""

    event_count: int
    cps_overrun: int
    overlong: int
    kept_original: int
    compressed: int
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
    done_events: int = 0
    total_events: int = 0
    active_blocks: list[int] = Field(default_factory=list)
    parallelism: int = 0
    uses_ocr: bool = False
    target_language: str | None = None
    secondary_language: str | None = None
    source_candidate_key: str | None = None
    elapsed_seconds: int = 0
    last_result: GenResultView | None = None


class GenStartPayload(BaseModel):
    target_language: str = "chs"  # 文件名语言 token（默认简体中文）
    secondary_language: str | None = None  # 双语第二行语言；None=单语
    source_candidate_key: str | None = None  # 绑定预检时选中的 embedded/external 候选
    convert_pgs: bool = False  # 仅在预检明确要求且用户确认后允许 OCR
    pgs_candidate_key: str | None = None  # 兼容旧客户端；新请求统一用 source_candidate_key
    pgs_ocr_language: str | None = None  # 异常时由用户确认的 PGS 图片语言


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
