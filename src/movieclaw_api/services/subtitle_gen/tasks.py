"""生成任务编排：选源 → 同步质检 → 翻译 → 机检 → 落盘 → 台账刷新
（subtitle-ai-translate.md §3.6/§6）。

任务按 library_file id 单飞（TaskState 三件套）；全局串行由单飞 + 前端
逐个发起保证（G1 手动触发场景不存在并发洪峰；自动化入库触发在 G2 连同
队列与额度护栏一起做）。产物落视频同目录 sidecar，写完直接刷新台账
（不等 watchdog，任务结束即可见）。
"""

from __future__ import annotations

import asyncio
import logging
import re as _re
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.services.library.subtitles import discover_external_subtitles
from movieclaw_api.services.subtitle_gen import extract, source, sync, translate, validate
from movieclaw_api.services.task_state import TaskState
from movieclaw_db.engine import get_database
from movieclaw_db.models import LibraryFile, MediaItem, MediaMetadata, utcnow

logger = logging.getLogger("movieclaw_api.subtitle_gen")

#: 每千字符对白的估算 token 量（原文+译文+提示词开销的经验粗估，
#: 只用于发起前的确认展示，不参与任何限额判断）
_TOKENS_PER_KCHAR = 2600


@dataclass
class GenState:
    """进行中任务的实时状态（进度面板数据源）。"""

    phase: str = "preparing"  # preparing/translating/writing
    message: str = "正在准备"
    done_blocks: int = 0
    total_blocks: int = 0
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class GenResult:
    """最近一次任务结论。"""

    ok: bool
    message: str
    filename: str | None = None
    report: validate.QualityReport | None = None
    sync_score: float | None = None
    source_desc: str | None = None
    finished_at: object = None


_tasks: TaskState[GenState] = TaskState()


def gen_state(file_id: int) -> GenState | None:
    return _tasks.state_of(file_id)


def last_result(file_id: int) -> GenResult | None:
    result = _tasks.last(file_id)
    return result if isinstance(result, GenResult) else None


def request_stop(file_id: int) -> bool:
    return _tasks.request_stop(file_id)


async def _load_row(session: AsyncSession, file_id: int) -> LibraryFile:
    row = (
        await session.execute(select(LibraryFile).where(LibraryFile.id == file_id))
    ).scalar_one_or_none()
    if row is None or row.missing_since is not None:
        raise NotFoundException(f"文件不存在或已丢失：id={file_id}")
    return row


async def _film_context(
    session: AsyncSession, row: LibraryFile
) -> tuple[translate.FilmContext, str | None]:
    item = await session.get(MediaItem, row.media_item_id) if row.media_item_id else None
    meta = None
    if row.media_item_id:
        meta = (
            await session.execute(
                select(MediaMetadata).where(
                    MediaMetadata.media_item_id == row.media_item_id
                )
            )
        ).scalar_one_or_none()
    ctx = translate.FilmContext(
        title=item.title if item else Path(row.file_path).stem,
        year=item.year if item else None,
        genres=list(meta.genres or []) if meta else [],
        overview=meta.overview if meta else None,
    )
    return ctx, (meta.original_language if meta else None)


# 目标语言 token 直接进 sidecar 文件名：只收短小写字母数字（防路径注入，
# 也保证产物文件名能被台账命名解析正常消化）
_LANGUAGE_TOKEN = _re.compile(r"^[a-z0-9-]{2,16}$")


def ensure_language_token(target_language: str) -> str:
    token = target_language.strip().lower()
    if not _LANGUAGE_TOKEN.fullmatch(token):
        raise BadRequestException(
            f"目标语言 token 不合法：{target_language!r}"
            "（只允许 2-16 位小写字母/数字/连字符，如 chs）"
        )
    return token


def _sidecar_path(row: LibraryFile, target_language: str) -> Path:
    video = Path(row.file_path)
    return video.parent / f"{video.stem}.{ensure_language_token(target_language)}.ai.srt"


@dataclass
class Preview:
    """发起前的确认素材（§6：展示选源结果与成本估算）。"""

    candidates: list[source.RankedCandidate]
    chosen: source.RankedCandidate | None
    event_count: int
    estimated_tokens: int
    already_generated: bool
    warnings: list[str]


async def preview(session: AsyncSession, file_id: int, target_language: str) -> Preview:
    """选源 + 加载最优候选做成本估算（不动 LLM）。"""
    target_language = ensure_language_token(target_language)
    row = await _load_row(session, file_id)
    _, original_language = await _film_context(session, row)
    ranked = source.rank_candidates(
        row, original_language=original_language, target_language=target_language
    )
    warnings: list[str] = []
    chosen, events = await _pick_loadable(row, ranked, warnings)
    est = 0
    if events:
        chars = sum(len(t) for _, _, t in events)
        est = int(chars / 1000 * _TOKENS_PER_KCHAR)
    return Preview(
        candidates=ranked,
        chosen=chosen,
        event_count=len(events),
        estimated_tokens=est,
        already_generated=_sidecar_path(row, target_language).name
        in {e.get("filename") for e in row.external_subtitles or []},
        warnings=warnings,
    )


async def _pick_loadable(
    row: LibraryFile,
    ranked: list[source.RankedCandidate],
    warnings: list[str],
) -> tuple[source.RankedCandidate | None, list[extract.SubEvent]]:
    """按排序逐个加载候选，返回第一个完整度合格的（§2：加载后评估）。"""
    for cand in ranked:
        if cand.excluded:
            continue
        try:
            events = await extract.load_candidate_events(row, cand.candidate)
        except extract.SourceLoadError as exc:
            warnings.append(str(exc))
            continue
        if cand.candidate.sdh:
            events = extract.strip_sdh_markers(events)
        assessment = source.assess_events(events, row.duration_seconds)
        cand.reasons.append(assessment.reason)
        if assessment.ok:
            return cand, events
        warnings.append(
            f"候选「{_cand_desc(cand.candidate)}」完整度不合格：{assessment.reason}"
        )
    return None, []


def _cand_desc(c: source.SourceCandidate) -> str:
    kind = "内封轨" if c.kind == "embedded" else "外挂"
    return f"{kind} {c.key}（{c.language or '未知语言'}/{c.format}）"


async def start_generation(
    session: AsyncSession, file_id: int, target_language: str
) -> Preview:
    """校验可行性并登记任务（调用方放 BackgroundTasks 执行 run_generation）。"""
    pv = await preview(session, file_id, target_language)
    if pv.chosen is None:
        raise BadRequestException(
            "没有可用的参考字幕：" + ("；".join(pv.warnings) or "该文件既无外挂也无文本内封轨")
        )
    if not _tasks.try_start(file_id, GenState()):
        raise BadRequestException("该文件已有字幕生成任务在进行中")
    return pv


async def run_generation(file_id: int, target_language: str) -> None:
    """后台执行体（自开会话，不向外抛异常；结论落 TaskState.last）。"""
    state = _tasks.state_of(file_id)
    assert state is not None  # start_generation 已登记
    result: GenResult | None = None
    try:
        result = await _run(file_id, target_language, state)
    except translate.TranslationAborted as exc:
        logger.warning("字幕生成中止：file=%s %s", file_id, exc)
        result = GenResult(ok=False, message=str(exc))
    except Exception:  # noqa: BLE001 -- 后台任务兜底
        logger.exception("字幕生成失败：file=%s", file_id)
        result = GenResult(ok=False, message="生成失败：发生未知错误（详见后端日志）")
    finally:
        if result is not None:
            result.finished_at = utcnow()
        _tasks.finish(file_id, result=result)


async def _run(file_id: int, target_language: str, state: GenState) -> GenResult:
    db = get_database()
    async with db.session() as session:
        row = await _load_row(session, file_id)
        ctx, original_language = await _film_context(session, row)
        chat = await _build_chat(session)

    ranked = source.rank_candidates(
        row, original_language=original_language, target_language=target_language
    )
    warnings: list[str] = []
    state.message = "正在选择参考字幕"
    chosen, events = await _pick_loadable(row, ranked, warnings)
    if chosen is None:
        return GenResult(ok=False, message="没有可用的参考字幕：" + "；".join(warnings))
    source_desc = _cand_desc(chosen.candidate)
    logger.info(
        "字幕生成选源：%s ← %s（%s）", row.file_path, source_desc, "；".join(chosen.reasons)
    )

    # L1 同步质检（§5.2）：错位参考在烧 LLM 钱之前拦下；无法检测按未知放行
    state.message = "正在检测参考字幕同步度"
    sync_score = await sync.sample_sync_score(
        Path(row.file_path), events, row.duration_seconds
    )
    if sync_score is not None and sync_score < sync.SYNC_THRESHOLD:
        return GenResult(
            ok=False,
            sync_score=sync_score,
            source_desc=source_desc,
            message=(
                f"参考字幕「{source_desc}」与影片疑似不同步"
                f"（语音命中率 {sync_score:.0%}，阈值 {sync.SYNC_THRESHOLD:.0%}）——"
                "请先用同步的字幕做参考，避免翻译成果整体错位"
            ),
        )

    state.phase = "translating"
    state.message = "正在翻译"

    def _progress(done: int, total: int) -> None:
        state.done_blocks, state.total_blocks = done, total

    out_events, stats = await translate.translate_events(
        chat,
        events,
        ctx,
        target_language,
        file_id=file_id,
        progress=_progress,
        cancelled=lambda: _tasks.stop_requested(file_id),
    )

    state.phase = "writing"
    state.message = "正在写出字幕文件"
    final_events, report = validate.finalize_events(out_events, stats.glossary)
    report.kept_original = stats.kept_original
    sidecar = _sidecar_path(row, target_language)
    try:
        await asyncio.to_thread(translate.write_srt, final_events, sidecar)
    except OSError as exc:
        return GenResult(
            ok=False,
            message=f"字幕文件写入失败（库目录是否只读？）：{sidecar}（{exc}）",
        )
    translate.Checkpoint(
        file_id, target_language, translate.source_fingerprint(events, target_language)
    ).discard()

    # 台账即时刷新（不等 watchdog）：任务结束播放器立刻可选
    async with db.session() as session:
        row = await _load_row(session, file_id)
        row.external_subtitles = await asyncio.to_thread(
            discover_external_subtitles, Path(row.file_path)
        )
        row.updated_at = utcnow()
        await session.commit()

    message = f"已生成 {sidecar.name}（参考：{source_desc}，{report.event_count} 条对白"
    if stats.failed_blocks:
        message += f"，{stats.failed_blocks} 块翻译失败保留原文"
    if report.cps_overrun:
        message += f"，{report.cps_overrun} 条超读速"
    message += "）"
    logger.info("字幕生成完成：%s", message)
    return GenResult(
        ok=True,
        message=message,
        filename=sidecar.name,
        report=report,
        sync_score=sync_score,
        source_desc=source_desc,
    )


async def _build_chat(session: AsyncSession) -> translate.ChatFn:
    """movieclaw_llm 路由 → translate 层的 chat 函数（§3.6）。"""
    from movieclaw_api.services.llm_config import acquire_llm_router
    from movieclaw_llm import ChatMessage, ChatRequest

    router = await acquire_llm_router(session)

    async def chat(system: str, user: str) -> str:
        response = await router.chat(
            ChatRequest(
                messages=[
                    ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=user),
                ]
            )
        )
        return response.content or ""

    return chat


# ---------------------------------------------------------------------------
# 一键校准已有外挂字幕（§5.4：独立工具，不依赖 LLM、零调用成本）
# ---------------------------------------------------------------------------


@dataclass
class CalibrateResult:
    ok: bool
    message: str
    scale: float | None = None
    offset_ms: int | None = None
    score: float | None = None


#: 校准结论可信度下限：相关峰太低说明参考与字幕根本对不上（如错片字幕）
_CALIBRATE_MIN_SCORE = 0.15
#: 变化小于该量级视为"本就同步"，不动文件
_NOOP_OFFSET_MS = 200


async def _audio_reference_intervals(
    row: LibraryFile, events: list[extract.SubEvent]
) -> list[tuple[int, int]] | None:
    """全片语音区间（校准参考）：分段抽 PCM 拼接;无 ffmpeg/无音轨返回 None。

    校准与检测不同,需要整片视野才能解出全局 (scale, offset)——按 10 分钟
    一段流式抽取拼接区间表,内存里从不持有整片 PCM。
    """
    runtime = row.duration_seconds or (max(e[1] for e in events) // 1000 if events else 0)
    if runtime <= 0:
        return None
    segment = 600
    intervals: list[tuple[int, int]] = []
    for start in range(0, runtime, segment):
        pcm = await asyncio.to_thread(
            sync._extract_pcm_sync,
            Path(row.file_path),
            float(start),
            float(min(segment, runtime - start)),
        )
        if pcm is None:
            return None if start == 0 else intervals  # 首段就失败=不可用
        intervals.extend(
            (s + start * 1000, e + start * 1000) for s, e in sync.speech_intervals(pcm)
        )
    return intervals


async def _subtitle_reference_intervals(
    row: LibraryFile, exclude_filename: str
) -> tuple[list[tuple[int, int]] | None, str | None]:
    """字幕对字幕兜底（L2'）：本文件另一条字幕的时间结构做参考。

    参考优先内封轨（配套概率最高）,其次其他外挂;返回 (区间表, 参考描述)。
    """
    ranked = source.rank_candidates(row, original_language=None, target_language="__none__")
    for cand in ranked:
        if cand.excluded and "forced" in cand.excluded:
            continue  # forced 轨时间结构残缺,不做参考;其余排除原因不影响对齐
        if cand.candidate.kind == "external" and cand.candidate.key == exclude_filename:
            continue
        try:
            events = await extract.load_candidate_events(row, cand.candidate)
        except extract.SourceLoadError:
            continue
        if len(events) >= 50:
            return [(s, e) for s, e, _ in events], _cand_desc(cand.candidate)
    return None, None


async def calibrate_external_subtitle(
    session: AsyncSession, file_id: int, filename: str
) -> CalibrateResult:
    """校准一条外挂字幕并覆盖写回（UTF-8）;台账即时刷新。

    参考优先音轨（真相源）,无音频（strm/无 ffmpeg）退字幕对字幕;两者都
    不可用如实报错。校准量小于噪声阈值时不动文件（"本就同步"）。
    """
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise BadRequestException(f"字幕文件名不合法：{filename!r}")
    row = await _load_row(session, file_id)
    entry = next(
        (e for e in row.external_subtitles or [] if e.get("filename") == filename),
        None,
    )
    if entry is None:
        # 台账兜底：旧行（NULL，未重扫）或刚放入的文件——控制台详情页展示
        # 的是磁盘现场清单，按现场重新发现一次，别让用户先扫库再校准
        discovered = await asyncio.to_thread(
            discover_external_subtitles, Path(row.file_path)
        )
        entry = next((e for e in discovered if e.get("filename") == filename), None)
        if entry is None:
            raise NotFoundException(f"没有找到这条外挂字幕文件：{filename}")
    cand = source.SourceCandidate(
        kind="external",
        key=filename,
        language=entry.get("language"),
        forced=bool(entry.get("forced")),
        sdh=bool(entry.get("sdh")),
        format=str(entry.get("format") or "").lower(),
    )
    events = await extract.load_candidate_events(row, cand)
    if len(events) < 20:
        return CalibrateResult(ok=False, message="字幕事件太少，无法可靠校准")

    reference = await _audio_reference_intervals(row, events)
    ref_desc = "影片音轨"
    if not reference:
        reference, ref_desc = await _subtitle_reference_intervals(row, filename)
        if not reference:
            return CalibrateResult(
                ok=False,
                message="没有可用的校准参考：本机无 ffmpeg 或无音轨（strm 云端文件），"
                "且该文件没有其他字幕轨可做时间结构对齐",
            )

    calibration = sync.estimate_calibration(
        reference, [(s, e) for s, e, _ in events]
    )
    if calibration is None or calibration.score < _CALIBRATE_MIN_SCORE:
        return CalibrateResult(
            ok=False,
            message=f"校准置信度不足（参考：{ref_desc}）——字幕可能与该影片不匹配",
            score=calibration.score if calibration else None,
        )
    if (
        calibration.scale == 1.0
        and abs(calibration.offset_ms) <= _NOOP_OFFSET_MS
    ):
        return CalibrateResult(
            ok=True,
            message=f"字幕本就同步（参考：{ref_desc}），未做修改",
            scale=1.0,
            offset_ms=calibration.offset_ms,
            score=calibration.score,
        )

    corrected = sync.apply_calibration(events, calibration)
    path = Path(row.file_path).parent / filename
    suffix = path.suffix.lstrip(".").lower()
    try:
        if suffix == "srt":
            await asyncio.to_thread(translate.write_srt, corrected, path)
        else:
            # 非 srt（ass/ssa/vtt）：pysubs2 原格式平移缩放后写回,样式保留
            await asyncio.to_thread(
                _shift_subtitle_file, path, calibration
            )
    except OSError as exc:
        return CalibrateResult(
            ok=False, message=f"校准结果写入失败（库目录是否只读？）：{exc}"
        )

    async with get_database().session() as refresh_session:
        fresh = await _load_row(refresh_session, file_id)
        fresh.external_subtitles = await asyncio.to_thread(
            discover_external_subtitles, Path(fresh.file_path)
        )
        fresh.updated_at = utcnow()
        await refresh_session.commit()

    logger.info(
        "外挂字幕校准完成：%s scale=%.6f offset=%dms score=%.2f 参考=%s",
        path, calibration.scale, calibration.offset_ms, calibration.score, ref_desc,
    )
    return CalibrateResult(
        ok=True,
        message=(
            f"校准完成（参考：{ref_desc}）：缩放 {calibration.scale:.4f}、"
            f"平移 {calibration.offset_ms / 1000:+.2f} 秒，已覆盖写回"
        ),
        scale=calibration.scale,
        offset_ms=calibration.offset_ms,
        score=calibration.score,
    )


def _shift_subtitle_file(path: Path, calibration: sync.Calibration) -> None:
    """（线程池）非 srt 字幕的原格式校准写回（保留 ass 样式）。"""
    import pysubs2

    raw = path.read_bytes()
    text = extract.decode_subtitle_bytes(raw, str(path))
    subs = pysubs2.SSAFile.from_string(text)
    for line in subs:
        line.start = max(0, int(line.start * calibration.scale) + calibration.offset_ms)
        line.end = max(line.start, int(line.end * calibration.scale) + calibration.offset_ms)
    fmt = {"ssa": "ssa", "ass": "ass", "vtt": "vtt"}.get(path.suffix.lstrip(".").lower(), "srt")
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(subs.to_string(fmt), encoding="utf-8")
    tmp.replace(path)
