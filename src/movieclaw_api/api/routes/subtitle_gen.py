"""AI 字幕生成接口（docs/design/subtitle-ai-translate.md §6，G1 手动触发）。

挂管理区：翻译消费 LLM 配额（真金白银），G1 只对管理员开放；成员开放
随 G2 的额度护栏一起评估。目标语言用文件名语言 token（默认 chs），
产物 sidecar 落视频同目录，任务结束台账即时刷新、播放器立刻可选。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.schemas.response import ApiResponse
from movieclaw_api.schemas.subtitle_gen import (
    GenPreviewView,
    GenProgressView,
    GenQualityView,
    GenResultView,
    GenStartPayload,
    GenStartView,
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
            )
            for c in pv.candidates
        ],
        chosen_key=(
            f"{pv.chosen.candidate.kind}:{pv.chosen.candidate.key}" if pv.chosen else None
        ),
        event_count=pv.event_count,
        estimated_tokens=pv.estimated_tokens,
        already_generated=pv.already_generated,
        warnings=pv.warnings,
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
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[GenPreviewView]:
    pv = await gen_tasks.preview(session, file_id, target_language)
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
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[GenStartView]:
    pv = await gen_tasks.start_generation(session, file_id, payload.target_language)
    background_tasks.add_task(
        gen_tasks.run_generation, file_id, payload.target_language
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
