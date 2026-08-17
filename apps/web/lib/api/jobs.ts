import { request } from "@/lib/http";

interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

export type JobStatus =
  | "queued"
  | "running"
  | "retry_wait"
  | "cancelling"
  | "waiting"
  | "blocked"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface JobProgress {
  mode: "determinate" | "indeterminate" | "waiting" | "paused";
  phase: string;
  message: string;
  current: number | null;
  total: number | null;
  percent: number | null;
  phase_index: number | null;
  phase_count: number | null;
  details: Record<string, unknown>;
}

export interface JobResource {
  resource_type: string;
  resource_id: string;
  relation: string;
}

export interface JobView {
  id: string;
  job_type: string;
  subject: string | null;
  definition_version: number;
  handler_revision: string;
  prompt_revision: string | null;
  provider_ref: string | null;
  status: JobStatus;
  input_data: Record<string, unknown>;
  progress: JobProgress;
  result: Record<string, unknown> | null;
  error: {
    code?: string;
    message?: string;
    details?: Record<string, unknown>;
    actions?: Array<{ type: string; label: string; target?: string }>;
  } | null;
  usage: Record<string, unknown>;
  resources: JobResource[];
  origin: string;
  actor_kind: string | null;
  actor_name: string | null;
  parent_job_id: string | null;
  root_job_id: string | null;
  retry_of_job_id: string | null;
  attempt: number;
  max_attempts: number;
  revision: number;
  cancel_requested_at: string | null;
  /** 取消发起方；`system:` 前缀代表系统自动收口，重跑没有意义，界面不给「重新执行」。 */
  cancel_requested_by: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** 系统自动收口的取消（如整树入库完成后清理被取代的分批任务）：重跑没有意义，不提供「重新执行」。 */
export function isSystemCancelled(job: JobView): boolean {
  return job.status === "cancelled" && (job.cancel_requested_by?.startsWith("system:") ?? false);
}

export const ACTIVE_JOB_STATUSES = new Set<JobStatus>([
  "queued",
  "running",
  "retry_wait",
  "cancelling",
  "waiting",
  "blocked",
]);

export async function listJobs(options: {
  activeOnly?: boolean;
  limit?: number;
} = {}): Promise<JobView[]> {
  const query = new URLSearchParams({ limit: String(options.limit ?? 100) });
  if (options.activeOnly) query.set("active_only", "true");
  const response = await request<ApiEnvelope<{ items: JobView[] }>>(`/jobs?${query}`);
  return response.data.items;
}

export async function cancelJob(jobId: string): Promise<JobView> {
  const response = await request<ApiEnvelope<{ cancelled: boolean; job: JobView }>>(
    `/jobs/${jobId}/cancel`,
    { method: "POST" },
  );
  return response.data.job;
}

export async function retryJob(jobId: string): Promise<JobView> {
  const response = await request<ApiEnvelope<{ created: boolean; job: JobView }>>(
    `/jobs/${jobId}/retry`,
    { method: "POST" },
  );
  return response.data.job;
}
