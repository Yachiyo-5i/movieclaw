"""L1 同步度检测：以音轨为真相源的抽样质量门（subtitle-ai-translate.md §5.2）。

参考字幕若与影片不同步，翻译再好也是全片错位——检测发生在烧 LLM 钱
之前。抽样即可（首/中/尾各 ~2 分钟），不扫全片。

VAD 两级：优先 silero-vad ONNX（模型在 data/models/silero-vad/ 时启用，
走 onnxruntime + 模型 Release 分发的既有惯例）；模型缺失/加载失败降级
**能量法**（帧 RMS 阈值，精度有限但零依赖、确定性可测）。任何一级不可用
（如无 ffmpeg/strm 无本体）返回 None=无法检测，调用方按"未知"处理而非
"不同步"。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np

from movieclaw_api.services.subtitle_gen.extract import SubEvent

logger = logging.getLogger("movieclaw_api.subtitle_gen")

SAMPLE_RATE = 16_000
_WINDOW_SECONDS = 120  # 每个抽样窗时长
_FFMPEG_TIMEOUT = 60.0
#: 事件命中率低于该值判"疑似不同步"（§5.2 阈值；能量法噪声大，宽松取值）
SYNC_THRESHOLD = 0.55


@functools.cache
def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _extract_pcm_sync(video: Path, start_s: float, duration_s: float) -> np.ndarray | None:
    """（线程池）抽一段 16k 单声道 PCM；失败返回 None（无音轨/坏文件）。"""
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        return None
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-v", "error",
                "-ss", str(start_s), "-t", str(duration_s),
                "-i", str(video),
                "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE),
                "-f", "s16le", "-",
            ],
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.warning("同步检测的音频抽样超时：%s @%ss", video, start_s)
        return None
    if proc.returncode != 0 or len(proc.stdout) < SAMPLE_RATE:  # 至少 0.5 秒
        return None
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# VAD：silero 优先，能量法兜底
# ---------------------------------------------------------------------------


def _silero_model_path() -> Path:
    return Path(os.environ.get("MOVIECLAW_VAD_DIR", "data/models/silero-vad")) / (
        "silero_vad.onnx"
    )


@functools.cache
def _silero_session():
    """silero-vad ONNX 会话；模型缺失/加载失败返回 None（降级能量法）。"""
    path = _silero_model_path()
    if not path.is_file():
        return None
    try:
        import onnxruntime

        return onnxruntime.InferenceSession(
            str(path), providers=["CPUExecutionProvider"]
        )
    except Exception as exc:  # noqa: BLE001 -- 模型/运行时问题一律降级
        logger.warning("silero-vad 模型加载失败，同步检测降级为能量法：%s", exc)
        return None


def _speech_mask_silero(pcm: np.ndarray) -> np.ndarray | None:
    """silero v5 逐 512 采样帧推理 → 每帧是否语音；失败返回 None。"""
    session = _silero_session()
    if session is None:
        return None
    try:
        frame = 512
        n = len(pcm) // frame
        state = np.zeros((2, 1, 128), dtype=np.float32)
        sr = np.array(SAMPLE_RATE, dtype=np.int64)
        probs = np.empty(n, dtype=np.float32)
        for i in range(n):
            chunk = pcm[i * frame : (i + 1) * frame].reshape(1, -1)
            out, state = session.run(
                None, {"input": chunk, "state": state, "sr": sr}
            )[:2]
            probs[i] = float(out.reshape(-1)[0])
        return probs > 0.5
    except Exception as exc:  # noqa: BLE001 -- 推理形状不符等一律降级
        logger.warning("silero-vad 推理失败，同步检测降级为能量法：%s", exc)
        return None


def _speech_mask_energy(pcm: np.ndarray, frame: int = 512) -> np.ndarray:
    """能量法 VAD：帧 RMS 高于自适应阈值即语音（精度有限的确定性兜底）。"""
    n = len(pcm) // frame
    if n == 0:
        return np.zeros(0, dtype=bool)
    frames = pcm[: n * frame].reshape(n, frame)
    rms = np.sqrt((frames**2).mean(axis=1))
    # 阈值 = 底噪（20 分位）与峰值间的折中；全静音段不误报
    floor = np.percentile(rms, 20)
    threshold = max(floor * 3.0, 0.01)
    return rms > threshold


def speech_intervals(pcm: np.ndarray) -> list[tuple[int, int]]:
    """PCM → 语音区间毫秒表（silero 优先，能量法兜底；合并 300ms 内的间隙）。"""
    mask = _speech_mask_silero(pcm)
    if mask is None:
        mask = _speech_mask_energy(pcm)
    frame_ms = 512 * 1000 / SAMPLE_RATE
    intervals: list[tuple[int, int]] = []
    start: int | None = None
    for i, hit in enumerate(mask):
        if hit and start is None:
            start = int(i * frame_ms)
        elif not hit and start is not None:
            intervals.append((start, int(i * frame_ms)))
            start = None
    if start is not None:
        intervals.append((start, int(len(mask) * frame_ms)))
    # 合并近邻（说话间的自然停顿不该断开区间）
    merged: list[tuple[int, int]] = []
    for s, e in intervals:
        if merged and s - merged[-1][1] <= 300:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def event_hit_rate(
    speech: list[tuple[int, int]], events: list[tuple[int, int]]
) -> float | None:
    """字幕事件与语音区间的命中率：有任一重叠即命中；无事件返回 None。"""
    if not events:
        return None
    hits = 0
    for es, ee in events:
        for ss, se in speech:
            if es < se and ss < ee:
                hits += 1
                break
    return hits / len(events)


async def sample_sync_score(
    video_path: Path, events: list[SubEvent], runtime_seconds: int | None
) -> float | None:
    """抽样同步度（0..1）；None=无法检测（无 ffmpeg/无音轨/事件太少）。

    首/中/尾三窗各 2 分钟，逐窗算事件命中率后取平均；窗内事件不足 5 条
    的窗跳过（片头片尾常无对白，不该拉低分数）。
    """
    if _ffmpeg() is None:
        logger.info("系统中未找到 ffmpeg，跳过字幕同步度检测（结果按未知处理）")
        return None
    runtime = runtime_seconds or (max(e[1] for e in events) // 1000 if events else 0)
    if runtime < _WINDOW_SECONDS * 2 or len(events) < 20:
        return None  # 短片/事件太少，抽样无统计意义
    starts = [
        runtime * 0.1,
        runtime * 0.5 - _WINDOW_SECONDS / 2,
        runtime * 0.9 - _WINDOW_SECONDS,
    ]
    scores: list[float] = []
    for start_s in starts:
        start_ms, end_ms = int(start_s * 1000), int((start_s + _WINDOW_SECONDS) * 1000)
        window_events = [
            (max(0, s - start_ms), e - start_ms)
            for s, e, _ in events
            if s < end_ms and e > start_ms
        ]
        if len(window_events) < 5:
            continue
        pcm = await asyncio.to_thread(
            _extract_pcm_sync, video_path, start_s, float(_WINDOW_SECONDS)
        )
        if pcm is None:
            return None  # 音频抽不出来：整个检测按未知处理
        rate = event_hit_rate(speech_intervals(pcm), window_events)
        if rate is not None:
            scores.append(rate)
    if not scores:
        return None
    return sum(scores) / len(scores)
