"""AI 字幕生成接口（docs/design/subtitle-ai-translate.md §6，G1 手动触发）。

挂管理区：翻译消费 LLM 配额（真金白银），G1 只对管理员开放；成员开放
随 G2 的额度护栏一起评估。目标语言用文件名语言 token（默认 chs），
产物 sidecar 落视频同目录，任务结束台账即时刷新、播放器立刻可选。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.schemas.response import ApiResponse
from movieclaw_api.schemas.subtitle_gen import (
    CalibratePayload,
    CalibrateResultView,
    GenPreviewBlockerView,
    GenPreviewView,
    GenProgressView,
    GenQualityView,
    GenResultView,
    GenStartPayload,
    GenStartView,
    PgsConversionView,
    PgsOcrLanguageOptionView,
    SourceCandidateView,
)
from movieclaw_api.services.subtitle_gen import tasks as gen_tasks
from movieclaw_db.engine import get_session

router = APIRouter(prefix="/library/files", tags=["subtitle-gen"])


def _preview_view(pv: gen_tasks.Preview) -> GenPreviewView:
    return GenPreviewView(
        candidates=[
            SourceCandidateView(
                kind=c.candidate.kind,
                key=c.candidate.key,
                language=c.candidate.language,
                format=c.candidate.format,
                excluded=c.excluded,
                reasons=c.reasons,
                selectable=gen_tasks.candidate_selectable(c),
                requires_ocr=c.exclusion_code == "pgs",
            )
            for c in pv.candidates
        ],
        chosen_key=(f"{pv.chosen.candidate.kind}:{pv.chosen.candidate.key}" if pv.chosen else None),
        selected_source_key=pv.selected_source_key,
        event_count=pv.event_count,
        estimated_tokens=pv.estimated_tokens,
        already_generated=pv.already_generated,
        warnings=pv.warnings,
        pgs_conversion=(
            PgsConversionView(
                candidate_key=(
                    f"{pv.pgs_conversion.candidate.candidate.kind}:"
                    f"{pv.pgs_conversion.candidate.candidate.key}"
                ),
                language=pv.pgs_conversion.candidate.candidate.language,
                available=pv.pgs_conversion.capability.available,
                engine=pv.pgs_conversion.capability.engine,
                platform=pv.pgs_conversion.capability.platform,
                architecture=pv.pgs_conversion.capability.architecture,
                cached=pv.pgs_conversion.capability.cached,
                message=pv.pgs_conversion.capability.message,
                suggestions=list(pv.pgs_conversion.capability.suggestions),
                ocr_language=pv.pgs_conversion.language.code,
                ocr_language_label=pv.pgs_conversion.language.label,
                language_confirmation_required=(pv.pgs_conversion.language.confirmation_required),
                language_reason=pv.pgs_conversion.language.reason,
                language_options=[
                    PgsOcrLanguageOptionView(code=code, label=label)
                    for code, label in pv.pgs_conversion.language_options
                ],
            )
            if pv.pgs_conversion
            else None
        ),
        blocker=(
            GenPreviewBlockerView(
                code=pv.blocker.code,
                title=pv.blocker.title,
                message=pv.blocker.message,
                suggestions=pv.blocker.suggestions,
            )
            if pv.blocker
            else None
        ),
        output_filename=pv.output_filename,
    )


def _result_view(result: gen_tasks.GenResult | None) -> GenResultView | None:
    if result is None:
        return None
    report = None
    if result.report is not None:
        report = GenQualityView(
            event_count=result.report.event_count,
            cps_overrun=result.report.cps_overrun,
            overlong=result.report.overlong,
            kept_original=result.report.kept_original,
            compressed=result.report.compressed,
            glossary_usage=result.report.glossary_usage,
        )
    return GenResultView(
        ok=result.ok,
        message=result.message,
        filename=result.filename,
        sync_score=result.sync_score,
        source_desc=result.source_desc,
        report=report,
        finished_at=result.finished_at,  # type: ignore[arg-type]
    )


@router.get(
    "/{file_id}/subtitles/generate/preview",
    response_model=ApiResponse[GenPreviewView],
    summary="AI 字幕生成预检：选源结果与成本估算（发起确认框素材）",
    operation_id="lib.subgen.preview",
)
async def gen_preview(
    file_id: int,
    target_language: str = "chs",
    secondary_language: str | None = None,
    source_candidate_key: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[GenPreviewView]:
    pv = await gen_tasks.preview(
        session,
        file_id,
        target_language,
        secondary_language=secondary_language,
        source_candidate_key=source_candidate_key,
    )
    return ApiResponse(data=_preview_view(pv))


@router.post(
    "/{file_id}/subtitles/generate",
    response_model=ApiResponse[GenStartView],
    summary="发起 AI 字幕生成（后台执行；同文件单飞）",
    operation_id="lib.subgen.start",
    openapi_extra={
        "x-cli-long-task": {
            "progress_op": "lib.subgen.status",
            "progress_field": "done_blocks",
        },
    },
)
async def gen_start(
    file_id: int,
    payload: GenStartPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[GenStartView]:
    pv = await gen_tasks.start_generation(
        session,
        file_id,
        payload.target_language,
        secondary_language=payload.secondary_language,
        source_candidate_key=payload.source_candidate_key,
        convert_pgs=payload.convert_pgs,
        pgs_candidate_key=payload.pgs_candidate_key,
        pgs_ocr_language=payload.pgs_ocr_language,
    )
    gen_tasks.queue_generation(
        file_id,
        payload.target_language,
        convert_pgs=payload.convert_pgs,
        pgs_candidate_key=payload.pgs_candidate_key,
        pgs_ocr_language=payload.pgs_ocr_language,
        secondary_language=payload.secondary_language,
        source_candidate_key=pv.selected_source_key,
    )
    return ApiResponse(data=GenStartView(preview=_preview_view(pv)))


@router.get(
    "/{file_id}/subtitles/generate/status",
    response_model=ApiResponse[GenProgressView],
    summary="AI 字幕生成任务状态（进行中进度 / 最近一次结论）",
    operation_id="lib.subgen.status",
)
async def gen_status(file_id: int) -> ApiResponse[GenProgressView]:
    state = gen_tasks.gen_state(file_id)
    if state is not None:
        return ApiResponse(
            data=GenProgressView(
                running=True,
                phase=state.phase,
                message=state.message,
                done_blocks=state.done_blocks,
                total_blocks=state.total_blocks,
                done_events=state.done_events,
                total_events=state.total_events,
                active_blocks=list(state.active_blocks),
                parallelism=state.parallelism,
                uses_ocr=state.uses_ocr,
                target_language=state.target_language,
                secondary_language=state.secondary_language,
                source_candidate_key=state.source_candidate_key,
                elapsed_seconds=max(0, int(time.monotonic() - state.started_at)),
            )
        )
    return ApiResponse(
        data=GenProgressView(
            running=False, last_result=_result_view(gen_tasks.last_result(file_id))
        )
    )


@router.post(
    "/{file_id}/subtitles/generate/stop",
    response_model=ApiResponse[dict],
    summary="停止进行中的字幕生成（已译块保留，可续传）",
    operation_id="lib.subgen.stop",
)
async def gen_stop(file_id: int) -> ApiResponse[dict]:
    stopped = gen_tasks.request_stop(file_id)
    return ApiResponse(
        data={"stopped": stopped},
        message="已请求停止（在下一块边界生效）" if stopped else "该文件没有进行中的任务",
    )


@router.post(
    "/{file_id}/subtitles/calibrate",
    response_model=ApiResponse[CalibrateResultView],
    summary="一键校准外挂字幕时间轴（音轨互相关；strm 退字幕对字幕）",
    operation_id="lib.subgen.calibrate",
)
async def subtitle_calibrate(
    file_id: int,
    payload: CalibratePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[CalibrateResultView]:
    """独立于 AI 翻译的零成本工具（subtitle-ai-translate.md §5.4）：用户
    手工下载的不同步字幕就地修正，校准后覆盖写回并即时刷新台账。"""
    result = await gen_tasks.calibrate_external_subtitle(session, file_id, payload.filename)
    return ApiResponse(
        data=CalibrateResultView(
            ok=result.ok,
            message=result.message,
            scale=result.scale,
            offset_ms=result.offset_ms,
            score=result.score,
        ),
        message=result.message,
    )
