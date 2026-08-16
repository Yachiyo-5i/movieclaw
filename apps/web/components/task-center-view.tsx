"use client";

import { useMemo, useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import {
  JOB_STATUS_LABELS,
  JOB_TYPE_LABELS,
  JobCard,
  TaskActionsMenu,
  TaskStatusDot,
} from "@/components/job-center";
import { useToast } from "@/components/feedback";
import {
  ChevronRightIcon,
  CheckIcon,
  ClockIcon,
  FilmIcon,
  InfoIcon,
  TvIcon,
  XIcon,
} from "@/components/icons";
import { Modal } from "@/components/modal";
import { OverflowText } from "@/components/overflow-text";
import { PosterImage } from "@/components/poster-image";
import {
  deleteDownloadTask,
  replaceDownloadTask,
  type DownloadTask,
  type DownloadTaskSource,
} from "@/lib/api/downloaders";
import { useDownloadTasks } from "@/lib/download-tasks";
import { formatBytes, formatDuration } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import { summarizeEpisodeUnits } from "@/lib/episode-units";
import {
  buildIngestHistoryDetail,
  type IngestHistoryDetail,
} from "@/lib/ingest-history";
import { shouldOfferInlineReplacement } from "@/lib/download-task-actions";
import { cancelJob, retryJob, type JobStatus, type JobView } from "@/lib/api/jobs";
import { useJobs } from "@/lib/jobs";
import type { TaskCenterViewName } from "@/lib/task-center";
import {
  formatClockTime,
  formatRelativeTime,
  formatTimelineDayLabel,
  timelineDayKey,
} from "@/lib/time";

const ATTENTION_JOB_STATUSES = new Set<JobStatus>(["blocked", "failed"]);
const ACTIVE_FEED_JOB_STATUSES = new Set<JobStatus>([
  "queued",
  "running",
  "retry_wait",
  "cancelling",
  "waiting",
]);
const HISTORY_JOB_STATUSES = new Set<JobStatus>(["succeeded", "cancelled"]);

const VIEW_LABELS: { id: TaskCenterViewName; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "attention", label: "需要处理" },
  { id: "active", label: "进行中" },
  { id: "history", label: "已结束" },
];

/**
 * 完整任务中心：统一“观察入口”，不制造统一状态表。
 * Job 状态来自 MovieClaw 数据库，下载状态来自下载器实时快照，订阅关系仅按
 * infohash 投影视图；各自的取消、重试和入库生命周期仍由原领域负责。
 */
export function TaskCenterView({
  initialView = "all",
}: {
  initialView?: TaskCenterViewName;
}) {
  const [view, setView] = useState<TaskCenterViewName>(initialView);
  const [deletingTaskId, setDeletingTaskId] = useState<string | null>(null);
  const [replacingTaskId, setReplacingTaskId] = useState<string | null>(null);
  const [cancellingJobId, setCancellingJobId] = useState<string | null>(null);
  const [retryingJobId, setRetryingJobId] = useState<string | null>(null);
  const [pendingDeleteTask, setPendingDeleteTask] = useState<DownloadTask | null>(null);
  const toast = useToast();
  const { jobs, upsert } = useJobs();
  const {
    tasks: downloadTasks,
    sources,
    loading,
    error,
    refreshedAt,
    refresh,
  } = useDownloadTasks();

  async function removeDownloadTask(task: DownloadTask, deleteFiles: boolean) {
    if (task.downloader_id == null || deletingTaskId != null) return;

    setDeletingTaskId(task.id);
    try {
      const result = await deleteDownloadTask(task.downloader_id, task.info_hash, deleteFiles);
      toast.success(
        result.delete_files ? "种子任务和数据文件已删除" : "种子任务已删除，数据文件已保留",
      );
      setPendingDeleteTask(null);
      refresh();
    } catch (caught) {
      toast.error((caught as Error).message || "删除种子任务失败");
    } finally {
      setDeletingTaskId(null);
    }
  }

  async function replaceStalledTask(task: DownloadTask) {
    if (task.downloader_id == null || replacingTaskId != null) return;
    setReplacingTaskId(task.id);
    try {
      await replaceDownloadTask(task.downloader_id, task.info_hash);
      toast.success("已开始寻找同品质替代源；旧任务会保留到新源产生真实进度");
      refresh();
    } catch (caught) {
      toast.error((caught as Error).message || "立即换种失败");
    } finally {
      setReplacingTaskId(null);
    }
  }

  async function cancelBackgroundJob(job: JobView) {
    if (cancellingJobId != null) return;
    setCancellingJobId(job.id);
    try {
      upsert(await cancelJob(job.id));
      toast.success("已提交取消请求");
    } catch (caught) {
      toast.error((caught as Error).message || "取消任务失败");
    } finally {
      setCancellingJobId(null);
    }
  }

  async function retryHistoricalJob(job: JobView) {
    if (retryingJobId != null) return;
    setRetryingJobId(job.id);
    try {
      upsert(await retryJob(job.id));
      toast.success("任务已重新加入队列");
    } catch (caught) {
      toast.error((caught as Error).message || "重新执行失败");
    } finally {
      setRetryingJobId(null);
    }
  }

  // 页面按“是否需要用户行动”组织，而不是按底层执行器分类。每个分区内部
  // 仍沿用 Provider 的更新时间倒序，保留实时刷新时最重要的任务在前。
  const attentionJobs = useMemo(
    () => jobs.filter((job) => ATTENTION_JOB_STATUSES.has(job.status)),
    [jobs],
  );
  const activeFeedJobs = useMemo(
    () => jobs.filter((job) => ACTIVE_FEED_JOB_STATUSES.has(job.status)),
    [jobs],
  );
  const historicalJobs = useMemo(
    () => jobs.filter((job) => HISTORY_JOB_STATUSES.has(job.status)),
    [jobs],
  );
  const ingestJobsByHash = useMemo(() => {
    const linked = new Map<string, JobView>();
    for (const job of jobs) {
      if (job.job_type !== "library.ingest") continue;
      for (const resource of job.resources) {
        if (resource.resource_type === "download" && !linked.has(resource.resource_id)) {
          linked.set(resource.resource_id.toLowerCase(), job);
        }
      }
    }
    return linked;
  }, [jobs]);
  const downloadGroups = useMemo(() => groupDownloadTasks(downloadTasks), [downloadTasks]);
  const attentionDownloadGroups = useMemo(
    () => downloadGroups.filter((group) => downloadGroupNeedsAttention(group, ingestJobsByHash)),
    [downloadGroups, ingestJobsByHash],
  );
  const activeDownloadGroups = useMemo(
    () => downloadGroups.filter((group) => !downloadGroupNeedsAttention(group, ingestJobsByHash)),
    [downloadGroups, ingestJobsByHash],
  );
  const linkedIngestJobIds = useMemo(
    () =>
      new Set(
        downloadTasks
          .map((task) => ingestJobsByHash.get(task.info_hash.toLowerCase())?.id)
          .filter((jobId): jobId is string => jobId != null),
      ),
    [downloadTasks, ingestJobsByHash],
  );
  // 已经串进下载生命周期的 ingest Job 不再作为第二张独立卡重复出现。
  const standaloneAttentionJobs = useMemo(
    () => attentionJobs.filter((job) => !linkedIngestJobIds.has(job.id)),
    [attentionJobs, linkedIngestJobIds],
  );
  const standaloneActiveJobs = useMemo(
    () => activeFeedJobs.filter((job) => !linkedIngestJobIds.has(job.id)),
    [activeFeedJobs, linkedIngestJobIds],
  );
  const standaloneHistoricalJobs = useMemo(
    () => historicalJobs.filter((job) => !linkedIngestJobIds.has(job.id)),
    [historicalJobs, linkedIngestJobIds],
  );
  const attentionTotal = attentionDownloadGroups.length + standaloneAttentionJobs.length;
  const activeTotal = activeDownloadGroups.length + standaloneActiveJobs.length;
  const showAttention = view === "all" || view === "attention";
  const showActive = view === "all" || view === "active";
  const showHistory = view === "all" || view === "history";
  const hasContentBeforeHistory =
    (showAttention && attentionTotal > 0) || (showActive && activeTotal > 0);
  const visibleCount =
    (showAttention ? attentionTotal : 0) +
    (showActive ? activeTotal : 0) +
    (showHistory ? standaloneHistoricalJobs.length : 0);
  const failedSources = sources.filter((source) => source.status !== "active");
  const healthySourceCount = sources.length - failedSources.length;
  const viewCounts: Partial<Record<TaskCenterViewName, number>> = {
    attention: attentionTotal,
    active: activeTotal,
    history: standaloneHistoricalJobs.length,
  };

  return (
    <div className="scroll-thin scroll-safe h-full overflow-y-auto pb-10">
      <div className="mx-auto w-full max-w-[1180px] px-6 pt-7 max-md:px-4 max-md:pt-4">
        <header>
          <div>
            <div className="flex items-center gap-2.5">
              <ClockIcon className="size-6 text-[var(--info)]" />
              <h1 className="text-on-image text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
                任务中心
              </h1>
            </div>
            <p className="text-on-image mt-1.5 max-w-2xl text-ui leading-6 text-[var(--text-muted)]">
              观察下载、入库和后台作业的完整过程，需要处理的任务会优先出现。
            </p>
          </div>
        </header>

        <div className="mt-4 flex min-h-10 flex-wrap items-center gap-x-3 gap-y-2 border-y border-white/[0.06] py-2.5 text-caption text-[var(--text-muted)]">
          <span
            className={`flex items-center gap-2 ${
              failedSources.length > 0 || error ? "text-amber-200/80" : "text-white/50"
            }`}
          >
            <span
              className={`size-1.5 rounded-full ${
                failedSources.length > 0 || error
                  ? "bg-[var(--warn)]"
                  : loading && refreshedAt == null
                    ? "animate-pulse bg-[var(--info)]"
                    : "bg-[var(--ok)]"
              }`}
            />
            {failedSources.length > 0 || error
              ? "自动更新部分异常"
              : loading && refreshedAt == null
                ? "正在同步任务"
                : "自动更新正常"}
          </span>
          {sources.length > 0 && (
            <>
              <span aria-hidden="true" className="h-3 w-px bg-white/10" />
              <span className={failedSources.length > 0 ? "text-amber-200/80" : "text-white/45"}>
                下载器 {healthySourceCount}/{sources.length} 可用
              </span>
            </>
          )}
        </div>

        {(failedSources.length > 0 || error) && (
          <SourceWarning sources={failedSources} error={error} />
        )}

        <div className="mt-6 flex items-center gap-3 border-b border-white/[0.08] max-md:mt-5">
          <div className="scroll-thin flex min-w-0 flex-1 gap-1 overflow-x-auto pb-2">
            {VIEW_LABELS.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-pressed={view === item.id}
                onClick={() => setView(item.id)}
                className={`shrink-0 rounded-full px-3.5 py-1.5 text-ui font-medium transition ${
                  view === item.id
                    ? "bg-white/[0.14] text-white"
                    : "text-[var(--text-muted)] hover:bg-white/[0.06] hover:text-white"
                }`}
              >
                {item.label}
                {item.id !== "all" && (viewCounts[item.id] ?? 0) > 0 && (
                  <span className="tnum ml-1.5 text-caption text-white/45">
                    {viewCounts[item.id]}
                  </span>
                )}
              </button>
            ))}
          </div>
          <p className="shrink-0 pb-2 text-caption text-[var(--text-faint)] max-md:hidden">
            {refreshedAt
              ? `${formatRelativeTime(new Date(refreshedAt).toISOString())}更新`
              : loading
                ? "正在读取下载器"
                : "等待刷新"}
          </p>
        </div>

        {showAttention && attentionTotal > 0 && (
          <TaskAttentionSection count={attentionTotal}>
            {attentionDownloadGroups.map((group) => (
              <DownloadTaskGroupCard
                key={group.key}
                group={group}
                ingestJobsByHash={ingestJobsByHash}
                deletingTaskId={deletingTaskId}
                replacingTaskId={replacingTaskId}
                onDelete={setPendingDeleteTask}
                onReplace={(task) => void replaceStalledTask(task)}
              />
            ))}
            {standaloneAttentionJobs.map((job) => (
              <JobCard key={job.id} job={job} onNavigate={() => undefined} />
            ))}
          </TaskAttentionSection>
        )}

        {showActive && activeTotal > 0 && (
          <TaskTimelineSection title="现在" count={activeTotal}>
            {activeDownloadGroups.map((group) => (
              <TaskTimelineItem
                key={group.key}
                label={`${group.title}的实时状态`}
                tone={downloadGroupTimelineTone(group, ingestJobsByHash)}
              >
                <DownloadTaskGroupFeed
                  group={group}
                  ingestJobsByHash={ingestJobsByHash}
                  deletingTaskId={deletingTaskId}
                  replacingTaskId={replacingTaskId}
                  onDelete={setPendingDeleteTask}
                  onReplace={(task) => void replaceStalledTask(task)}
                />
              </TaskTimelineItem>
            ))}
            {standaloneActiveJobs.map((job) => (
              <TaskTimelineItem
                key={job.id}
                time={formatClockTime(job.created_at)}
                label={`${job.subject || job.job_type}的发起时间`}
                tone={jobTimelineTone(job)}
              >
                <ActiveJobFeedItem
                  job={job}
                  cancelling={cancellingJobId === job.id}
                  onCancel={() => void cancelBackgroundJob(job)}
                />
              </TaskTimelineItem>
            ))}
          </TaskTimelineSection>
        )}

        {showHistory && standaloneHistoricalJobs.length > 0 && (
          <TaskHistorySection
            jobs={standaloneHistoricalJobs}
            initiallyOpen={view === "history"}
            separated={hasContentBeforeHistory}
            retryingJobId={retryingJobId}
            onRetry={(job) => void retryHistoricalJob(job)}
          />
        )}

        {visibleCount === 0 && !loading && <EmptyView view={view} />}

        {loading && visibleCount === 0 && (
          <div className="flex items-center justify-center gap-2.5 py-20 text-ui text-[var(--text-muted)]">
            <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
            正在汇总任务…
          </div>
        )}

      </div>
      {pendingDeleteTask && (
        <DeleteDownloadTaskDialog
          task={pendingDeleteTask}
          busy={deletingTaskId === pendingDeleteTask.id}
          onClose={() => {
            if (deletingTaskId == null) setPendingDeleteTask(null);
          }}
          onConfirm={(deleteFiles) => void removeDownloadTask(pendingDeleteTask, deleteFiles)}
        />
      )}
    </div>
  );
}

/** 删除任务的二次确认：安全默认只移除任务，是否删除数据文件由用户显式选择。 */
function DeleteDownloadTaskDialog({
  task,
  busy,
  onClose,
  onConfirm,
}: {
  task: DownloadTask;
  busy: boolean;
  onClose: () => void;
  onConfirm: (deleteFiles: boolean) => void;
}) {
  const [deleteFiles, setDeleteFiles] = useState(false);
  const downloaderName = task.downloader_name || `下载器 ${task.downloader_id}`;

  return (
    <Modal open onClose={busy ? () => {} : onClose} label="删除种子任务" topmost>
      <div className="p-6 max-md:p-5">
        <h2 className="text-title-sm font-bold text-white">删除种子任务？</h2>
        <p className="mt-2 text-sub leading-6 text-[var(--text-muted)]">
          将从「{downloaderName}」停止并移除该任务。
        </p>
        <p className="mt-3 break-words rounded-xl border border-white/[0.08] bg-white/[0.035] px-3.5 py-3 text-sub leading-6 text-white/75">
          {task.name || task.info_hash}
        </p>
        <label
          className={`mt-4 flex cursor-pointer items-start gap-3 rounded-xl border px-3.5 py-3 transition ${
            deleteFiles
              ? "border-[#ff6b6b]/35 bg-[#ff6b6b]/[0.08]"
              : "border-white/[0.08] bg-white/[0.025] hover:bg-white/[0.045]"
          }`}
        >
          <input
            type="checkbox"
            checked={deleteFiles}
            onChange={(event) => setDeleteFiles(event.target.checked)}
            className="mt-1 size-4 shrink-0 accent-[#ff6b6b]"
          />
          <span className="min-w-0">
            <span className="block text-ui font-semibold text-white/90">同时删除数据文件</span>
            <span className="mt-0.5 block text-caption leading-5 text-white/50">
              包括已下载和未完成的数据；若文件仍由下载器管理，即使已经入库也可能被删除，且无法恢复。
            </span>
          </span>
        </label>
        {task.source === "subscription" && (
          <p className="mt-3 text-caption leading-5 text-amber-100/65">
            关联订阅会保留原工单身份；巡检确认任务无进度后会按换源策略寻找替代源。
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2.5">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="rounded-lg border border-white/10 bg-white/[0.06] px-4 py-2 text-ui text-white/80 transition hover:bg-white/[0.1] disabled:opacity-40"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => onConfirm(deleteFiles)}
            disabled={busy}
            className="rounded-lg bg-red-500/85 px-4 py-2 text-ui font-medium text-white transition hover:bg-red-500 disabled:opacity-40"
          >
            {busy ? "正在删除…" : deleteFiles ? "删除任务和文件" : "仅删除任务"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function SourceWarning({ sources, error }: { sources: DownloadTaskSource[]; error: string | null }) {
  const message = error
    ? error
    : sources
        .map((source) => `「${source.name}」${source.message || "当前不可用"}`)
        .join("；");
  return (
    <div className="mt-4 flex items-start gap-3 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub leading-6 text-amber-100">
      <InfoIcon className="mt-0.5 size-4 shrink-0" />
      <p className="min-w-0 flex-1">{message}</p>
      <Link
        href={"/settings/downloaders" as Route}
        className="shrink-0 font-semibold text-amber-50 hover:underline"
      >
        检查设置
      </Link>
    </div>
  );
}

/** 需要用户行动的任务脱离普通时间流置顶，避免失败记录被持续刷新的进度淹没。 */
function TaskAttentionSection({
  count,
  children,
}: {
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-6" aria-labelledby="attention-tasks-title">
      <div className="mb-3 flex items-center gap-2.5">
        <span className="size-2 rounded-full bg-[var(--danger)] ring-4 ring-[var(--danger)]/10" />
        <h2 id="attention-tasks-title" className="text-ui font-semibold text-[var(--danger)]">
          需要你处理
        </h2>
        <span className="tnum rounded-full bg-[var(--danger)]/10 px-2 py-0.5 text-caption text-[var(--danger)]">
          {count}
        </span>
      </div>
      <div className="space-y-2.5 rounded-2xl border border-[var(--danger)]/20 bg-[var(--danger)]/[0.035] p-3.5 max-md:p-3">
        {children}
      </div>
    </section>
  );
}

/**
 * 统一时间线只负责排列视图，不合并领域状态。下载卡仍读取下载器快照，JobCard
 * 仍读取 Job；时间轴仅把它们放入同一条用户可扫描的阅读路径。
 */
function TaskTimelineSection({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-7" aria-labelledby="active-tasks-title">
      <div className="mb-3 flex items-center gap-3">
        <h2 id="active-tasks-title" className="text-ui font-semibold text-white/65">
          {title}
        </h2>
        <span className="tnum text-caption text-white/30">{count}</span>
        <span aria-hidden="true" className="h-px min-w-8 flex-1 bg-white/[0.09]" />
      </div>
      <div>{children}</div>
    </section>
  );
}

function TaskTimelineItem({
  time,
  label,
  tone = "active",
  children,
}: {
  time?: string;
  label: string;
  tone?: "active" | "waiting" | "success" | "cancelled";
  children: React.ReactNode;
}) {
  const completed = tone === "success" || tone === "cancelled";
  const dotClass =
    tone === "waiting"
      ? "bg-[var(--warn)] ring-[var(--warn)]/25"
      : tone === "success"
        ? "bg-[var(--ok)] text-[#07120b] ring-[var(--ok)]/20"
        : tone === "cancelled"
          ? "bg-white/20 text-white/60 ring-white/10"
          : "bg-[var(--info)] ring-[var(--info)]/30";
  return (
    <div className="grid grid-cols-[3.75rem_1.5rem_minmax(0,1fr)] gap-x-2 last:[&_[data-timeline-line]]:hidden max-md:grid-cols-[1.25rem_minmax(0,1fr)] max-md:gap-x-2.5">
      <span
        aria-label={time ? label : undefined}
        aria-hidden={time ? undefined : true}
        className="tnum pt-2.5 text-right text-caption text-white/35 max-md:hidden"
      >
        {time}
      </span>
      <span className="relative flex self-stretch justify-center" aria-hidden="true">
        <span
          className={`absolute top-[0.7rem] z-10 flex items-center justify-center rounded-full border-2 border-[#0b0f17] ring-2 ${
            completed ? "size-4" : "mt-0.5 size-2.5"
          } ${dotClass}`}
        >
          {tone === "success" && <CheckIcon className="size-2.5" />}
          {tone === "cancelled" && <XIcon className="size-2.5" />}
        </span>
        <span
          data-timeline-line
          className="absolute bottom-0 top-[1.65rem] w-px bg-white/[0.12]"
        />
      </span>
      <div className="min-w-0 pb-4">
        {time && (
          <p className="tnum mb-1 hidden text-caption text-white/35 max-md:block">{time}</p>
        )}
        {children}
      </div>
    </div>
  );
}

function jobTimelineTone(job: JobView): "active" | "waiting" {
  return job.status === "running" ? "active" : "waiting";
}

const ACTIVE_JOB_ACTIONS: Record<string, string> = {
  "subtitle.generate": "正在生成字幕",
  "library.scan": "正在扫描",
  "library.metadata.refresh": "正在刷新媒体库元数据",
  "media.metadata.refresh": "正在刷新元数据",
  "library.organize": "正在整理文件",
  "library.transfer": "正在转移文件",
  "library.ingest": "正在入库",
};

const COMPLETED_JOB_ACTIONS: Record<string, string> = {
  "subtitle.generate": "字幕生成",
  "library.scan": "扫描",
  "library.metadata.refresh": "元数据刷新",
  "media.metadata.refresh": "元数据刷新",
  "library.organize": "文件整理",
  "library.transfer": "文件转移",
  "library.ingest": "入库",
};

function jobDetailString(job: JobView, key: string): string | null {
  const detail = job.progress.details[key] ?? job.input_data[key];
  return typeof detail === "string" && detail ? detail : null;
}

function jobDetailNumber(job: JobView, key: string): number | null {
  const detail = job.progress.details[key];
  return typeof detail === "number" && Number.isFinite(detail) ? detail : null;
}

/** Feed 主标题优先使用执行结果确认过的作品名，避免把长 release 名当业务标题。 */
function jobFeedIdentity(job: JobView): string | null {
  const quotedTitle = job.progress.message.match(/《([^》]+)》/)?.[1] ?? null;
  const title =
    quotedTitle ||
    jobDetailString(job, "media_title") ||
    jobDetailString(job, "title") ||
    job.subject;
  if (!title) return null;
  if (/^[《「].+[》」]$/.test(title)) return title;
  if (job.job_type === "library.scan") return `「${title}」`;
  if (
    job.job_type === "library.ingest" ||
    job.job_type === "subtitle.generate" ||
    job.job_type.includes("metadata.refresh")
  ) {
    return `《${title}》`;
  }
  return title;
}

function jobProgressAmount(job: JobView): string | null {
  const { current, total } = job.progress;
  if (current == null || total == null) return null;
  if (job.job_type === "library.ingest") {
    return `已处理 ${formatBytes(current)} / ${formatBytes(total)}`;
  }
  const unit =
    job.job_type === "subtitle.generate"
      ? "个分块"
      : job.job_type === "library.scan"
        ? "个文件"
        : job.job_type.includes("metadata.refresh")
          ? "个条目"
          : "项";
  return `已处理 ${current} / ${total} ${unit}`;
}

function jobOriginLabel(job: JobView): string {
  const origin =
    job.origin === "cli"
      ? "CLI 发起"
      : job.origin === "agent"
        ? "Agent 发起"
        : job.origin === "scheduler" || job.origin === "system"
          ? "系统自动"
          : "网页发起";
  return job.actor_name ? `${origin} · ${job.actor_name}` : origin;
}

function activeJobStatus(job: JobView): string {
  if (job.status === "running") {
    return ACTIVE_JOB_ACTIONS[job.job_type] || "正在处理";
  }
  return JOB_STATUS_LABELS[job.status] || job.status;
}

function historicalJobTitle(job: JobView): string {
  const identity = jobFeedIdentity(job);
  const action = COMPLETED_JOB_ACTIONS[job.job_type];
  const result = job.status === "cancelled" ? "已取消" : "完成";
  if (identity && action) return `${identity}${action}${result}`;
  return `${identity || JOB_TYPE_LABELS[job.job_type] || job.job_type}${result}`;
}

function historicalJobSummary(job: JobView): string {
  if (job.job_type === "library.scan") {
    const identified = jobDetailNumber(job, "identified");
    const unidentified = jobDetailNumber(job, "unidentified");
    const parts = [
      identified != null ? `识别 ${identified} 个` : null,
      unidentified != null ? `待识别 ${unidentified} 个` : null,
    ].filter((item): item is string => item != null);
    if (parts.length > 0) return parts.join(" · ");
  }
  return job.progress.message || (job.status === "cancelled" ? "任务已取消" : "任务已完成");
}

function ActiveJobFeedItem({
  job,
  cancelling,
  onCancel,
}: {
  job: JobView;
  cancelling: boolean;
  onCancel: () => void;
}) {
  const percent = job.progress.percent;
  const amount = jobProgressAmount(job);
  const cancellable = job.status !== "cancelling";
  const title = jobFeedIdentity(job) || JOB_TYPE_LABELS[job.job_type] || job.job_type;
  return (
    <article className="min-w-0 py-1.5">
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-ui font-semibold leading-5 text-white/90">
            <OverflowText>{title}</OverflowText>
          </h3>
          <p className="mt-1 flex flex-wrap items-center gap-x-1.5 text-sub text-white/60">
            <span className="font-medium text-white/70">{activeJobStatus(job)}</span>
            {percent != null && (
              <>
                <span aria-hidden="true" className="text-white/25">·</span>
                <span className="tnum">{Math.round(percent)}%</span>
              </>
            )}
          </p>
        </div>
        {cancellable && (
          <button
            type="button"
            onClick={onCancel}
            disabled={cancelling}
            className="shrink-0 rounded-lg px-2.5 py-1.5 text-caption font-medium text-white/45 transition hover:bg-white/[0.06] hover:text-white/75 disabled:cursor-wait disabled:opacity-45"
          >
            {cancelling ? "正在取消…" : "取消任务"}
          </button>
        )}
      </div>
      {job.progress.message && (
        <OverflowText lines={2} className="mt-1.5 text-caption leading-5 text-white/42">
          {job.progress.message}
        </OverflowText>
      )}
      {(percent != null || job.status === "running") && (
        <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-white/[0.07]">
          <div
            className={`h-full rounded-full bg-[var(--info)] transition-[width] duration-700 ${
              percent == null ? "w-1/3 animate-pulse opacity-65" : ""
            }`}
            style={
              percent == null
                ? undefined
                : { width: `${Math.min(100, Math.max(percent > 0 ? 1 : 0, percent))}%` }
            }
          />
        </div>
      )}
      {amount && <p className="tnum mt-1.5 text-caption text-white/38">{amount}</p>}
      <p className="mt-2 text-caption text-white/30">
        {jobOriginLabel(job)}
        <span aria-hidden="true"> · </span>
        {job.started_at ? `${formatClockTime(job.started_at)} 开始` : `${formatClockTime(job.created_at)} 发起`}
      </p>
    </article>
  );
}

interface HistoryDayGroup {
  key: string;
  label: string;
  jobs: JobView[];
}

function groupHistoricalJobs(jobs: JobView[]): HistoryDayGroup[] {
  const groups = new Map<string, HistoryDayGroup>();
  for (const job of jobs) {
    const timestamp = job.finished_at || job.created_at;
    const key = timelineDayKey(timestamp);
    const existing = groups.get(key);
    if (existing) {
      existing.jobs.push(job);
    } else {
      groups.set(key, { key, label: formatTimelineDayLabel(timestamp), jobs: [job] });
    }
  }
  return [...groups.values()];
}

function HistoricalJobFeedItem({
  job,
  retrying,
  onRetry,
}: {
  job: JobView;
  retrying: boolean;
  onRetry: () => void;
}) {
  const ingestDetail = buildIngestHistoryDetail(job);
  return (
    <article className="group/history min-w-0 py-1.5">
      <div className="flex min-w-0 items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-ui font-medium leading-5 text-white/78">
            <OverflowText>{historicalJobTitle(job)}</OverflowText>
          </h3>
          <OverflowText lines={2} className="mt-1 text-caption leading-5 text-white/40">
            {ingestDetail?.summary ?? historicalJobSummary(job)}
          </OverflowText>
          {ingestDetail && <IngestHistoryFiles detail={ingestDetail} />}
        </div>
        {job.status === "cancelled" && (
          <button
            type="button"
            onClick={onRetry}
            disabled={retrying}
            className="shrink-0 rounded-lg px-2.5 py-1.5 text-caption font-medium text-white/35 transition hover:bg-white/[0.05] hover:text-white/65 disabled:cursor-wait disabled:opacity-40"
          >
            {retrying ? "重新执行中…" : "重新执行"}
          </button>
        )}
      </div>
    </article>
  );
}

/** 入库历史默认只占一行，需要核对时再展开真实文件名与目标媒体库。 */
function IngestHistoryFiles({ detail }: { detail: IngestHistoryDetail }) {
  if (detail.fileGroups.length === 0 && detail.context == null) return null;
  const fileCount = detail.fileGroups.reduce((total, group) => total + group.files.length, 0);
  return (
    <details className="group/files mt-1.5 max-w-full text-caption">
      <summary className="flex w-fit max-w-full cursor-pointer list-none items-center gap-1.5 rounded-md py-1 pr-1 text-white/32 transition hover:text-white/60 [&::-webkit-details-marker]:hidden">
        <ChevronRightIcon className="size-3.5 shrink-0 transition-transform group-open/files:rotate-90" />
        <span>查看文件明细</span>
        {fileCount > 0 && <span className="tnum text-white/22">{fileCount} 个</span>}
      </summary>
      <div className="mt-1.5 hidden rounded-lg border border-white/[0.06] bg-white/[0.025] px-3 py-2.5 group-open/files:block">
        {detail.context && <p className="mb-2 text-white/38">{detail.context}</p>}
        <div className="space-y-2.5">
          {detail.fileGroups.map((group) => (
            <section key={group.label} aria-label={group.label}>
              <p className="mb-1 text-micro font-medium text-white/35">
                {group.label} · {group.files.length} 个
              </p>
              <ul className="space-y-1 text-white/52">
                {group.files.map((file) => (
                  <li key={file} title={file} className="break-all leading-5">
                    {file}
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </div>
    </details>
  );
}

/** 历史按本地日期拆成轻量 Feed，首组默认展开，避免完成卡片占满纵向空间。 */
function TaskHistorySection({
  jobs,
  initiallyOpen,
  separated,
  retryingJobId,
  onRetry,
}: {
  jobs: JobView[];
  initiallyOpen: boolean;
  separated: boolean;
  retryingJobId: string | null;
  onRetry: (job: JobView) => void;
}) {
  const groups = groupHistoricalJobs(jobs);
  return (
    <section
      className={separated ? "mt-6 border-t border-white/[0.1]" : "mt-3"}
      aria-label="历史任务"
    >
      {groups.map((group, index) => (
        <details
          key={group.key}
          open={initiallyOpen || index === 0 || undefined}
          className="group border-b border-white/[0.07] py-3.5 last:border-b-0"
        >
          <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg py-1 text-ui text-white/55 transition hover:text-white/80 [&::-webkit-details-marker]:hidden">
            <span className="font-semibold">{group.label}</span>
            <span className="tnum text-caption text-white/30">{group.jobs.length} 项</span>
            <span className="ml-auto text-caption text-white/30 group-open:hidden">展开</span>
            <span className="ml-auto hidden text-caption text-white/30 group-open:inline">收起</span>
            <ChevronRightIcon className="size-4 transition-transform group-open:rotate-90" />
          </summary>
          <div className="mt-3">
            {group.jobs.map((job) => (
              <TaskTimelineItem
                key={job.id}
                time={formatClockTime(job.finished_at || job.created_at)}
                label={`${historicalJobTitle(job)}的完成时间`}
                tone={job.status === "succeeded" ? "success" : "cancelled"}
              >
                <HistoricalJobFeedItem
                  job={job}
                  retrying={retryingJobId === job.id}
                  onRetry={() => onRetry(job)}
                />
              </TaskTimelineItem>
            ))}
          </div>
        </details>
      ))}
    </section>
  );
}

/**
 * 一个任务状态的展示三件套。``color`` 走内联样式（进度条与状态文字共用），
 * ``dot`` 是状态点的 class。
 *
 * 取色只从 globals.css 的状态四档里挑（--ok / --info / --warn / --danger），
 * 「没有 / 不会发生」不占状态色，用本表既有的中性灰。判断口径见 globals.css：
 * 颜色说的是「系统状态」还是「你该做什么」，只有后者用 --danger。
 *
 * 同一档里的多个状态长得一样是刻意的——「下载中」和「校验中」都是正在进行，
 * 区别由 label 承担；用色相去暗示它们是两类不同的东西，只会稀释颜色的信号。
 */
interface TaskStateMeta {
  label: string;
  color: string;
  dot: string;
}

const DOWNLOAD_STATE_META: Record<DownloadTask["state"], TaskStateMeta> = {
  downloading: {
    label: "下载中",
    color: "var(--info)",
    dot: "animate-pulse bg-[var(--info)]",
  },
  stalled: {
    label: "等待连接",
    color: "var(--warn)",
    dot: "bg-[var(--warn)]",
  },
  paused: {
    label: "已暂停",
    color: "var(--warn)",
    dot: "bg-[var(--warn)]",
  },
  queued: {
    label: "排队中",
    color: "#cbd5e1",
    dot: "bg-white/40",
  },
  checking: {
    label: "校验中",
    color: "var(--info)",
    dot: "animate-pulse bg-[var(--info)]",
  },
  completed: {
    label: "等待入库",
    color: "var(--ok)",
    dot: "bg-[var(--ok)]",
  },
  error: {
    label: "下载异常",
    color: "var(--danger)",
    dot: "bg-[var(--danger)]",
  },
  missing: {
    label: "任务缺失",
    color: "var(--danger)",
    dot: "bg-[var(--danger)]",
  },
  unknown: {
    label: "状态未知",
    color: "#cbd5e1",
    dot: "bg-white/40",
  },
};

const INGEST_STATE_META: Record<JobStatus, TaskStateMeta> = {
  queued: { label: "等待入库", color: "#cbd5e1", dot: "bg-white/40" },
  running: { label: "正在入库", color: "var(--ok)", dot: "animate-pulse bg-[var(--ok)]" },
  retry_wait: { label: "等待重试", color: "var(--warn)", dot: "bg-[var(--warn)]" },
  cancelling: { label: "正在停止", color: "var(--warn)", dot: "bg-[var(--warn)]" },
  waiting: { label: "等待条件", color: "#cbd5e1", dot: "bg-white/40" },
  blocked: { label: "入库待处理", color: "var(--danger)", dot: "bg-[var(--danger)]" },
  succeeded: { label: "入库完成", color: "var(--ok)", dot: "bg-[var(--ok)]" },
  failed: { label: "入库待处理", color: "var(--danger)", dot: "bg-[var(--danger)]" },
  // 已取消是终态，不会再有进展：归到本表的中性灰，和「等待入库 / 等待条件」
  // 一样不占状态色——它们的区别本来就该由 label 说清楚。
  cancelled: { label: "入库已取消", color: "#cbd5e1", dot: "bg-white/40" },
};

interface DownloadTaskGroup {
  key: string;
  mediaItemId: number | null;
  title: string;
  kind: string | null;
  posterUrl: string | null;
  tasks: DownloadTask[];
}

function groupDownloadTasks(tasks: DownloadTask[]): DownloadTaskGroup[] {
  const groups = new Map<string, DownloadTaskGroup>();
  for (const task of tasks) {
    // 只用数据库里的媒体条目主键合并。同名但未识别的资源必须各自保留，
    // 不能因为标题解析相似就把两个版本或两部同名作品错误折叠。
    const key = task.media_item_id != null ? `media:${task.media_item_id}` : `task:${task.id}`;
    const existing = groups.get(key);
    if (existing) {
      existing.tasks.push(task);
      if (!existing.posterUrl && task.poster_url) existing.posterUrl = task.poster_url;
      continue;
    }
    groups.set(key, {
      key,
      mediaItemId: task.media_item_id,
      title: task.media_title || task.name || task.info_hash,
      kind: task.media_kind,
      posterUrl: task.poster_url,
      tasks: [task],
    });
  }
  return [...groups.values()];
}

function downloadGroupNeedsAttention(
  group: DownloadTaskGroup,
  ingestJobsByHash: Map<string, JobView>,
): boolean {
  return group.tasks.some((task) => {
    const ingestJob = ingestJobsByHash.get(task.info_hash.toLowerCase());
    return (
      task.can_replace ||
      task.state === "error" ||
      task.state === "missing" ||
      (ingestJob != null && ATTENTION_JOB_STATUSES.has(ingestJob.status))
    );
  });
}

function downloadGroupTimelineTone(
  group: DownloadTaskGroup,
  ingestJobsByHash: Map<string, JobView>,
): "active" | "waiting" {
  const executing = group.tasks.some((task) => {
    const ingestJob = ingestJobsByHash.get(task.info_hash.toLowerCase());
    return ingestJob?.status === "running" || ["downloading", "checking"].includes(task.state);
  });
  return executing ? "active" : "waiting";
}

/** “现在”区域使用过程行，不再把下载卡片原样塞进时间轴。 */
function DownloadTaskGroupFeed({
  group,
  ingestJobsByHash,
  deletingTaskId,
  replacingTaskId,
  onDelete,
  onReplace,
}: {
  group: DownloadTaskGroup;
  ingestJobsByHash: Map<string, JobView>;
  deletingTaskId: string | null;
  replacingTaskId: string | null;
  onDelete: (task: DownloadTask) => void;
  onReplace: (task: DownloadTask) => void;
}) {
  const grouped = group.mediaItemId != null;
  const firstSubscription = group.tasks.flatMap((task) => task.subscriptions)[0];
  return (
    <article className="min-w-0 py-1.5">
      {grouped && (
        <div className="mb-2 flex min-w-0 items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h3 className="flex min-w-0 items-center gap-2 text-ui font-semibold leading-5 text-white/90">
              <span className="shrink-0 rounded-full border border-[var(--info)]/25 bg-[var(--info)]/[0.12] px-1.5 py-0.5 text-[11px] font-semibold leading-none text-[var(--info)]">
                实时
              </span>
              <OverflowText className="flex-1">{group.title}</OverflowText>
            </h3>
            <p className="mt-1 text-caption text-white/35">
              {group.kind === "tv" ? "剧集" : "电影"}
              <span aria-hidden="true"> · </span>
              {group.tasks.length} 个下载资源
            </p>
          </div>
          {firstSubscription && (
            <Link
              href={`/subscriptions/${firstSubscription.id}` as Route}
              className="shrink-0 rounded-lg px-2.5 py-1.5 text-caption font-medium text-white/40 transition hover:bg-white/[0.06] hover:text-white/70"
            >
              查看订阅
            </Link>
          )}
        </div>
      )}
      <div
        className={
          grouped
            ? "divide-y divide-white/[0.06] border-l border-white/[0.1] pl-3.5"
            : ""
        }
      >
        {group.tasks.map((task) => (
          <DownloadTaskFeedItem
            key={task.id}
            task={task}
            ingestJob={ingestJobsByHash.get(task.info_hash.toLowerCase()) ?? null}
            grouped={grouped}
            deleting={deletingTaskId === task.id}
            replacing={replacingTaskId === task.id}
            onDelete={onDelete}
            onReplace={onReplace}
          />
        ))}
      </div>
    </article>
  );
}

function DownloadTaskFeedItem({
  task,
  ingestJob,
  grouped,
  deleting,
  replacing,
  onDelete,
  onReplace,
}: {
  task: DownloadTask;
  ingestJob: JobView | null;
  grouped: boolean;
  deleting: boolean;
  replacing: boolean;
  onDelete: (task: DownloadTask) => void;
  onReplace: (task: DownloadTask) => void;
}) {
  const ingestOwnsState = ingestJob != null && ingestJob.status !== "cancelled";
  const meta = ingestOwnsState ? INGEST_STATE_META[ingestJob.status] : DOWNLOAD_STATE_META[task.state];
  const downloadPercent = task.progress == null ? null : Math.floor(task.progress * 100);
  const percent = ingestOwnsState
    ? ingestJob.progress.percent == null
      ? null
      : Math.floor(ingestJob.progress.percent)
    : downloadPercent;
  const title = grouped
    ? task.name || task.media_title || task.info_hash
    : task.media_title || task.name || task.info_hash;
  // 有关联入库 Job 时不再单独出一行文字说明：入库阶段与解释由底部生命
  // 周期承担，覆盖集标签会标注已入库进度，feed 行只保留下载态/换源提示
  const note = ingestJob != null ? null : downloadTaskNote(task, null);
  const showReplace = shouldOfferInlineReplacement(task);
  const firstSubscription = task.subscriptions[0];
  // 种子名前用站点徽标回答"这个资源从哪来"；画面规格与片源紧随其后，
  // 弥补高度同质化的 release 名。状态与进度由底部生命周期承担，不再重复。
  const specs = [task.resolution, task.media_source].filter(
    (item): item is string => item != null,
  );

  return (
    <div className={grouped ? "py-2.5 first:pt-1 last:pb-1" : ""}>
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h3
            className={`flex min-w-0 items-center gap-2 ${
              grouped ? "text-sub" : "text-ui"
            } font-semibold leading-5 text-white/88`}
          >
            {!grouped && (
              <span className="shrink-0 rounded-full border border-[var(--info)]/25 bg-[var(--info)]/[0.12] px-1.5 py-0.5 text-[11px] font-semibold leading-none text-[var(--info)]">
                实时
              </span>
            )}
            {task.site_name && (
              <span className="shrink-0 rounded-full border border-white/[0.14] bg-white/[0.06] px-1.5 py-0.5 text-[11px] font-medium leading-none text-white/55">
                {task.site_name}
              </span>
            )}
            <OverflowText className="flex-1">{title}</OverflowText>
          </h3>
          {specs.length > 0 && (
            <p className="mt-1 flex flex-wrap items-center gap-x-1.5 text-caption text-white/45">
              {specs.map((spec, index) => (
                <span key={spec} className="flex items-center gap-x-1.5">
                  {index > 0 && (
                    <span aria-hidden="true" className="text-white/25">·</span>
                  )}
                  {spec}
                </span>
              ))}
            </p>
          )}
        </div>
        {(task.page_url != null || task.downloader_id != null) && (
          <DownloadTaskActionsMenu
            task={task}
            deleting={deleting}
            replacing={replacing}
            onDelete={onDelete}
          />
        )}
      </div>
      {note && (
        <OverflowText lines={2} className="mt-1.5 text-caption leading-5 text-white/42">
          {note}
        </OverflowText>
      )}
      {percent != null && task.state !== "missing" && (
        <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-white/[0.07]">
          <div
            className="h-full rounded-full transition-[width] duration-700"
            style={{
              width: `${Math.min(100, Math.max(percent > 0 ? 1 : 0, percent))}%`,
              backgroundColor: meta.color,
            }}
          />
        </div>
      )}
      {/* 大小/速度/剩余时间合并成一行；投递来源与下载器名由生命周期"已投递"承担 */}
      {(task.size_bytes != null ||
        (!ingestOwnsState &&
          ((task.state === "downloading" && task.dlspeed_bytes != null && task.dlspeed_bytes > 0) ||
            task.eta_seconds != null))) && (
        <div className="mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-caption text-white/38">
          {task.size_bytes != null && <span className="tnum">{formatBytes(task.size_bytes)}</span>}
          {!ingestOwnsState &&
            task.state === "downloading" &&
            task.dlspeed_bytes != null &&
            task.dlspeed_bytes > 0 && (
              <span className="tnum">↓ {formatBytes(task.dlspeed_bytes)}/s</span>
            )}
          {!ingestOwnsState && task.eta_seconds != null && (
            <span>剩余约 {formatDuration(task.eta_seconds)}</span>
          )}
        </div>
      )}
      {firstSubscription && (
        <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-caption">
          <EpisodeUnitsLabel units={firstSubscription.units} />
        </div>
      )}
      <DownloadLifecycle task={task} ingestJob={ingestJob} variant="feed" />
      {showReplace && (
        <button
          type="button"
          onClick={() => onReplace(task)}
          disabled={replacing}
          className="mt-2 rounded-lg border border-[var(--danger)]/25 bg-[var(--danger)]/[0.07] px-2.5 py-1.5 text-caption font-semibold text-[#fee2e2] transition hover:bg-[var(--danger)]/[0.13] disabled:cursor-wait disabled:opacity-50"
        >
          {replacing ? "正在换种…" : "立即换种"}
        </button>
      )}
    </div>
  );
}

function DownloadTaskGroupCard({
  group,
  ingestJobsByHash,
  deletingTaskId,
  replacingTaskId,
  onDelete,
  onReplace,
}: {
  group: DownloadTaskGroup;
  ingestJobsByHash: Map<string, JobView>;
  deletingTaskId: string | null;
  replacingTaskId: string | null;
  onDelete: (task: DownloadTask) => void;
  onReplace: (task: DownloadTask) => void;
}) {
  if (group.mediaItemId == null) {
    const task = group.tasks[0];
    return (
      <DownloadTaskCard
        task={task}
        ingestJob={ingestJobsByHash.get(task.info_hash.toLowerCase()) ?? null}
        deleting={deletingTaskId === task.id}
        replacing={replacingTaskId === task.id}
        onDelete={onDelete}
        onReplace={onReplace}
      />
    );
  }

  const isTv = group.kind === "tv";
  const firstSubscription = group.tasks.flatMap((task) => task.subscriptions)[0];
  return (
    <article className="overflow-hidden rounded-2xl border border-white/[0.08] bg-[rgba(14,16,22,0.52)] backdrop-blur-xl">
      {/* 影片身份集中在紧凑顶部，种子列表另起整行占满卡片宽度。海报不再
          作为贯穿整组的左栏，长种子名、状态和操作按钮因此有完整横向空间。 */}
      <div className="flex items-center gap-3.5 border-b border-white/[0.07] px-4 py-3 max-md:px-3.5">
        <div className="w-10 shrink-0 max-md:w-9">
          <div className="aspect-[2/3] overflow-hidden rounded-lg bg-[#141824] ring-1 ring-white/10">
            <PosterImage
              src={imageUrl(group.posterUrl)}
              alt={`${group.title}海报`}
              className="size-full"
              fallback={
                <div className="flex size-full items-center justify-center bg-gradient-to-b from-white/[0.07] to-[#11141c] text-white/25">
                  {isTv ? <TvIcon className="size-4" /> : <FilmIcon className="size-4" />}
                </div>
              }
            />
          </div>
        </div>
        <div className="min-w-0 flex-1">
          <h3 className="min-w-0 text-ui font-semibold text-white/90">
            <OverflowText>{group.title}</OverflowText>
          </h3>
          <p className="mt-0.5 text-caption text-white/40">
            {isTv ? "剧集" : "电影"}
            <span aria-hidden="true"> · </span>
            {group.tasks.length} 个下载资源
          </p>
        </div>
        {firstSubscription && (
          <Link
            href={`/subscriptions/${firstSubscription.id}` as Route}
            className="shrink-0 rounded-lg border border-white/10 px-3 py-1.5 text-caption font-medium text-white/60 transition hover:bg-white/[0.06] hover:text-white"
          >
            查看订阅
          </Link>
        )}
      </div>
      <div className="space-y-2 p-3.5 max-md:p-3">
        {group.tasks.map((task) => (
          <DownloadTaskCard
            key={task.id}
            task={task}
            ingestJob={ingestJobsByHash.get(task.info_hash.toLowerCase()) ?? null}
            grouped
            deleting={deletingTaskId === task.id}
            replacing={replacingTaskId === task.id}
            onDelete={onDelete}
            onReplace={onReplace}
          />
        ))}
      </div>
    </article>
  );
}

function DownloadTaskCard({
  task,
  ingestJob,
  grouped = false,
  deleting,
  replacing,
  onDelete,
  onReplace,
}: {
  task: DownloadTask;
  ingestJob: JobView | null;
  grouped?: boolean;
  deleting: boolean;
  replacing: boolean;
  onDelete: (task: DownloadTask) => void;
  onReplace: (task: DownloadTask) => void;
}) {
  const ingestNeedsAttention =
    ingestJob !== null && ["blocked", "failed"].includes(ingestJob.status);
  const ingestOwnsCardState = ingestJob !== null && ingestJob.status !== "cancelled";
  const meta = ingestOwnsCardState
    ? INGEST_STATE_META[ingestJob.status]
    : DOWNLOAD_STATE_META[task.state];
  const title = grouped
    ? task.name || task.media_title || task.info_hash
    : task.media_title || task.name || task.info_hash;
  const torrentName = !grouped && task.name && task.name !== title ? task.name : null;
  const sourceLabel =
    task.source === "subscription"
      ? "订阅投递"
      : task.source === "manual"
        ? "手动下载"
        : "下载器任务";
  const percent = task.progress == null ? null : Math.floor(task.progress * 100);
  const displayPercent = ingestOwnsCardState
    ? ingestJob?.progress.percent == null
      ? null
      : Math.floor(ingestJob.progress.percent)
    : percent;
  const progressLabel = ingestOwnsCardState ? "入库进度" : "下载进度";
  const firstSubscription = task.subscriptions[0];
  const showInlineReplace = shouldOfferInlineReplacement(task);
  const needsAttention =
    ingestNeedsAttention || task.state === "error" || task.state === "missing" || task.can_replace;
  const taskNote = downloadTaskNote(task, ingestJob);
  const showDownloadRuntime = !ingestOwnsCardState && task.state === "downloading";

  return (
    <article
      className={
        grouped
          ? "rounded-xl border border-white/[0.06] bg-black/15 p-3"
          : "rounded-2xl border border-white/[0.08] bg-[rgba(14,16,22,0.5)] p-4 backdrop-blur-xl max-md:p-3.5"
      }
    >
      <div className="min-w-0">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 flex-1 items-center gap-2.5">
            <TaskStatusDot label={meta.label} dotClass={meta.dot} />
            <h3 className="min-w-0 text-ui font-semibold leading-5 text-white/90">
              <OverflowText>{title}</OverflowText>
            </h3>
          </div>
          {(task.page_url != null || task.downloader_id != null) && (
            <DownloadTaskActionsMenu
              task={task}
              deleting={deleting}
              replacing={replacing}
              onDelete={onDelete}
            />
          )}
        </div>
        {(torrentName || taskNote) && (
          <div
            className={`mt-2.5 text-sub leading-5 ${
              needsAttention
                ? "rounded-xl border border-[var(--danger)]/15 bg-[var(--danger)]/[0.06] px-3 py-2.5 text-[var(--danger)]"
                : "text-white/60"
            }`}
          >
            <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-2">
              <div className="min-w-0 flex-1 basis-[28rem]">
                <OverflowText
                  lines={2}
                  className="break-words"
                  tooltipContent={
                    <span className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
                      {needsAttention && <span className="mr-1.5 font-semibold">需要处理：</span>}
                      {torrentName && <span>{torrentName}</span>}
                      {torrentName && taskNote && <span aria-hidden="true"> · </span>}
                      {taskNote}
                    </span>
                  }
                >
                  {needsAttention && <span className="mr-1.5 font-semibold">需要处理：</span>}
                  {torrentName && <span>{torrentName}</span>}
                  {torrentName && taskNote && <span aria-hidden="true"> · </span>}
                  {taskNote}
                </OverflowText>
              </div>
              {showInlineReplace && (
                <button
                  type="button"
                  onClick={() => onReplace(task)}
                  disabled={replacing}
                  className="shrink-0 rounded-lg border border-[var(--danger)]/30 bg-[var(--danger)]/[0.1] px-3 py-1.5 text-caption font-semibold text-[#fee2e2] transition hover:border-[var(--danger)]/45 hover:bg-[var(--danger)]/[0.16] disabled:cursor-wait disabled:opacity-55"
                >
                  {replacing ? "正在换种…" : "立即换种"}
                </button>
              )}
            </div>
          </div>
        )}
        {firstSubscription && (
          <div className="mt-2.5 flex flex-wrap items-start gap-x-2 gap-y-1.5 text-caption">
            <EpisodeUnitsLabel units={firstSubscription.units} />
          </div>
        )}
        <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-white/40">
          <span>{sourceLabel}</span>
          <span>{task.downloader_name || "所有下载器均未找到"}</span>
          {task.size_bytes != null && <span>{formatBytes(task.size_bytes)}</span>}
        </div>
      </div>
      {displayPercent != null && task.state !== "missing" && (
        <div className="mt-3">
          <div className="mb-1.5 grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-3 text-caption">
            <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5">
              <span className="font-medium text-white/55">{progressLabel}</span>
              {showDownloadRuntime && task.dlspeed_bytes != null && task.dlspeed_bytes > 0 && (
                <>
                  <span aria-hidden="true" className="text-white/25">
                    ·
                  </span>
                  <span className="tnum text-white/45">
                    <span aria-hidden="true">↓ </span>
                    {formatBytes(task.dlspeed_bytes)}/s
                  </span>
                </>
              )}
              {showDownloadRuntime && task.eta_seconds != null && (
                <>
                  <span aria-hidden="true" className="text-white/25">
                    ·
                  </span>
                  <span className="text-white/45">
                    剩余约 {formatDuration(task.eta_seconds)}
                  </span>
                </>
              )}
            </div>
            <span className="tnum shrink-0 font-semibold text-white/65">
              {Math.min(100, Math.max(0, displayPercent))}%
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.08]">
            <div
              className="h-full rounded-full transition-[width] duration-700"
              style={{
                width: `${Math.min(100, Math.max(displayPercent > 0 ? 1 : 0, displayPercent))}%`,
                backgroundColor: meta.color,
              }}
            />
          </div>
        </div>
      )}
      <DownloadLifecycle task={task} ingestJob={ingestJob} />
      {["error", "missing"].includes(task.state) && (
        <footer className="mt-3 flex flex-wrap items-center justify-end gap-2 border-t border-white/[0.06] pt-3">
          {["error", "missing"].includes(task.state) && (
            <Link
              href={"/settings/downloaders" as Route}
              className="rounded-lg border border-[var(--danger)]/25 bg-[var(--danger)]/[0.07] px-3 py-1.5 text-caption font-medium text-[var(--danger)]"
            >
              检查下载器
            </Link>
          )}
        </footer>
      )}
    </article>
  );
}

type LifecycleTone = "done" | "current" | "waiting" | "attention" | "future";

interface LifecycleStep {
  label: string;
  detail: string;
  tone: LifecycleTone;
}

const LIFECYCLE_DOT_STYLE: Record<LifecycleTone, string> = {
  done: "bg-[var(--ok)]",
  current: "animate-pulse bg-[var(--info)] ring-2 ring-[var(--info)]/15",
  waiting: "bg-[var(--warn)] ring-2 ring-[var(--warn)]/10",
  attention: "bg-[var(--danger)] ring-2 ring-[var(--danger)]/15",
  future: "border border-white/25",
};

const LIFECYCLE_LABEL_STYLE: Record<LifecycleTone, string> = {
  done: "text-white/60",
  current: "text-[var(--info)]",
  waiting: "text-amber-100/70",
  attention: "text-[var(--danger)]",
  future: "text-white/30",
};

const LIFECYCLE_DETAIL_STYLE: Record<LifecycleTone, string> = {
  done: "text-white/30",
  current: "text-white/30",
  waiting: "text-amber-100/40",
  attention: "text-[var(--danger)]/70",
  future: "text-white/30",
};

/**
 * 把下载器状态与关联入库 Job 投影成同一条业务过程。这里不生成新状态：每一步
 * 都能回溯到下载快照或 Job，因此刷新、重试与回退仍由原领域负责。
 */
function DownloadLifecycle({
  task,
  ingestJob,
  variant = "card",
}: {
  task: DownloadTask;
  ingestJob: JobView | null;
  variant?: "card" | "feed";
}) {
  const downloaded = task.state === "completed" || ingestJob != null;
  const downloadAttention = task.state === "error" || task.state === "missing";
  const downloadWaiting = ["stalled", "paused", "queued", "unknown"].includes(task.state);
  const percent = task.progress == null ? null : Math.floor(task.progress * 100);
  const source = task.downloader_name || "下载器";
  let downloadStep: LifecycleStep;
  if (downloadAttention) {
    downloadStep = {
      label: DOWNLOAD_STATE_META[task.state].label,
      detail: task.state === "missing" ? "等待恢复关联" : "等待下载器恢复",
      tone: "attention",
    };
  } else if (downloaded) {
    downloadStep = {
      label: "下载完成",
      detail: task.size_bytes == null ? "文件已就绪" : formatBytes(task.size_bytes),
      tone: "done",
    };
  } else {
    downloadStep = {
      label: DOWNLOAD_STATE_META[task.state].label,
      detail: percent == null ? "读取实时进度" : `${percent}%`,
      tone: downloadWaiting ? "waiting" : "current",
    };
  }

  let ingestStep: LifecycleStep;
  if (ingestJob && ATTENTION_JOB_STATUSES.has(ingestJob.status)) {
    ingestStep = {
      label: "入库待处理",
      detail: ingestJob.error?.message || ingestJob.progress.message,
      tone: "attention",
    };
  } else if (ingestJob?.status === "running") {
    ingestStep = {
      label: "正在入库",
      detail: ingestJob.progress.message,
      tone: "current",
    };
  } else if (ingestJob && ACTIVE_FEED_JOB_STATUSES.has(ingestJob.status)) {
    ingestStep = {
      label: INGEST_STATE_META[ingestJob.status].label,
      detail: ingestJob.progress.message,
      tone: "waiting",
    };
  } else if (ingestJob?.status === "succeeded") {
    ingestStep = {
      label: "入库完成",
      detail: ingestJob.progress.message,
      tone: "done",
    };
  } else if (ingestJob?.status === "cancelled") {
    ingestStep = {
      label: "入库已取消",
      detail: "等待重新处理",
      tone: "waiting",
    };
  } else {
    ingestStep = {
      label: downloaded ? "等待入库" : "等待下载完成",
      detail: "下一步",
      tone: "future",
    };
  }

  const steps: LifecycleStep[] = [
    { label: "已投递", detail: source, tone: "done" },
    downloadStep,
    ingestStep,
  ];
  const feed = variant === "feed";

  return (
    <ol
      aria-label="任务完整过程"
      className={
        feed
          ? "mt-3 space-y-1.5"
          : "mt-3 grid grid-cols-3 gap-2 border-t border-white/[0.06] pt-3 max-md:grid-cols-1"
      }
    >
      {steps.map((step, index) => (
        <li
          key={`${step.label}:${step.detail}`}
          className="relative flex min-w-0 items-start gap-2"
        >
          {index < steps.length - 1 && (
            <span
              aria-hidden="true"
              className={`absolute bottom-[-0.5rem] left-[0.21875rem] top-3 w-px bg-white/[0.1] ${
                feed ? "block" : "hidden max-md:block"
              }`}
            />
          )}
          <span
            aria-hidden="true"
            className={`mt-1.5 size-2 shrink-0 rounded-full ${LIFECYCLE_DOT_STYLE[step.tone]}`}
          />
          <span className={feed ? "flex min-w-0 flex-wrap items-baseline gap-x-2" : "min-w-0"}>
            <span className={`text-caption font-medium ${LIFECYCLE_LABEL_STYLE[step.tone]}`}>
              {step.label}
            </span>
            <OverflowText
              lines={step.tone === "attention" ? 2 : 1}
              className={`${feed ? "text-caption" : "mt-0.5 text-micro"} ${LIFECYCLE_DETAIL_STYLE[step.tone]}`}
            >
              {step.detail}
            </OverflowText>
          </span>
        </li>
      ))}
    </ol>
  );
}

/** 打开种子页与删除收进右上角菜单；查看订阅由分组标题承担，不在此重复。 */
function DownloadTaskActionsMenu({
  task,
  deleting,
  replacing,
  onDelete,
}: {
  task: DownloadTask;
  deleting: boolean;
  replacing: boolean;
  onDelete: (task: DownloadTask) => void;
}) {
  return (
    <TaskActionsMenu
      ariaLabel={`${task.name || task.info_hash}的更多操作`}
      disabled={deleting || replacing}
      items={[
        ...(task.page_url
          ? [
              {
                id: "torrent-page",
                label: "打开种子页",
                onSelect: () => {
                  window.open(task.page_url!, "_blank", "noopener,noreferrer");
                },
              },
            ]
          : []),
        ...(task.downloader_id != null
          ? [
              {
                id: "delete",
                label: deleting ? "删除中…" : "删除任务",
                onSelect: () => onDelete(task),
                disabled: deleting,
                tone: "danger" as const,
              },
            ]
          : []),
      ]}
    />
  );
}

function downloadTaskNote(
  task: DownloadTask,
  ingestJob: JobView | null,
): string | null {
  if (ingestJob?.status === "succeeded") {
    return "入库已完成，等待任务中心同步收尾";
  }
  if (ingestJob?.status === "cancelled") {
    return "上次入库已取消，等待重新处理";
  }
  if (ingestJob && ["blocked", "failed"].includes(ingestJob.status)) {
    return ingestJob.error?.message ?? ingestJob.progress.message;
  }
  if (
    ingestJob &&
    ["queued", "running", "retry_wait", "cancelling", "waiting"].includes(ingestJob.status)
  ) {
    return ingestJob.progress.message;
  }
  if (task.rescue_message) return task.rescue_message;
  if (task.state === "missing") return "已投递记录仍在，但下载器中找不到对应任务";
  if (task.state === "completed") return "下载已完成，等待监听导入或媒体库扫描收尾";
  if (task.state === "error") return "下载器报告任务异常";
  if (task.state === "paused") return "已在下载器中暂停";
  if (task.state === "queued") return "下载器正在排队，暂停无进度计时";
  if (task.state === "checking") return "下载器正在校验数据，暂停无进度计时";
  if (task.state === "stalled") return "暂无可用连接，等待继续下载";
  if (task.state === "unknown") return "下载器未返回可识别的状态";
  return null;
}

/**
 * 多集资源默认显示连续区间，同一标签内并列入库进度；点击后按季查看完整
 * 集号，已入库的集标绿。原生 details 保留了键盘与读屏交互，也不会为了
 * 这块纯展示状态引入额外 React 状态。
 */
function EpisodeUnitsLabel({
  units,
}: {
  units: DownloadTask["subscriptions"][number]["units"];
}) {
  const summary = summarizeEpisodeUnits(units);
  if (!summary) return null;
  const importedKeys = new Set(
    units
      .filter((unit) => unit.imported)
      .map((unit) => `${unit.season_number}:${unit.episode_number}`),
  );
  const importedCount = importedKeys.size;
  if (summary.kind === "movie" || summary.episodeCount <= 1) {
    return (
      <span className="tnum">
        {summary.label}
        {summary.kind === "episodes" && importedCount > 0 && (
          <span className="text-[var(--ok)]"> · 已入库</span>
        )}
      </span>
    );
  }

  return (
    <details className="group min-w-0 max-w-full open:basis-full open:w-full">
      <summary
        aria-label={`覆盖 ${summary.episodeCount} 集：${summary.fullLabel}。${
          importedCount > 0 ? `其中 ${importedCount} 集已入库。` : ""
        }展开查看全部集号`}
        className="flex w-fit max-w-full cursor-pointer list-none items-center gap-1.5 rounded-md border border-[var(--info)]/20 bg-[var(--info)]/[0.07] px-2 py-1 text-[var(--info)] transition-colors hover:bg-[var(--info)]/[0.11] [&::-webkit-details-marker]:hidden"
      >
        <OverflowText
          focusable={false}
          placement="top-start"
          className="tnum"
          tooltipContent={
            <span className="tnum break-words [overflow-wrap:anywhere]">{summary.fullLabel}</span>
          }
        >
          {summary.label}
        </OverflowText>
        {importedCount > 0 && (
          <span className="tnum shrink-0 text-[var(--ok)]">
            · 已入库 {importedCount}/{summary.episodeCount}
          </span>
        )}
        <ChevronRightIcon className="size-3 shrink-0 rotate-90 transition-transform group-open:-rotate-90" />
      </summary>
      <div className="mt-2 max-w-xl space-y-2.5 rounded-xl border border-white/[0.07] bg-white/[0.035] px-3 py-2.5">
        {summary.seasons.map((season) => (
          <div key={season.seasonNumber}>
            <div className="mb-1.5 flex items-center gap-2 font-medium text-white/65">
              <span className="tnum">S{String(season.seasonNumber).padStart(2, "0")}</span>
              <span className="font-normal text-white/35">共 {season.episodes.length} 集</span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {season.episodes.map((episode) => {
                const imported = importedKeys.has(`${season.seasonNumber}:${episode}`);
                return (
                  <span
                    key={episode}
                    title={imported ? "已入库" : undefined}
                    className={`tnum rounded-md px-1.5 py-0.5 text-micro ${
                      imported
                        ? "bg-[var(--ok)]/[0.16] text-[var(--ok)]"
                        : "bg-white/[0.055] text-white/60"
                    }`}
                  >
                    E{String(episode).padStart(2, "0")}
                  </span>
                );
              })}
            </div>
          </div>
        ))}
        {summary.seasons.length > 1 && (
          <p className="border-t border-white/[0.06] pt-2 text-right text-micro text-white/35">
            合计 {summary.episodeCount} 集
          </p>
        )}
      </div>
    </details>
  );
}

function EmptyView({ view }: { view: TaskCenterViewName }) {
  const copy: Record<TaskCenterViewName, { title: string; note: string }> = {
    all: { title: "当前没有任务", note: "新的后台作业或下载任务会自动出现在这里。" },
    attention: { title: "当前无需处理", note: "异常或需要确认的任务会优先出现在这里。" },
    active: { title: "当前没有进行中的任务", note: "新任务启动后会自动进入实时过程。" },
    history: { title: "还没有历史记录", note: "完成和取消的后台作业会保留在这里。" },
  };
  return (
    <div className="mt-8 rounded-2xl border border-white/[0.07] bg-black/20 px-6 py-16 text-center backdrop-blur-xl">
      <p className="text-title-sm font-semibold text-white/75">{copy[view].title}</p>
      <p className="mt-2 text-ui text-[var(--text-muted)]">{copy[view].note}</p>
    </div>
  );
}
