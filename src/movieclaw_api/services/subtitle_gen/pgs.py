"""PGS → SRT 适配层：能力检测、轨道抽取与 ``seconv`` OCR。

这一层只负责把内封 PGS 变成可复用的文本中间品，不参与选源、翻译和
最终 sidecar 命名。官方 Docker 镜像按目标架构内置 Subtitle Edit 5.1 的
``seconv`` 与 Tesseract；源码直跑时也可从 ``PATH`` 或
``MOVIECLAW_SECONV_PATH`` 使用用户安装的版本。

跨平台边界在真正启动任务前完成检测：只接受 Subtitle Edit 官方提供的
Windows/Linux/macOS x64/ARM64 组合，并分别校验 ffmpeg、seconv、OCR
引擎及语言包。检测失败返回结构化中文说明，绝不等后台任务启动后才静默跳过。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from movieclaw_api.services.subtitle_gen.source import SourceCandidate
    from movieclaw_db.models import LibraryFile


PGS_CODECS = frozenset({"hdmv_pgs_subtitle", "pgs", "sup"})

_SECONV_PROBE_TIMEOUT = 10.0
_OCR_PROBE_TIMEOUT = 20.0
_EXTRACT_TIMEOUT = 180.0
_OCR_TIMEOUT = 3600.0

_ARCH_ALIASES = {
    "amd64": "x64",
    "x86_64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
_PLATFORM_LABELS = {
    "darwin": "macOS",
    "linux": "Linux",
    "win32": "Windows",
}

# seconv 对 Tesseract 传语言码；中文需从媒体元数据的 ISO 639 写法转换成
# Tesseract 的 chi_sim/chi_tra，其他常见语言沿用三字码。
_TESSERACT_LANGUAGES = {
    "chi": "chi_sim",
    "chs": "chi_sim",
    "zho": "chi_sim",
    "cht": "chi_tra",
    "eng": "eng",
    "fre": "fra",
    "fra": "fra",
    "deu": "deu",
    "ger": "deu",
    "jpn": "jpn",
    "kor": "kor",
    "spa": "spa",
    "ita": "ita",
    "por": "por",
    "rus": "rus",
}

# PaddleOCR 的命令行语言码与 Tesseract 不同。只映射项目能明确确认的常见
# 字幕语言；未知语言宁可在预检提示，也不默认用英语产出一整部乱码字幕。
_PADDLE_LANGUAGES = {
    "chi": "ch",
    "chs": "ch",
    "zho": "ch",
    "cht": "chinese_cht",
    "eng": "en",
    "fre": "fr",
    "fra": "fr",
    "deu": "german",
    "ger": "german",
    "jpn": "japan",
    "kor": "korean",
    "spa": "es",
    "ita": "it",
    "por": "pt",
    "rus": "ru",
}

# 用户确认的是“PGS 图片里写的语言”，不是 Tesseract/Paddle 的内部参数。
# 对外统一使用项目已有的 ISO 639-2/B 风格 token，简繁中文单独保留，避免
# 通用字幕语言规范化把 ``cht`` 合并成 ``chi`` 后选错识别模型。
OCR_LANGUAGE_LABELS: dict[str, str] = {
    "eng": "英语",
    "chs": "简体中文",
    "cht": "繁体中文",
    "jpn": "日语",
    "kor": "韩语",
    "fre": "法语",
    "ger": "德语",
    "spa": "西班牙语",
    "ita": "意大利语",
    "por": "葡萄牙语",
    "rus": "俄语",
}

_OCR_LANGUAGE_ALIASES = {
    "en": "eng",
    "eng": "eng",
    "english": "eng",
    "chs": "chs",
    "zh-cn": "chs",
    "zh-hans": "chs",
    "简体": "chs",
    "简中": "chs",
    "cht": "cht",
    "zh-tw": "cht",
    "zh-hant": "cht",
    "繁体": "cht",
    "繁體": "cht",
    "繁中": "cht",
    # chi/zho/zh 不携带简繁信息；用于自动推荐时必须让用户确认。
    "chi": "chs",
    "zho": "chs",
    "zh": "chs",
    "ja": "jpn",
    "jp": "jpn",
    "jpn": "jpn",
    "ko": "kor",
    "kor": "kor",
    "fr": "fre",
    "fre": "fre",
    "fra": "fre",
    "de": "ger",
    "ger": "ger",
    "deu": "ger",
    "es": "spa",
    "spa": "spa",
    "it": "ita",
    "ita": "ita",
    "pt": "por",
    "por": "por",
    "ru": "rus",
    "rus": "rus",
}

_AMBIGUOUS_CHINESE_CODES = frozenset({"chi", "zho", "zh", "chinese"})
_UNKNOWN_LANGUAGE_CODES = frozenset({"", "und", "unk", "unknown", "none"})
_TITLE_LANGUAGE_HINTS = (
    (("繁体", "繁體", "繁中", "traditional chinese", "zh-hant"), "cht"),
    (("简体", "簡體", "简中", "簡中", "simplified chinese", "zh-hans"), "chs"),
    (("english", "英语", "英語"), "eng"),
    (("japanese", "日本語", "日语", "日語"), "jpn"),
    (("korean", "한국어", "韩语", "韓語"), "kor"),
    (("french", "français", "法语", "法語"), "fre"),
    (("german", "deutsch", "德语", "德語"), "ger"),
    (("spanish", "español", "西班牙语", "西班牙語"), "spa"),
    (("italian", "italiano", "意大利语", "義大利語"), "ita"),
    (("portuguese", "português", "葡萄牙语", "葡萄牙語"), "por"),
    (("russian", "русский", "俄语", "俄語"), "rus"),
)


class PgsConversionError(Exception):
    """PGS 转换失败；异常文本可直接展示给部署者。"""


@dataclass(frozen=True)
class Capability:
    """当前主机转换某条 PGS 的完整能力结论。"""

    available: bool
    platform: str
    architecture: str
    engine: str | None
    ocr_language: str | None
    cached: bool
    message: str
    suggestions: tuple[str, ...] = ()
    seconv_path: str | None = None


@dataclass(frozen=True)
class OcrLanguageDecision:
    """PGS 识别语言的推断结论；不确定时由确认框覆盖 ``code``。"""

    code: str | None
    label: str | None
    confirmation_required: bool
    reason: str


def is_pgs_codec(codec: str | None) -> bool:
    return str(codec or "").strip().lower() in PGS_CODECS


def normalize_ocr_language(language: str | None) -> str | None:
    """把轨道/用户语言写法收敛为 OCR 对外 token；不支持时返回 ``None``。"""
    raw = str(language or "").strip().lower()
    return _OCR_LANGUAGE_ALIASES.get(raw)


def _title_language_hint(title: str | None) -> str | None:
    normalized = str(title or "").strip().casefold()
    for hints, code in _TITLE_LANGUAGE_HINTS:
        if any(hint.casefold() in normalized for hint in hints):
            return code
    return None


def infer_ocr_language(
    file: LibraryFile,
    candidate: SourceCandidate,
    original_language: str | None,
) -> OcrLanguageDecision:
    """按轨道元数据 → 轨道标题 → 影片原语言推断，异常时要求用户确认。"""
    raw_language = ""
    title = ""
    try:
        stream = (file.subtitle_streams or [])[int(candidate.key)]
        raw_language = str(stream.get("language") or "").strip()
        title = str(stream.get("title") or "").strip()
    except (IndexError, TypeError, ValueError):
        raw_language = str(candidate.language or "").strip()

    raw_key = raw_language.lower()
    metadata_code = normalize_ocr_language(raw_language)
    title_code = _title_language_hint(title)

    if metadata_code and title_code:
        if raw_key in _AMBIGUOUS_CHINESE_CODES and title_code in {"chs", "cht"}:
            return OcrLanguageDecision(
                title_code,
                OCR_LANGUAGE_LABELS[title_code],
                False,
                f"根据轨道标题“{title}”识别为 {OCR_LANGUAGE_LABELS[title_code]}",
            )
        if metadata_code != title_code:
            return OcrLanguageDecision(
                title_code,
                OCR_LANGUAGE_LABELS[title_code],
                True,
                (
                    f"轨道语言标记“{raw_language}”与标题“{title}”不一致，"
                    f"暂按标题推荐 {OCR_LANGUAGE_LABELS[title_code]}"
                ),
            )
        return OcrLanguageDecision(
            metadata_code,
            OCR_LANGUAGE_LABELS[metadata_code],
            False,
            f"根据轨道语言标记自动识别为 {OCR_LANGUAGE_LABELS[metadata_code]}",
        )

    if metadata_code:
        ambiguous = raw_key in _AMBIGUOUS_CHINESE_CODES
        return OcrLanguageDecision(
            metadata_code,
            OCR_LANGUAGE_LABELS[metadata_code],
            ambiguous,
            (
                "轨道只标记为中文，无法区分简体或繁体，已暂时推荐简体中文"
                if ambiguous
                else f"根据轨道语言标记自动识别为 {OCR_LANGUAGE_LABELS[metadata_code]}"
            ),
        )

    if title_code:
        return OcrLanguageDecision(
            title_code,
            OCR_LANGUAGE_LABELS[title_code],
            False,
            f"根据轨道标题“{title}”自动识别为 {OCR_LANGUAGE_LABELS[title_code]}",
        )

    original_code = normalize_ocr_language(original_language)
    if original_code and raw_key in _UNKNOWN_LANGUAGE_CODES:
        return OcrLanguageDecision(
            original_code,
            OCR_LANGUAGE_LABELS[original_code],
            True,
            (
                "轨道没有语言标记，"
                f"根据影片原语言暂时推荐 {OCR_LANGUAGE_LABELS[original_code]}"
            ),
        )

    if raw_key not in _UNKNOWN_LANGUAGE_CODES:
        reason = f"轨道语言标记“{raw_language}”不在当前 OCR 语言范围内"
    else:
        reason = "轨道和影片都没有可确认的语言信息"
    return OcrLanguageDecision(None, None, True, reason)


def _runtime() -> tuple[str, str, str | None]:
    """返回展示平台、规范架构与不兼容原因。"""
    platform_name = _PLATFORM_LABELS.get(sys.platform)
    raw_arch = platform.machine().strip().lower()
    architecture = _ARCH_ALIASES.get(raw_arch)
    if platform_name is None:
        return sys.platform or "未知系统", raw_arch or "未知架构", (
            "当前操作系统不在 seconv 官方支持范围内；仅支持 Windows、Linux 和 macOS"
        )
    if architecture is None:
        return platform_name, raw_arch or "未知架构", (
            f"当前 {platform_name} 架构 {raw_arch or '未知'} 不受支持；"
            "仅支持 64 位 x64 与 ARM64，32 位 NAS 无法自动转换 PGS"
        )
    return platform_name, architecture, None


def _resolve_seconv() -> str | None:
    override = os.environ.get("MOVIECLAW_SECONV_PATH", "").strip()
    candidates = [override] if override else []
    if sys.platform == "linux":
        candidates.append("/opt/movieclaw/seconv/seconv")
    candidates.extend(filter(None, (shutil.which("seconv"), shutil.which("seconv.exe"))))
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _run_probe(argv: list[str], timeout: float) -> tuple[bool, str]:
    """运行轻量健康探针；收敛各平台的启动异常和短错误文本。"""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (proc.stdout or proc.stderr or "").strip().replace("\n", " ")[:240]
    return proc.returncode == 0, output


def _tesseract_languages(executable: str) -> tuple[set[str], str | None]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [executable, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=_OCR_PROBE_TIMEOUT,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return set(), str(exc)
    if proc.returncode != 0:
        return set(), (proc.stderr or proc.stdout or "未知错误").strip()[:240]
    lines = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return {line for line in lines if " " not in line}, None


def _base_capability(
    *,
    available: bool,
    platform_name: str,
    architecture: str,
    message: str,
    suggestions: tuple[str, ...] = (),
    engine: str | None = None,
    ocr_language: str | None = None,
    seconv_path: str | None = None,
) -> Capability:
    return Capability(
        available=available,
        platform=platform_name,
        architecture=architecture,
        engine=engine,
        ocr_language=ocr_language,
        cached=False,
        message=message,
        suggestions=suggestions,
        seconv_path=seconv_path,
    )


def detect_capability(language: str | None) -> Capability:
    """检测当前设备能否把指定语言的 PGS 转为 SRT。

    ``MOVIECLAW_PGS_OCR_ENGINE`` 可设为 ``paddle`` 或 ``tesseract``；默认
    ``auto`` 优先选择已安装且健康的 PaddleOCR，失败后再尝试 Tesseract。
    """
    platform_name, architecture, runtime_error = _runtime()
    if runtime_error:
        return _base_capability(
            available=False,
            platform_name=platform_name,
            architecture=architecture,
            message=runtime_error,
            suggestions=("请改用 x64/ARM64 主机执行 OCR，或手动添加 SRT/ASS/VTT 字幕",),
        )

    if shutil.which("ffmpeg") is None:
        return _base_capability(
            available=False,
            platform_name=platform_name,
            architecture=architecture,
            message="系统中未找到 ffmpeg，无法从媒体文件抽取 PGS 轨道",
            suggestions=("安装 ffmpeg 并确保命令位于 PATH；官方 Docker 镜像已内置",),
        )

    seconv = _resolve_seconv()
    if seconv is None:
        return _base_capability(
            available=False,
            platform_name=platform_name,
            architecture=architecture,
            message=f"当前 {platform_name} {architecture} 未找到 Subtitle Edit seconv",
            suggestions=(
                "安装与当前系统和架构匹配的 SeConv 5.1，并加入 PATH",
                "也可通过 MOVIECLAW_SECONV_PATH 指向 seconv 可执行文件",
                "官方 Docker 镜像会按 amd64/arm64 自动内置正确版本",
            ),
        )
    healthy, detail = _run_probe([seconv, "--version"], _SECONV_PROBE_TIMEOUT)
    if not healthy:
        return _base_capability(
            available=False,
            platform_name=platform_name,
            architecture=architecture,
            message=f"seconv 无法在当前设备启动：{detail or '未返回版本信息'}",
            suggestions=("确认下载的 seconv 架构正确，并检查其系统动态库依赖",),
            seconv_path=seconv,
        )

    raw_lang = str(language or "").strip().lower()
    lang = normalize_ocr_language(raw_lang) or raw_lang
    requested = os.environ.get("MOVIECLAW_PGS_OCR_ENGINE", "auto").strip().lower()
    if requested not in {"auto", "paddle", "tesseract"}:
        return _base_capability(
            available=False,
            platform_name=platform_name,
            architecture=architecture,
            message=(
                "MOVIECLAW_PGS_OCR_ENGINE 配置无效："
                f"{requested!r}（只允许 auto、paddle、tesseract）"
            ),
            seconv_path=seconv,
        )

    paddle_error: str | None = None
    if requested in {"auto", "paddle"}:
        paddle_language = _PADDLE_LANGUAGES.get(lang)
        paddle = shutil.which("paddleocr")
        if not paddle_language:
            paddle_error = f"PGS 语言 {lang or '未知'} 没有可确认的 PaddleOCR 语言映射"
        elif paddle is None:
            paddle_error = "未找到 paddleocr 命令"
        else:
            paddle_ok, paddle_detail = _run_probe([paddle, "--help"], _OCR_PROBE_TIMEOUT)
            if paddle_ok:
                return _base_capability(
                    available=True,
                    platform_name=platform_name,
                    architecture=architecture,
                    engine="paddle",
                    ocr_language=paddle_language,
                    message=(
                        f"可使用 PaddleOCR（{paddle_language}）将 PGS 转为临时 SRT；"
                        "转换完成并通过完整度检查后才会调用 AI 翻译"
                    ),
                    seconv_path=seconv,
                )
            paddle_error = f"paddleocr 无法启动：{paddle_detail or '未知错误'}"
        if requested == "paddle":
            return _base_capability(
                available=False,
                platform_name=platform_name,
                architecture=architecture,
                message=paddle_error,
                suggestions=(
                    "安装可用的 PaddleOCR，或将 MOVIECLAW_PGS_OCR_ENGINE 改为 auto/tesseract",
                ),
                seconv_path=seconv,
            )

    tesseract_language = _TESSERACT_LANGUAGES.get(lang)
    tesseract = shutil.which("tesseract")
    if not tesseract_language:
        message = f"PGS 语言 {lang or '未知'} 无法自动选择 OCR 语言包"
    elif tesseract is None:
        message = "未找到 Tesseract OCR"
    else:
        installed, error = _tesseract_languages(tesseract)
        if error:
            message = f"Tesseract 无法读取语言包列表：{error}"
        elif tesseract_language not in installed:
            message = f"Tesseract 缺少 {tesseract_language} 语言包"
        else:
            return _base_capability(
                available=True,
                platform_name=platform_name,
                architecture=architecture,
                engine="tesseract",
                ocr_language=tesseract_language,
                message=(
                    f"可使用 Tesseract（{tesseract_language}）将 PGS 转为临时 SRT；"
                    "转换完成并通过完整度检查后才会调用 AI 翻译"
                ),
                seconv_path=seconv,
            )

    details = message
    if paddle_error and requested == "auto":
        details = f"{message}；PaddleOCR 也不可用（{paddle_error}）"
    return _base_capability(
        available=False,
        platform_name=platform_name,
        architecture=architecture,
        message=details,
        suggestions=(
            f"安装与字幕语言匹配的 OCR 语言包（当前轨道：{lang or '未知语言'}）",
            "也可以为影片添加可直接翻译的 SRT、ASS 或 VTT 外挂字幕",
        ),
        seconv_path=seconv,
    )


def available_ocr_languages() -> tuple[tuple[tuple[str, str], ...], Capability | None]:
    """返回当前设备真正可用的用户语言选项及一份代表性环境结论。

    只在语言缺失或冲突时调用；异常轨很少，逐语言复用完整探针可确保源码部署
    缺少部分 traineddata 时，下拉框不会给出实际不可用的选项。
    """
    options: list[tuple[str, str]] = []
    first_available: Capability | None = None
    first_failure: Capability | None = None
    for code, label in OCR_LANGUAGE_LABELS.items():
        capability = detect_capability(code)
        if capability.available:
            options.append((code, label))
            if first_available is None:
                first_available = capability
        elif first_failure is None:
            first_failure = capability
    return tuple(options), first_available or first_failure


def _cache_stem(
    file: LibraryFile,
    candidate: SourceCandidate,
    source_language: str | None = None,
) -> str:
    # 语言来自媒体元数据，不能直接进入文件名；Windows 对 ``<>:\\|?*`` 等字符
    # 更严格，统一收敛为 ASCII 字母数字与连字符，保证同一缓存名跨平台可用。
    raw_language = str(source_language or candidate.language or "und").strip().lower()
    language = "".join(
        char if char.isascii() and (char.isalnum() or char == "-") else "-"
        for char in raw_language
    ).strip("-")
    language = language[:16] or "und"
    return f"{file.id}.embedded{candidate.key}.{language}.pgs"


def cached_srt_path(
    file: LibraryFile,
    candidate: SourceCandidate,
    source_language: str | None = None,
) -> Path:
    from movieclaw_api.services.subtitle_gen.extract import cache_dir

    return cache_dir() / f"{_cache_stem(file, candidate, source_language)}.srt"


def cached_sup_path(file: LibraryFile, candidate: SourceCandidate) -> Path:
    """SUP 与识别语言无关，同一轨道重新 OCR 时复用一份抽取缓存。"""
    from movieclaw_api.services.subtitle_gen.extract import cache_dir

    return cache_dir() / f"{file.id}.embedded{candidate.key}.pgs.sup"


def _is_fresh(path: Path, video: Path) -> bool:
    try:
        return (
            path.is_file()
            and path.stat().st_size > 0
            and path.stat().st_mtime_ns > video.stat().st_mtime_ns
        )
    except OSError:
        return False


def conversion_capability(
    file: LibraryFile,
    candidate: SourceCandidate,
    source_language: str | None = None,
) -> Capability:
    """缓存命中时无需运行时 OCR；否则执行完整设备检测。"""
    video = Path(file.file_path)
    cached = cached_srt_path(file, candidate, source_language)
    if _is_fresh(cached, video):
        platform_name, architecture, _ = _runtime()
        return Capability(
            available=True,
            platform=platform_name,
            architecture=architecture,
            engine="cache",
            ocr_language=None,
            cached=True,
            message="已有与当前媒体文件匹配的 PGS OCR 缓存，将复用后直接开始翻译",
        )
    return detect_capability(source_language or candidate.language)


def _extract_sup_sync(video: Path, stream_index: int, out_path: Path) -> None:
    if _is_fresh(out_path, video):
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f".{out_path.stem}.{uuid.uuid4().hex}.part.sup")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(video),
                "-map",
                f"0:s:{stream_index}",
                "-c:s",
                "copy",
                str(tmp_path),
            ],
            capture_output=True,
            timeout=_EXTRACT_TIMEOUT,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise PgsConversionError(
            f"PGS 轨道抽取超过 {_EXTRACT_TIMEOUT:.0f} 秒：{video}"
        ) from exc
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise PgsConversionError(f"无法启动 ffmpeg 抽取 PGS：{exc}") from exc
    if proc.returncode != 0 or not tmp_path.is_file() or tmp_path.stat().st_size == 0:
        error = proc.stderr.decode(errors="replace").strip()[:300]
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise PgsConversionError(
            f"PGS 轨道抽取失败：{video} 字幕轨 {stream_index}（{error or '未知错误'}）"
        )
    try:
        tmp_path.replace(out_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise PgsConversionError(f"PGS 缓存写入失败：{out_path}（{exc}）") from exc


def _convert_sync(
    file: LibraryFile,
    candidate: SourceCandidate,
    capability: Capability,
    source_language: str | None,
) -> Path:
    video = Path(file.file_path)
    out_path = cached_srt_path(file, candidate, source_language)
    if _is_fresh(out_path, video):
        return out_path
    if not capability.available or not capability.seconv_path:
        raise PgsConversionError(capability.message)
    if not capability.engine or capability.engine == "cache" or not capability.ocr_language:
        raise PgsConversionError("PGS OCR 能力检测结果不完整，请重新预检后再试")

    sup_path = cached_sup_path(file, candidate)
    _extract_sup_sync(video, int(candidate.key), sup_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        with tempfile.TemporaryDirectory(prefix="pgs-ocr-", dir=out_path.parent) as temp:
            temp_dir = Path(temp)
            argv = [
                capability.seconv_path,
                str(sup_path),
                "subrip",
                f"--ocr-engine:{capability.engine}",
                f"--ocr-language:{capability.ocr_language}",
                f"--output-folder:{temp_dir}",
                "--overwrite",
            ]
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=_OCR_TIMEOUT,
                    creationflags=creationflags,
                )
            except subprocess.TimeoutExpired as exc:
                raise PgsConversionError(
                    f"PGS OCR 超过 {_OCR_TIMEOUT / 60:.0f} 分钟，已停止转换"
                ) from exc
            except OSError as exc:
                raise PgsConversionError(f"无法启动 seconv：{exc}") from exc
            outputs = sorted(
                path
                for path in temp_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".srt"
            )
            if proc.returncode != 0 or not outputs or outputs[0].stat().st_size == 0:
                detail = (proc.stderr or proc.stdout or "未知错误").strip().replace("\n", " ")
                raise PgsConversionError(f"PGS OCR 转换失败：{detail[:400]}")
            try:
                outputs[0].replace(out_path)
            except OSError as exc:
                raise PgsConversionError(f"PGS OCR 结果写入缓存失败：{out_path}（{exc}）") from exc
    except OSError as exc:
        raise PgsConversionError(f"无法创建 PGS OCR 临时目录：{exc}") from exc
    return out_path


async def convert_embedded_pgs(
    file: LibraryFile,
    candidate: SourceCandidate,
    capability: Capability | None = None,
    source_language: str | None = None,
) -> Path:
    """把一个内封 PGS 候选转换成缓存 SRT，命中缓存时不重复 OCR。"""
    if candidate.kind != "embedded" or not is_pgs_codec(candidate.format):
        raise PgsConversionError("所选字幕不是可转换的内封 PGS 轨道")
    current = (
        conversion_capability(file, candidate, source_language)
        if capability is None
        else capability
    )
    if current.cached:
        return cached_srt_path(file, candidate, source_language)
    if not current.available:
        raise PgsConversionError(current.message)
    return await asyncio.to_thread(
        _convert_sync,
        file,
        candidate,
        current,
        source_language,
    )
