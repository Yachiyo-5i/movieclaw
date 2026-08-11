import { request } from "@/lib/http";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse） */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

async function unwrap<T>(promise: Promise<ApiEnvelope<T>>): Promise<T> {
  return (await promise).data;
}

/**
 * AI 字幕生成（docs/design/subtitle-ai-translate.md）：选源 → 同步质检 →
 * LLM 翻译 → 机检 → 落盘外挂 sidecar。管理员专属（消费 LLM 配额）。
 */

/** 一个候选参考字幕的打分结论（预检确认框展示）。 */
export interface SubgenCandidate {
  kind: "embedded" | "external";
  key: string;
  language: string | null;
  format: string | null;
  /** 非空 = 被排除原因（forced/目标语言/图形轨） */
  excluded: string | null;
  /** 打分明细："为什么选了这条"要能看懂 */
  reasons: string[];
}

export interface SubgenPreview {
  candidates: SubgenCandidate[];
  /** "kind:key"；null = 没有可用参考 */
  chosen_key: string | null;
  event_count: number;
  estimated_tokens: number;
  /** 目标语言 AI 字幕已存在（再生成 = 覆盖） */
  already_generated: boolean;
  warnings: string[];
}

export interface SubgenQuality {
  event_count: number;
  cps_overrun: number;
  overlong: number;
  kept_original: number;
  glossary_usage: Record<string, number>;
}

export interface SubgenResult {
  ok: boolean;
  message: string;
  filename: string | null;
  sync_score: number | null;
  source_desc: string | null;
  report: SubgenQuality | null;
  finished_at: string | null;
}

export interface SubgenProgress {
  running: boolean;
  phase: string | null;
  message: string | null;
  done_blocks: number;
  total_blocks: number;
  last_result: SubgenResult | null;
}

export interface CalibrateResult {
  ok: boolean;
  message: string;
  scale: number | null;
  offset_ms: number | null;
  score: number | null;
}

/** 生成预检：选源结果 + 成本估算（确认框素材，不动 LLM）。 */
export function subgenPreview(fileId: number, targetLanguage = "chs"): Promise<SubgenPreview> {
  const query = new URLSearchParams({ target_language: targetLanguage });
  return unwrap(
    request<ApiEnvelope<SubgenPreview>>(
      `/library/files/${fileId}/subtitles/generate/preview?${query}`,
    ),
  );
}

/** 发起生成（后台执行；同文件已在跑时后端 400）。 */
export function subgenStart(
  fileId: number,
  targetLanguage = "chs",
): Promise<{ preview: SubgenPreview }> {
  return unwrap(
    request<ApiEnvelope<{ preview: SubgenPreview }>>(
      `/library/files/${fileId}/subtitles/generate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_language: targetLanguage }),
      },
    ),
  );
}

/** 任务状态：进行中带块进度，否则带最近一次结论。 */
export function subgenStatus(fileId: number): Promise<SubgenProgress> {
  return unwrap(
    request<ApiEnvelope<SubgenProgress>>(`/library/files/${fileId}/subtitles/generate/status`),
  );
}

/** 停止进行中的生成（下一块边界生效；已译块保留可续传）。 */
export function subgenStop(fileId: number): Promise<{ stopped: boolean }> {
  return unwrap(
    request<ApiEnvelope<{ stopped: boolean }>>(
      `/library/files/${fileId}/subtitles/generate/stop`,
      { method: "POST" },
    ),
  );
}

/** 一键校准外挂字幕时间轴（零 LLM 成本；校准后覆盖写回）。 */
export function calibrateSubtitle(fileId: number, filename: string): Promise<CalibrateResult> {
  return unwrap(
    request<ApiEnvelope<CalibrateResult>>(`/library/files/${fileId}/subtitles/calibrate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename }),
    }),
  );
}
