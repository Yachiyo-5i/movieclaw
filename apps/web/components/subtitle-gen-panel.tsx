"use client";

/**
 * AI 字幕生成与校准入口（docs/design/subtitle-ai-translate.md §6，G1/G2）。
 *
 * 挂在文件规格区下方（SpecRows 同容器），管理员可见：
 * - 「AI 生成中文字幕」：预检（选源结果 + 成本估算）→ 确认 → 后台任务 →
 *   2 秒轮询块进度 → 结论（成功/失败/同步门拦截的中文说明）；
 * - 外挂字幕的「校准时间轴」：零 LLM 成本的一键工具，结果即时提示。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { LibraryItemFile } from "@/lib/api/libraries";
import {
  calibrateSubtitle,
  subgenPreview,
  subgenStart,
  subgenStatus,
  subgenStop,
  type SubgenPreview,
  type SubgenProgress,
} from "@/lib/api/subtitle-gen";
import { usePermissions } from "@/lib/permissions";

const BUTTON_CLASS =
  "rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-1 text-sub " +
  "text-[var(--text-secondary)] transition hover:bg-white/[0.09] hover:text-white " +
  "disabled:pointer-events-none disabled:opacity-40";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请稍后再试";
}

export function SubtitleGenPanel({ file }: { file: LibraryItemFile }) {
  const { isAdmin } = usePermissions();
  const [preview, setPreview] = useState<SubgenPreview | null>(null);
  const [progress, setProgress] = useState<SubgenProgress | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const poll = useCallback(async () => {
    try {
      const status = await subgenStatus(file.id);
      setProgress(status);
      if (!status.running) {
        stopPolling();
        if (status.last_result) setNotice(status.last_result.message);
      }
    } catch {
      // 轮询失败不打断页面；下一轮重试
    }
  }, [file.id, stopPolling]);

  const startPolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(() => void poll(), 2000);
  }, [poll, stopPolling]);

  // 进入页面时探一次：任务可能在别处发起后仍在跑
  useEffect(() => {
    void poll();
    return stopPolling;
  }, [poll, stopPolling]);
  useEffect(() => {
    if (progress?.running && pollRef.current === null) startPolling();
  }, [progress?.running, startPolling]);

  const onPreview = useCallback(async () => {
    setBusy(true);
    setNotice(null);
    try {
      const pv = await subgenPreview(file.id);
      setPreview(pv);
      setConfirming(true);
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }, [file.id]);

  const onStart = useCallback(async () => {
    setBusy(true);
    try {
      await subgenStart(file.id);
      setConfirming(false);
      setProgress({
        running: true,
        phase: "preparing",
        message: "正在准备",
        done_blocks: 0,
        total_blocks: 0,
        last_result: null,
      });
      startPolling();
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }, [file.id, startPolling]);

  const onStop = useCallback(async () => {
    try {
      await subgenStop(file.id);
    } catch (error) {
      setNotice(errorMessage(error));
    }
  }, [file.id]);

  const onCalibrate = useCallback(
    async (filename: string) => {
      setBusy(true);
      setNotice(null);
      try {
        const result = await calibrateSubtitle(file.id, filename);
        setNotice(result.message);
      } catch (error) {
        setNotice(errorMessage(error));
      } finally {
        setBusy(false);
      }
    },
    [file.id],
  );

  if (!isAdmin || file.missing) return null;

  const running = progress?.running ?? false;
  const externals = file.subtitle_streams.filter((s) => s.external && s.file_name);
  const chosen = preview?.candidates.find(
    (c) => `${c.kind}:${c.key}` === preview.chosen_key,
  );

  return (
    <div className="mt-3 space-y-2 border-t border-white/[0.06] pt-3">
      <div className="flex flex-wrap items-center gap-2">
        {running ? (
          <>
            <span className="text-sub text-[var(--text-secondary)]">
              {progress?.message}
              {progress && progress.total_blocks > 0 && (
                <>
                  ：{progress.done_blocks}/{progress.total_blocks} 块
                </>
              )}
            </span>
            <button type="button" className={BUTTON_CLASS} onClick={() => void onStop()}>
              停止
            </button>
          </>
        ) : (
          <button
            type="button"
            className={BUTTON_CLASS}
            disabled={busy}
            onClick={() => void onPreview()}
          >
            AI 生成中文字幕
          </button>
        )}
        {externals.map((s) => (
          <button
            key={s.file_name}
            type="button"
            className={BUTTON_CLASS}
            disabled={busy || running}
            title={`校准「${s.file_name}」的时间轴（以音轨为参考，零模型成本）`}
            onClick={() => void onCalibrate(s.file_name!)}
          >
            校准 {s.title || s.file_name}
          </button>
        ))}
      </div>

      {confirming && preview && (
        <div className="space-y-2 rounded-xl border border-white/[0.08] bg-black/25 p-3 text-sub">
          {chosen ? (
            <>
              <div className="text-[var(--text-secondary)]">
                参考字幕：
                {chosen.kind === "embedded" ? `内封轨 ${chosen.key}` : chosen.key}
                {chosen.language ? `（${chosen.language}）` : ""} · {preview.event_count} 条对白 ·
                预计消耗约 {Math.round(preview.estimated_tokens / 1000)}k token
              </div>
              <ul className="list-inside list-disc text-[var(--text-muted)]">
                {chosen.reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
              {preview.already_generated && (
                <div className="text-[var(--text-muted)]">已有 AI 字幕，本次生成将覆盖</div>
              )}
              <div className="flex gap-2">
                <button
                  type="button"
                  className={BUTTON_CLASS}
                  disabled={busy}
                  onClick={() => void onStart()}
                >
                  确认生成
                </button>
                <button
                  type="button"
                  className={BUTTON_CLASS}
                  onClick={() => setConfirming(false)}
                >
                  取消
                </button>
              </div>
            </>
          ) : (
            <div className="text-[var(--text-muted)]">
              没有可用的参考字幕{preview.warnings.length > 0 && `：${preview.warnings.join("；")}`}
            </div>
          )}
        </div>
      )}

      {notice && <div className="text-sub text-[var(--text-muted)]">{notice}</div>}
    </div>
  );
}
