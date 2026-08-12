"""生成任务编排：选源 → 同步质检 → 翻译 → 机检 → 落盘 → 台账刷新
（subtitle-ai-translate.md §3.6/§6）。

任务按 library_file id 单飞（TaskState 三件套），不同文件可排队；实际执行
由进程级锁全局串行，手动入口与自动批次共用同一成本闸门。产物落视频同目录
sidecar，写完直接刷新台账（不等 watchdog，任务结束即可见）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re as _re
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import AppException, BadRequestException, NotFoundException
from movieclaw_api.services.library.subtitles import discover_external_subtitles
from movieclaw_api.services.subtitle_gen import extract, pgs, source, sync, translate, validate
from movieclaw_api.services.task_state import TaskState
from movieclaw_db.engine import get_database
from movieclaw_db.models import LibraryFile, MediaItem, MediaMetadata, utcnow

logger = logging.getLogger("movieclaw_api.subtitle_gen")

#: 每千字符对白的估算 token 量（原文+译文+提示词开销的经验粗估，
#: 只用于发起前的确认展示，不参与任何限额判断）
_TOKENS_PER_KCHAR = 2600
_JOB_INTENT_VERSION = 1


@dataclass(frozen=True)
class GenerationIntent:
    """用户已确认且在进程意外退出后仍应继续的字幕任务。"""

    file_id: int
    target_language: str
    convert_pgs: bool = False
    pgs_candidate_key: str | None = None
    pgs_ocr_language: str | None = None
    secondary_language: str | None = None
    source_candidate_key: str | None = None


def _intent_path(intent: GenerationIntent) -> Path:
    output_key = subtitle_output_key(intent.target_language, intent.secondary_language)
    return extract.cache_dir() / f"{intent.file_id}.{output_key}.job.json"


def _persist_intent(intent: GenerationIntent) -> None:
    """原子写入任务意图；断电最多留下 .part，不会产生半份有效任务。"""
    target_language, secondary_language = ensure_output_languages(
        intent.target_language, intent.secondary_language
    )
    path = _intent_path(intent)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(
            {
                "version": _JOB_INTENT_VERSION,
                "file_id": intent.file_id,
                "target_language": target_language,
                "convert_pgs": intent.convert_pgs,
                "pgs_candidate_key": intent.pgs_candidate_key,
                "pgs_ocr_language": intent.pgs_ocr_language,
                "secondary_language": secondary_language,
                "source_candidate_key": intent.source_candidate_key,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _discard_intent(intent: GenerationIntent) -> None:
    try:
        _intent_path(intent).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("清理字幕任务意图失败：file=%s %s", intent.file_id, exc)


def _discard_file_intents(file_id: int) -> None:
    """明确停止时清掉该文件的全部目标语言意图，防止重启后反向复活。"""
    for path in extract.cache_dir().glob(f"{file_id}.*.job.json"):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理字幕任务意图失败：file=%s %s", file_id, exc)


def _load_intent(path: Path) -> GenerationIntent | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != _JOB_INTENT_VERSION:
            raise ValueError("版本不受支持")
        file_id = int(data["file_id"])
        if file_id <= 0:
            raise ValueError("文件 ID 无效")
        target_language = ensure_language_token(str(data["target_language"]))
        convert_pgs = data.get("convert_pgs") is True
        candidate_key = data.get("pgs_candidate_key")
        ocr_language = data.get("pgs_ocr_language")
        secondary_language = data.get("secondary_language")
        source_candidate_key = data.get("source_candidate_key")
        if candidate_key is not None and not isinstance(candidate_key, str):
            raise ValueError("PGS 轨道标识无效")
        if ocr_language is not None and not isinstance(ocr_language, str):
            raise ValueError("OCR 语言无效")
        if secondary_language is not None and not isinstance(secondary_language, str):
            raise ValueError("第二字幕语言无效")
        if source_candidate_key is not None and not isinstance(source_candidate_key, str):
            raise ValueError("参考字幕标识无效")
        target_language, secondary_language = ensure_output_languages(
            target_language, secondary_language
        )
        return GenerationIntent(
            file_id=file_id,
            target_language=target_language,
            convert_pgs=convert_pgs,
            pgs_candidate_key=candidate_key,
            pgs_ocr_language=ocr_language,
            secondary_language=secondary_language,
            source_candidate_key=source_candidate_key,
        )
    except (
        BadRequestException,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        logger.warning("跳过损坏的字幕任务意图：%s（%s）", path, exc)
        return None


@dataclass
class GenState:
    """进行中任务的实时状态（进度面板数据源）。"""

    # 阶段值由前端映射成稳定的五步进度，message 则描述阶段内的即时动作。
    phase: str = "preparing"
    message: str = "正在准备"
    done_blocks: int = 0
    total_blocks: int = 0
    done_events: int = 0
    total_events: int = 0
    active_blocks: tuple[int, ...] = ()
    parallelism: int = 0
    uses_ocr: bool = False
    target_language: str = "chs"
    secondary_language: str | None = None
    source_candidate_key: str | None = None
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
# 翻译调用会消耗真实 LLM 配额；不同文件可以同时排队，但执行体必须全局串行。
# TaskState 只负责“同文件单飞”，不能替代这把进程级串行闸门。
_generation_lock = asyncio.Lock()


def gen_state(file_id: int) -> GenState | None:
    return _tasks.state_of(file_id)


def last_result(file_id: int) -> GenResult | None:
    result = _tasks.last(file_id)
    return result if isinstance(result, GenResult) else None


def request_stop(file_id: int) -> bool:
    stopped = _tasks.request_stop(file_id)
    if stopped:
        # 用户明确停止与服务重启不同：立即撤销持久化意图，即使进程随后崩溃，
        # 下次启动也不能擅自把用户刚停掉的任务重新拉起。
        _discard_file_intents(file_id)
    return stopped


def is_generation_running(file_id: int) -> bool:
    """该台账行是否正有字幕生成任务在执行。"""
    return _tasks.running(file_id)


def discard_removed_file_job_state(file_id: int) -> None:
    """台账行已提交删除后，清理它遗留的字幕续传状态。

    调用方先以 ``is_generation_running`` 确认没有在跑的任务，且只能在删除
    台账事务提交成功后调用。这样服务重启时不会续传一个已经不存在的文件，
    同时避免提交失败却提前丢掉用户已确认的任务意图。
    """
    _discard_file_intents(file_id)
    for path in extract.cache_dir().glob(f"{file_id}.*.checkpoint.json"):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理字幕翻译断点失败：file=%s %s", file_id, exc)
    # 防止 SQLite 以后复用这个 id 时，详情页读到被删除行遗留的“最近结果”。
    _tasks.discard(file_id)


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
                select(MediaMetadata).where(MediaMetadata.media_item_id == row.media_item_id)
            )
        ).scalar_one_or_none()
    ctx = translate.FilmContext(
        title=item.title if item else Path(row.file_path).stem,
        year=item.year if item else None,
        genres=list(meta.genres or []) if meta else [],
        overview=meta.overview if meta else None,
    )
    return ctx, (meta.original_language if meta else None)


# 输出语言是产品能力边界，也是提示词、文件名和断点身份的一部分。语言 token
# 采用项目既有短码；外挂文件末段另写 ISO 639-2/B，兼容 Jellyfin/Plex/Kodi。
OUTPUT_LANGUAGES: dict[str, str] = {
    "chs": "简体中文",
    "cht": "繁体中文",
    "eng": "英语",
    "jpn": "日语",
    "kor": "韩语",
    "fre": "法语",
    "ger": "德语",
    "spa": "西班牙语",
    "ita": "意大利语",
    "por": "葡萄牙语",
    "rus": "俄语",
    "tha": "泰语",
}
_FILENAME_LANGUAGE = {"chs": "chi", "cht": "chi"}
_LANGUAGE_TOKEN = _re.compile(r"^[a-z0-9-]{2,16}$")


def ensure_language_token(target_language: str) -> str:
    token = target_language.strip().lower()
    if not _LANGUAGE_TOKEN.fullmatch(token):
        raise BadRequestException(
            f"目标语言 token 不合法：{target_language!r}"
            "（只允许 2-16 位小写字母/数字/连字符，如 chs）"
        )
    return token


def ensure_output_languages(
    target_language: str, secondary_language: str | None = None
) -> tuple[str, str | None]:
    """校验用户选择的单语/双语输出；双语两行不能选择同一种语言。"""
    target = ensure_language_token(target_language)
    secondary = ensure_language_token(secondary_language) if secondary_language else None
    if target not in OUTPUT_LANGUAGES:
        raise BadRequestException(f"暂不支持生成 {target} 字幕")
    if secondary is not None and secondary not in OUTPUT_LANGUAGES:
        raise BadRequestException(f"暂不支持生成 {secondary} 字幕")
    if secondary == target:
        raise BadRequestException("双语字幕的第一行和第二行不能选择同一种语言")
    return target, secondary


def subtitle_output_key(target_language: str, secondary_language: str | None = None) -> str:
    target, secondary = ensure_output_languages(target_language, secondary_language)
    return target if secondary is None else f"{target}-{secondary}"


def subtitle_output_label(target_language: str, secondary_language: str | None = None) -> str:
    target, secondary = ensure_output_languages(target_language, secondary_language)
    if secondary is None:
        return OUTPUT_LANGUAGES[target]
    return f"{OUTPUT_LANGUAGES[target]} + {OUTPUT_LANGUAGES[secondary]}双语"


def _sidecar_path(
    row: LibraryFile, target_language: str, secondary_language: str | None = None
) -> Path:
    video = Path(row.file_path)
    target, secondary = ensure_output_languages(target_language, secondary_language)
    language = _FILENAME_LANGUAGE.get(target, target)
    title = f"ai-{target}" if target != language else "ai"
    if secondary is not None:
        title = f"ai-bilingual-{target}-{secondary}"
    return video.parent / f"{video.stem}.{title}.{language}.srt"


def _legacy_sidecar_path(row: LibraryFile, target_language: str) -> Path:
    """v2.1 及更早版本的 AI 字幕命名；只用于无重复迁移。"""
    video = Path(row.file_path)
    target = ensure_language_token(target_language)
    return video.parent / f"{video.stem}.{target}.ai.srt"


def _pgs_sidecar_path(row: LibraryFile, source_language: str) -> Path:
    """PGS OCR 通过完整度检查后的可复用外挂，语言码保持播放器可识别。"""
    video = Path(row.file_path)
    normalized = source.normalize_language(source_language) or "und"
    language = _FILENAME_LANGUAGE.get(normalized, normalized)
    return video.parent / f"{video.stem}.pgs-ocr.{language}.srt"


@dataclass
class Preview:
    """发起前的确认素材（§6：展示选源结果与成本估算）。"""

    candidates: list[source.RankedCandidate]
    chosen: source.RankedCandidate | None
    event_count: int
    estimated_tokens: int
    already_generated: bool
    warnings: list[str]
    pgs_conversion: PgsConversionPlan | None
    blocker: PreviewBlocker | None
    output_filename: str | None = None
    selected_source_key: str | None = None


@dataclass(frozen=True)
class PreviewBlocker:
    """预检未通过时面向产品层的结构化解释，而不是一条不可行动的错误。"""

    code: str
    title: str
    message: str
    suggestions: list[str]


@dataclass(frozen=True)
class PgsConversionPlan:
    """没有文本源时，预检选中的 PGS 轨道及当前设备能力。"""

    candidate: source.RankedCandidate
    capability: pgs.Capability
    language: pgs.OcrLanguageDecision
    language_options: tuple[tuple[str, str], ...] = ()


def candidate_key(candidate: source.RankedCandidate) -> str:
    """候选字幕的稳定中性引用；内封用序号，外挂用完整文件名。"""
    return f"{candidate.candidate.kind}:{candidate.candidate.key}"


def candidate_selectable(candidate: source.RankedCandidate) -> bool:
    """文本字幕可直接选择，PGS 可选择后进入 OCR；其他排除项不可选。"""
    return candidate.exclusion_code in {None, "pgs"}


def _select_reference(
    ranked: list[source.RankedCandidate], requested_key: str | None
) -> source.RankedCandidate | None:
    """绑定用户指定候选；未指定时优先英语，再回退既有质量排序。"""
    if requested_key is not None:
        selected = next((item for item in ranked if candidate_key(item) == requested_key), None)
        if selected is None:
            raise BadRequestException("所选参考字幕已不存在，请重新选择")
        if not candidate_selectable(selected):
            raise BadRequestException(f"所选参考字幕不能用于翻译：{selected.excluded}")
        return selected

    selectable = [item for item in ranked if candidate_selectable(item)]
    # 默认英语符合绝大多数用户的二次翻译习惯；有文本英语时不为了同语种
    # PGS 多走一次 OCR，只有没有英语文本时才选择英语 PGS。
    english_text = [
        item
        for item in selectable
        if item.candidate.language == "eng" and item.exclusion_code is None
    ]
    if english_text:
        return english_text[0]
    english = [item for item in selectable if item.candidate.language == "eng"]
    if english:
        return english[0]
    return selectable[0] if selectable else None


async def _pgs_plan(
    row: LibraryFile,
    ranked: list[source.RankedCandidate],
    *,
    original_language: str | None = None,
    requested_candidate_key: str | None = None,
    requested_ocr_language: str | None = None,
) -> PgsConversionPlan | None:
    """按既有顺序选 PGS；用户确认后只允许复验同一轨道和识别语言。"""
    first_unavailable: PgsConversionPlan | None = None
    for ranked_candidate in ranked:
        if ranked_candidate.exclusion_code != "pgs":
            continue
        candidate_key = f"{ranked_candidate.candidate.kind}:{ranked_candidate.candidate.key}"
        if requested_candidate_key and candidate_key != requested_candidate_key:
            continue

        language = pgs.infer_ocr_language(
            row,
            ranked_candidate.candidate,
            original_language,
        )
        if requested_ocr_language is not None:
            selected = pgs.normalize_ocr_language(requested_ocr_language)
            language = pgs.OcrLanguageDecision(
                code=selected,
                label=pgs.OCR_LANGUAGE_LABELS.get(selected) if selected else None,
                confirmation_required=selected is None,
                reason=(
                    f"已确认使用 {pgs.OCR_LANGUAGE_LABELS[selected]} 识别 PGS"
                    if selected
                    else f"不支持的 OCR 语言：{requested_ocr_language}"
                ),
            )

        options: tuple[tuple[str, str], ...] = ()
        if language.code is not None:
            capability = await asyncio.to_thread(
                pgs.conversion_capability,
                row,
                ranked_candidate.candidate,
                language.code,
            )
            if language.confirmation_required and not capability.cached:
                options, representative = await asyncio.to_thread(pgs.available_ocr_languages)
                if language.code not in {code for code, _label in options}:
                    language = pgs.OcrLanguageDecision(
                        code=None,
                        label=None,
                        confirmation_required=True,
                        reason=(
                            f"{language.reason}；但当前设备没有对应语言包，"
                            "请从实际可用语言中重新选择"
                        ),
                    )
                    if representative is not None:
                        capability = representative
        else:
            options, representative = await asyncio.to_thread(pgs.available_ocr_languages)
            capability = representative or await asyncio.to_thread(pgs.detect_capability, None)

        if capability.cached and language.confirmation_required:
            language = pgs.OcrLanguageDecision(
                code=language.code,
                label=language.label,
                confirmation_required=False,
                reason="已有可复用的 OCR 缓存，无需再次选择识别语言",
            )
        plan = PgsConversionPlan(
            candidate=ranked_candidate,
            capability=capability,
            language=language,
            language_options=options,
        )
        if capability.available:
            return plan
        if first_unavailable is None:
            first_unavailable = plan
    return first_unavailable


def _preview_blocker(
    ranked: list[source.RankedCandidate],
    pgs_plan: PgsConversionPlan | None = None,
    target_language: str = "chs",
    secondary_language: str | None = None,
) -> PreviewBlocker:
    """把选源失败归纳为用户能理解、能继续处理的原因。"""
    output_label = subtitle_output_label(target_language, secondary_language)
    add_sidecar = "添加与当前片源匹配的 SRT、ASS 或 VTT 外挂字幕"
    rescan = "重新扫描媒体库后再试"

    if not ranked:
        return PreviewBlocker(
            code="no_subtitle",
            title="这份片源没有参考字幕",
            message="当前功能只翻译现有文本字幕，不会从音轨听写对白。",
            suggestions=[add_sidecar, rescan],
        )

    if pgs_plan is not None:
        capability = pgs_plan.capability
        language = pgs_plan.language.label or "待确认语言"
        if capability.available:
            if pgs_plan.language.confirmation_required:
                return PreviewBlocker(
                    code="pgs_conversion_required",
                    title="请确认字幕语言",
                    message=(
                        "这份字幕是图片，需要先识别其中的文字。"
                        f"请选择画面中实际显示的语言，再开始生成{output_label}字幕。"
                    ),
                    suggestions=[
                        f"这里选择的是原字幕语言，不是最终生成的{output_label}字幕语言",
                        "不确定时可以先播放影片查看字幕内容",
                    ],
                )
            return PreviewBlocker(
                code="pgs_conversion_required",
                title="先识别图片字幕",
                message=(
                    f"检测到{language}图片字幕。MovieClaw 会先识别文字，"
                    f"确认内容完整后再生成{output_label}字幕。"
                ),
                suggestions=[
                    "原影片和字幕不会被修改",
                    "识别结果可能有少量错字，完成后建议抽查人名与特殊字体",
                ],
            )
        return PreviewBlocker(
            code="pgs_conversion_unavailable",
            title="暂时无法识别这份字幕",
            message=(
                "当前设备还不能自动识别这份图片字幕。你可以按下面的建议处理，或交给 Agent 检查。"
            ),
            suggestions=list(capability.suggestions),
        )

    if all(c.exclusion_code in {"graphic", "pgs"} for c in ranked):
        return PreviewBlocker(
            code="graphics_only",
            title="暂时无法识别这份字幕",
            message=(f"检测到 {len(ranked)} 条图片字幕，但当前设备无法自动识别其中的文字。"),
            suggestions=[add_sidecar, rescan],
        )

    if all(c.exclusion_code == "target_language" for c in ranked):
        return PreviewBlocker(
            code="target_exists",
            title="已有目标语言字幕",
            message=f"检测到的字幕已经是{output_label}，无需再次生成。",
            suggestions=["如需替换，请先添加其他语言的参考字幕"],
        )

    if any(c.excluded is None for c in ranked):
        return PreviewBlocker(
            code="unusable_text",
            title="参考字幕不完整",
            message="文本字幕无法解析，或对白数量、时间覆盖不足。",
            suggestions=[
                "换用与当前片源匹配、内容完整的文本字幕",
                rescan,
            ],
        )

    graphic_count = sum(c.exclusion_code == "graphic" for c in ranked)
    return PreviewBlocker(
        code="no_usable_reference",
        title="现有字幕无法用于翻译",
        message=(
            f"检测到 {len(ranked)} 条字幕"
            + (f"，其中 {graphic_count} 条是图形字幕" if graphic_count else "")
            + "；其余为强制片段或目标语言字幕。"
        ),
        suggestions=[add_sidecar, rescan],
    )


async def preview(
    session: AsyncSession,
    file_id: int,
    target_language: str,
    *,
    secondary_language: str | None = None,
    source_candidate_key: str | None = None,
    pgs_candidate_key: str | None = None,
    pgs_ocr_language: str | None = None,
) -> Preview:
    """选源 + 加载最优候选做成本估算（不动 LLM）。"""
    target_language, secondary_language = ensure_output_languages(
        target_language, secondary_language
    )
    row = await _load_row(session, file_id)
    _, original_language = await _film_context(session, row)
    # 双语允许把其中一种语言的现有字幕当参考；例如英语原字幕生成英中双语，
    # 不能因为第一行也是英语就把这条唯一参考排除。
    source_target = target_language if secondary_language is None else "__bilingual__"
    ranked = source.rank_candidates(
        row, original_language=original_language, target_language=source_target
    )
    requested_source_key = source_candidate_key or pgs_candidate_key
    selected = _select_reference(ranked, requested_source_key)
    selected_candidates = [selected] if selected is not None else []
    warnings: list[str] = []
    chosen, events = await _pick_loadable(row, selected_candidates, warnings)
    pgs_conversion = (
        await _pgs_plan(
            row,
            selected_candidates,
            original_language=original_language,
            requested_candidate_key=(candidate_key(selected) if selected is not None else None),
            requested_ocr_language=pgs_ocr_language,
        )
        if chosen is None
        else None
    )
    est = 0
    if events:
        chars = sum(len(t) for _, _, t in events)
        est = int(chars / 1000 * _TOKENS_PER_KCHAR)
        if secondary_language is not None:
            # 同一个请求同时产出两种语言，共享提示词和上下文，但输出量接近翻倍。
            est = int(est * 1.8)
    return Preview(
        candidates=ranked,
        chosen=chosen,
        event_count=len(events),
        estimated_tokens=est,
        already_generated=bool(
            {
                _sidecar_path(row, target_language, secondary_language).name,
                *(
                    [_legacy_sidecar_path(row, target_language).name]
                    if secondary_language is None
                    else []
                ),
            }
            & {e.get("filename") for e in row.external_subtitles or []}
        ),
        warnings=warnings,
        pgs_conversion=pgs_conversion,
        blocker=(
            _preview_blocker(ranked, pgs_conversion, target_language, secondary_language)
            if chosen is None
            else None
        ),
        output_filename=_sidecar_path(row, target_language, secondary_language).name,
        selected_source_key=candidate_key(selected) if selected is not None else None,
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
        warnings.append(f"候选「{_cand_desc(cand.candidate)}」完整度不合格：{assessment.reason}")
    return None, []


def _cand_desc(c: source.SourceCandidate) -> str:
    kind = "内封轨" if c.kind == "embedded" else "外挂"
    return f"{kind} {c.key}（{c.language or '未知语言'}/{c.format}）"


async def start_generation(
    session: AsyncSession,
    file_id: int,
    target_language: str,
    *,
    secondary_language: str | None = None,
    source_candidate_key: str | None = None,
    convert_pgs: bool = False,
    pgs_candidate_key: str | None = None,
    pgs_ocr_language: str | None = None,
) -> Preview:
    """校验可行性并登记任务；调用方随后交给任务主管执行。"""
    target_language, secondary_language = ensure_output_languages(
        target_language, secondary_language
    )
    pv = await preview(
        session,
        file_id,
        target_language,
        secondary_language=secondary_language,
        source_candidate_key=source_candidate_key,
        pgs_candidate_key=pgs_candidate_key,
        pgs_ocr_language=pgs_ocr_language,
    )
    if pv.chosen is None:
        if pgs_candidate_key and pv.pgs_conversion is None:
            raise BadRequestException("用户确认的 PGS 轨道已变化，请重新预检后再试")
        language_ready = bool(
            pv.pgs_conversion
            and (
                pv.pgs_conversion.capability.cached
                or (
                    pv.pgs_conversion.language.code
                    and not pv.pgs_conversion.language.confirmation_required
                )
            )
        )
        conversion_allowed = (
            convert_pgs
            and pv.pgs_conversion is not None
            and pv.pgs_conversion.capability.available
            and language_ready
        )
        if not conversion_allowed:
            message = pv.blocker.message if pv.blocker else "没有可用的参考字幕"
            raise BadRequestException(message)
        initial = GenState(
            phase="ocr",
            message="正在排队识别图片字幕",
            uses_ocr=True,
            target_language=target_language,
            secondary_language=secondary_language,
            source_candidate_key=pv.selected_source_key,
        )
    else:
        initial = GenState(
            target_language=target_language,
            secondary_language=secondary_language,
            source_candidate_key=pv.selected_source_key,
        )

    # 入队前只组装一次 LLM 路由，不发送网络请求。这样缺少模型配置时由当前
    # POST 直接返回可读错误，不会让页面先看到“任务已开始”，随后又收到一条
    # 后台“未知错误”。排队后配置仍可能被删除，run_generation 另有兜底。
    from movieclaw_api.services.llm_config import acquire_llm_router

    await acquire_llm_router(session)
    if not _tasks.try_start(file_id, initial):
        raise BadRequestException("该文件已有字幕生成任务在进行中")
    return pv


def queue_generation(
    file_id: int,
    target_language: str,
    convert_pgs: bool = False,
    pgs_candidate_key: str | None = None,
    pgs_ocr_language: str | None = None,
    secondary_language: str | None = None,
    source_candidate_key: str | None = None,
) -> asyncio.Task[None]:
    """把已登记的字幕任务交给进程级主管，立即返回且不绑定 HTTP 响应。"""
    if _tasks.state_of(file_id) is None:
        raise RuntimeError(f"字幕任务 #{file_id} 尚未登记，无法入队")
    intent = GenerationIntent(
        file_id=file_id,
        target_language=target_language,
        convert_pgs=convert_pgs,
        pgs_candidate_key=pgs_candidate_key,
        pgs_ocr_language=pgs_ocr_language,
        secondary_language=secondary_language,
        source_candidate_key=source_candidate_key,
    )
    # 先持久化用户已经确认过的任务，再交给事件循环。即使进程在下一行前崩溃，
    # 下次启动也能恢复；正常结论和明确停止会由统一收尾删除这份意图。
    _persist_intent(intent)

    from movieclaw_api.services.task_supervisor import get_task_supervisor

    try:
        return get_task_supervisor().start(
            run_generation(
                file_id,
                target_language,
                convert_pgs,
                pgs_candidate_key,
                pgs_ocr_language,
                secondary_language,
                source_candidate_key,
            ),
            name=f"subtitle-generation-{file_id}",
        )
    except Exception:
        # HTTP 请求尚未返回时入队失败，撤销 start_generation 占住的单飞位，
        # 让用户可以立即重试，而不是留下一个永远不会执行的“进行中”任务。
        _tasks.finish(file_id)
        _discard_intent(intent)
        raise


def resume_interrupted_generations() -> int:
    """启动时恢复仍有持久化意图的任务；返回成功入队数量。"""
    resumed = 0
    for path in sorted(extract.cache_dir().glob("*.job.json")):
        intent = _load_intent(path)
        if intent is None:
            continue
        initial = GenState(
            phase="ocr" if intent.convert_pgs else "preparing",
            message="正在从已保存进度自动续传",
            uses_ocr=intent.convert_pgs,
            target_language=intent.target_language,
            secondary_language=intent.secondary_language,
            source_candidate_key=intent.source_candidate_key,
        )
        if not _tasks.try_start(intent.file_id, initial):
            logger.warning("跳过重复的字幕续传任务：file=%s", intent.file_id)
            continue
        try:
            queue_generation(
                intent.file_id,
                intent.target_language,
                intent.convert_pgs,
                intent.pgs_candidate_key,
                intent.pgs_ocr_language,
                intent.secondary_language,
                intent.source_candidate_key,
            )
        except Exception:  # noqa: BLE001 -- 单个损坏任务不能阻断整个应用启动
            logger.exception("自动续传字幕任务入队失败：file=%s", intent.file_id)
            continue
        resumed += 1
        logger.info(
            "已自动续传字幕任务：file=%s target=%s",
            intent.file_id,
            intent.target_language,
        )
    if resumed:
        logger.info("字幕任务自动续传完成：共 %d 个", resumed)
    return resumed


async def run_generation(
    file_id: int,
    target_language: str,
    convert_pgs: bool = False,
    pgs_candidate_key: str | None = None,
    pgs_ocr_language: str | None = None,
    secondary_language: str | None = None,
    source_candidate_key: str | None = None,
) -> None:
    """后台执行体（自开会话，不向外抛异常；结论落 TaskState.last）。"""
    state = _tasks.state_of(file_id)
    assert state is not None  # start_generation 已登记
    intent = GenerationIntent(
        file_id=file_id,
        target_language=target_language,
        convert_pgs=convert_pgs,
        pgs_candidate_key=pgs_candidate_key,
        pgs_ocr_language=pgs_ocr_language,
        secondary_language=secondary_language,
        source_candidate_key=source_candidate_key,
    )
    result: GenResult | None = None
    interrupted_by_shutdown = False
    try:
        async with _generation_lock:
            if _tasks.stop_requested(file_id):
                raise translate.TranslationAborted("任务在排队期间被用户取消")
            if convert_pgs:
                kwargs = {
                    "convert_pgs": True,
                    "pgs_candidate_key": pgs_candidate_key,
                    "pgs_ocr_language": pgs_ocr_language,
                }
                if secondary_language is not None:
                    kwargs["secondary_language"] = secondary_language
                if source_candidate_key is not None:
                    kwargs["source_candidate_key"] = source_candidate_key
                result = await _run(file_id, target_language, state, **kwargs)
            else:
                # 保持单语原调用形态，避免自动生成和既有扩展点被无关参数破坏。
                if secondary_language is None and source_candidate_key is None:
                    result = await _run(file_id, target_language, state)
                else:
                    kwargs = {}
                    if secondary_language is not None:
                        kwargs["secondary_language"] = secondary_language
                    if source_candidate_key is not None:
                        kwargs["source_candidate_key"] = source_candidate_key
                    result = await _run(file_id, target_language, state, **kwargs)
    except translate.TranslationAborted as exc:
        logger.warning("字幕生成中止：file=%s %s", file_id, exc)
        result = GenResult(ok=False, message=str(exc))
    except asyncio.CancelledError:
        # 服务重载只暂停任务，不把已花费的 LLM 成本作废：translate.Checkpoint
        # 已逐块落在 data/，用户重新发起后会从最后完成块继续。
        logger.info("服务关闭，字幕生成已暂停：file=%s", file_id)
        interrupted_by_shutdown = True
        result = GenResult(
            ok=False,
            message="服务重启，字幕生成已暂停；启动完成后将从已保存进度自动继续",
        )
        raise
    except AppException as exc:
        # AppException 的中文 message 是服务层明确允许展示的业务错误（未配置、
        # 文件丢失等）；保留它才能给非开发者可执行的下一步。任意未知异常仍走
        # 下方兜底，避免把密钥、路径或上游响应泄露到页面。
        logger.warning("字幕生成失败：file=%s %s", file_id, exc.message)
        result = GenResult(ok=False, message=exc.message)
    except Exception:  # noqa: BLE001 -- 后台任务兜底
        logger.exception("字幕生成失败：file=%s", file_id)
        result = GenResult(ok=False, message="生成失败：发生未知错误（详见后端日志）")
    finally:
        if result is not None:
            result.finished_at = utcnow()
        if not interrupted_by_shutdown:
            _discard_intent(intent)
        _tasks.finish(file_id, result=result)


async def _run(
    file_id: int,
    target_language: str,
    state: GenState,
    *,
    convert_pgs: bool = False,
    pgs_candidate_key: str | None = None,
    pgs_ocr_language: str | None = None,
    secondary_language: str | None = None,
    source_candidate_key: str | None = None,
) -> GenResult:
    target_language, secondary_language = ensure_output_languages(
        target_language, secondary_language
    )
    db = get_database()
    async with db.session() as session:
        row = await _load_row(session, file_id)
        ctx, original_language = await _film_context(session, row)
        chat = await _build_chat(session)

    source_target = target_language if secondary_language is None else "__bilingual__"
    ranked = source.rank_candidates(
        row, original_language=original_language, target_language=source_target
    )
    requested_source_key = source_candidate_key or pgs_candidate_key
    try:
        selected = _select_reference(ranked, requested_source_key)
    except BadRequestException as exc:
        return GenResult(ok=False, message=exc.message)
    state.source_candidate_key = candidate_key(selected) if selected is not None else None
    selected_candidates = [selected] if selected is not None else []
    warnings: list[str] = []
    state.phase = "preparing"
    state.message = "正在选择参考字幕"
    chosen, events = await _pick_loadable(row, selected_candidates, warnings)
    source_desc: str | None = None
    if chosen is None and convert_pgs:
        plan = await _pgs_plan(
            row,
            selected_candidates,
            original_language=original_language,
            requested_candidate_key=(candidate_key(selected) if selected is not None else None),
            requested_ocr_language=pgs_ocr_language,
        )
        if plan is None:
            return GenResult(ok=False, message="文件中的 PGS 字幕已变化，请重新预检后再试")
        if not plan.capability.available:
            return GenResult(
                ok=False,
                message=f"当前设备无法识别这份图片字幕：{plan.capability.message}",
            )
        if not plan.capability.cached and plan.language.code is None:
            return GenResult(ok=False, message="原字幕语言尚未确认，请重新检查后再试")
        state.phase = "ocr"
        state.message = "正在识别图片字幕，可能需要一些时间"
        try:
            ocr_path = await pgs.convert_embedded_pgs(
                row,
                plan.candidate.candidate,
                plan.capability,
                plan.language.code,
            )
            raw = await asyncio.to_thread(ocr_path.read_bytes)
            events = extract.parse_events(
                extract.decode_subtitle_bytes(raw, str(ocr_path)), str(ocr_path)
            )
        except (OSError, extract.SourceLoadError, pgs.PgsConversionError) as exc:
            return GenResult(ok=False, message=f"图片字幕识别未完成：{exc}")
        if _tasks.stop_requested(file_id):
            raise translate.TranslationAborted("图片字幕识别完成，已按你的请求停止后续 AI 翻译")
        assessment = source.assess_events(events, row.duration_seconds)
        if not assessment.ok:
            return GenResult(
                ok=False,
                message=f"图片字幕已识别，但完整度检查未通过：{assessment.reason}",
            )
        # OCR 不是只能服务当前这次 AI 翻译的临时步骤。完整度达标后立即规范化
        # 为 UTF-8 SRT 外挂；即使后续模型失败，这份耗时得到的文本以后仍可预览、
        # 校准或作为其他语言任务的参考。
        ocr_sidecar = _pgs_sidecar_path(row, plan.language.code or "und")
        try:
            await asyncio.to_thread(translate.write_srt, events, ocr_sidecar)
        except OSError as exc:
            return GenResult(
                ok=False,
                message=f"图片字幕已识别，但无法保存外挂字幕：{ocr_sidecar}（{exc}）",
            )
        await _refresh_subtitle_inventory(db, file_id)
        chosen = plan.candidate
        chosen.reasons.append(
            f"PGS 以 {plan.language.label or '已确认语言'}"
            f"经 {plan.capability.engine or 'OCR'} 转为 SRT"
        )
        chosen.reasons.append(assessment.reason)
        source_desc = f"{_cand_desc(chosen.candidate)}（OCR 转换）"
    if chosen is None:
        detail = "；".join(warnings) or "当前没有可选择的文本字幕"
        return GenResult(ok=False, message="所选参考字幕无法用于翻译：" + detail)
    if source_desc is None:
        source_desc = _cand_desc(chosen.candidate)
    logger.info(
        "字幕生成选源：%s ← %s（%s）", row.file_path, source_desc, "；".join(chosen.reasons)
    )

    # L1 同步质检（§5.2）：错位参考在烧 LLM 钱之前拦下；无法检测按未知放行
    state.phase = "syncing"
    state.message = "正在检查字幕与影片是否同步"
    state.total_events = len(events)
    sync_score = await sync.sample_sync_score(Path(row.file_path), events, row.duration_seconds)
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

    def _phase(phase: str, message: str) -> None:
        state.phase = phase
        state.message = message
        state.active_blocks = ()

    def _progress(progress: translate.TranslationProgress) -> None:
        state.done_blocks = progress.done_blocks
        state.total_blocks = progress.total_blocks
        state.done_events = progress.done_events
        state.total_events = progress.total_events
        state.active_blocks = progress.active_blocks
        state.parallelism = progress.concurrency

    out_events, stats = await translate.translate_events(
        chat,
        events,
        ctx,
        target_language,
        file_id=file_id,
        secondary_language=secondary_language,
        progress=_progress,
        phase=_phase,
        cancelled=lambda: _tasks.stop_requested(file_id),
    )

    state.phase = "validating"
    state.message = "正在检查字幕质量"
    state.active_blocks = ()
    final_events, report = validate.finalize_events(
        out_events,
        stats.glossary,
        target_language=target_language,
        secondary_language=secondary_language,
    )
    # 超读速二次压缩（§3.3"浓缩优先"的闭环）：只回炉超标子集（通常 <10%），
    # 压缩失败保留原译文——这是增益项，绝不因它废任务
    compressed = 0
    overruns = validate.overrun_indices(
        final_events,
        target_language=target_language,
        secondary_language=secondary_language,
    )
    # 双语的两行就是语种边界，压缩模型再次改写容易破坏严格两行结构；本期
    # 对双语只报告超读速，由首次翻译提示词负责浓缩。
    if overruns and secondary_language is None:
        state.phase = "compressing"
        state.message = f"正在压缩 {len(overruns)} 条超读速译文"
        refined, compressed = await translate.compress_overruns(
            chat,
            final_events,
            overruns,
            ctx,
            target_language,
            secondary_language,
        )
        if compressed:
            final_events, report = validate.finalize_events(
                refined,
                stats.glossary,
                target_language=target_language,
            )
    report.kept_original = stats.kept_original
    report.compressed = compressed

    state.phase = "writing"
    state.message = "正在保存字幕文件"
    sidecar = _sidecar_path(row, target_language, secondary_language)
    try:
        await asyncio.to_thread(translate.write_srt, final_events, sidecar)
    except OSError as exc:
        return GenResult(
            ok=False,
            message=f"字幕文件写入失败（库目录是否只读？）：{sidecar}（{exc}）",
        )
    if secondary_language is None:
        legacy_sidecar = _legacy_sidecar_path(row, target_language)
        if legacy_sidecar != sidecar and legacy_sidecar.exists():
            try:
                await asyncio.to_thread(legacy_sidecar.unlink)
                logger.info("旧版 AI 字幕已迁移为规范文件名：%s → %s", legacy_sidecar, sidecar)
            except OSError as exc:
                # 新文件已经原子写成，旧文件清理失败不应让整次付费生成报失败；
                # 台账会同时展示两份，日志给出管理员可定位的明确原因。
                logger.warning("旧版 AI 字幕清理失败：%s（%s）", legacy_sidecar, exc)
    translate.Checkpoint(
        file_id,
        subtitle_output_key(target_language, secondary_language),
        translate.source_fingerprint(events, target_language, secondary_language),
    ).discard()

    # 台账即时刷新（不等 watchdog）：任务结束播放器立刻可选
    state.phase = "refreshing"
    state.message = "正在更新媒体库字幕列表"
    await _refresh_subtitle_inventory(db, file_id)

    message = f"已生成 {sidecar.name}（参考：{source_desc}，{report.event_count} 条对白"
    if stats.failed_blocks:
        message += f"，{stats.failed_blocks} 块翻译失败保留原文"
    if report.compressed:
        message += f"，压缩改写 {report.compressed} 条超读速译文"
    if report.cps_overrun:
        message += f"，仍有 {report.cps_overrun} 条超读速"
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


async def _refresh_subtitle_inventory(db, file_id: int) -> None:  # noqa: ANN001
    """按磁盘现场刷新单文件字幕台账；PGS 中间产物与 AI 成品共用。"""
    async with db.session() as session:
        row = await _load_row(session, file_id)
        row.external_subtitles = await asyncio.to_thread(
            discover_external_subtitles, Path(row.file_path)
        )
        row.updated_at = utcnow()
        await session.commit()


async def _build_chat(session: AsyncSession) -> translate.ChatFn:
    """movieclaw_llm 路由 → translate 层的 chat 函数（§3.6）。"""
    from movieclaw_api.services.llm_config import acquire_llm_router
    from movieclaw_llm import ChatMessage, ChatRequest, LlmRateLimitError

    router = await acquire_llm_router(session)

    async def chat(system: str, user: str) -> str:
        try:
            response = await router.chat(
                ChatRequest(
                    messages=[
                        ChatMessage(role="system", content=system),
                        ChatMessage(role="user", content=user),
                    ]
                )
            )
        except LlmRateLimitError as exc:
            # 翻译层只认识统一限流信号，不依赖具体供应商或 OpenAI SDK；
            # 调度器据此降低并发、按 Retry-After 退避并重试当前块。
            raise translate.ChatRateLimited(exc.retry_after) from exc
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


#: 校准参考的抽样窗：沿时间轴均布 6 窗 × 2 分钟。稀疏参考不影响互相关
#: 峰位置（匹配发生在有信号处,错位处只加常数底噪）,却把 2 小时片的
#: 音频解码从整片降到 12 分钟——校准在 HTTP 请求内即可完成,不必转后台
_CALIBRATE_WINDOWS = 6
_CALIBRATE_WINDOW_S = 120


async def _audio_reference_intervals(
    row: LibraryFile, events: list[extract.SubEvent]
) -> list[tuple[int, int]] | None:
    """抽样语音区间（校准参考,映射回全片时间坐标）;无 ffmpeg/无音轨返回 None。"""
    runtime = row.duration_seconds or (max(e[1] for e in events) // 1000 if events else 0)
    if runtime <= 0:
        return None
    if runtime <= _CALIBRATE_WINDOWS * _CALIBRATE_WINDOW_S:
        starts = [0]
        window = float(runtime)
    else:
        # 均布起点,避开首尾 5%（片头 logo/片尾字幕常无对白）
        usable = runtime * 0.9
        step = usable / _CALIBRATE_WINDOWS
        starts = [int(runtime * 0.05 + i * step) for i in range(_CALIBRATE_WINDOWS)]
        window = float(_CALIBRATE_WINDOW_S)
    intervals: list[tuple[int, int]] = []
    for start in starts:
        pcm = await asyncio.to_thread(
            sync._extract_pcm_sync, Path(row.file_path), float(start), window
        )
        if pcm is None:
            return intervals if intervals else None  # 首窗就失败=不可用
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
        discovered = await asyncio.to_thread(discover_external_subtitles, Path(row.file_path))
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

    calibration = sync.estimate_calibration(reference, [(s, e) for s, e, _ in events])
    if calibration is None or calibration.score < _CALIBRATE_MIN_SCORE:
        return CalibrateResult(
            ok=False,
            message=f"校准置信度不足（参考：{ref_desc}）——字幕可能与该影片不匹配",
            score=calibration.score if calibration else None,
        )
    if calibration.scale == 1.0 and abs(calibration.offset_ms) <= _NOOP_OFFSET_MS:
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
            await asyncio.to_thread(_shift_subtitle_file, path, calibration)
    except OSError as exc:
        return CalibrateResult(ok=False, message=f"校准结果写入失败（库目录是否只读？）：{exc}")

    async with get_database().session() as refresh_session:
        fresh = await _load_row(refresh_session, file_id)
        fresh.external_subtitles = await asyncio.to_thread(
            discover_external_subtitles, Path(fresh.file_path)
        )
        fresh.updated_at = utcnow()
        await refresh_session.commit()

    logger.info(
        "外挂字幕校准完成：%s scale=%.6f offset=%dms score=%.2f 参考=%s",
        path,
        calibration.scale,
        calibration.offset_ms,
        calibration.score,
        ref_desc,
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
