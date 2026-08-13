"use client";

import { useMemo, useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import { JobCard, TaskActionsMenu, TaskStatusDot } from "@/components/job-center";
import { useToast } from "@/components/feedback";
import {
  ClockIcon,
  DownloadIcon,
  FilmIcon,
  InfoIcon,
  TvIcon,
} from "@/components/icons";
import { Modal } from "@/components/modal";
import { PosterImage } from "@/components/poster-image";
import {
  deleteDownloadTask,
  type DownloadTask,
  type DownloadTaskSource,
} from "@/lib/api/downloaders";
import { useDownloadTasks } from "@/lib/download-tasks";
import { formatBytes, formatDuration } from "@/lib/format";
import { imageUrl } from "@/lib/image-proxy";
import type { JobView } from "@/lib/api/jobs";
import { useJobs } from "@/lib/jobs";
import { formatRelativeTime } from "@/lib/time";

type TaskView = "all" | "jobs" | "downloads";

const ATTENTION_JOB_STATUSES = new Set(["blocked", "failed"]);

const VIEW_LABELS: { id: TaskView; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "jobs", label: "后台作业" },
  { id: "downloads", label: "下载任务" },
];

/**
 * 完整任务中心：统一“观察入口”，不制造统一状态表。
 * Job 状态来自 MovieClaw 数据库，下载状态来自下载器实时快照，订阅关系仅按
 * infohash 投影视图；各自的取消、重试和入库生命周期仍由原领域负责。
 */
export function TaskCenterView() {
  const [view, setView] = useState<TaskView>("all");
  const [deletingTaskId, setDeletingTaskId] = useState<string | null>(null);
  const [pendingDeleteTask, setPendingDeleteTask] = useState<DownloadTask | null>(null);
  const toast = useToast();
  const { jobs, activeJobs } = useJobs();
  const {
    tasks: downloadTasks,
    sources,
    attentionTasks,
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

  // 后台作业按“进行中优先、历史随后”排序，让历史自然归入所属分类。
  const orderedJobs = useMemo(() => {
    const activeJobIds = new Set(activeJobs.map((job) => job.id));
    return [...activeJobs, ...jobs.filter((job) => !activeJobIds.has(job.id))];
  }, [activeJobs, jobs]);
  const attentionJobs = useMemo(
    () => jobs.filter((job) => ATTENTION_JOB_STATUSES.has(job.status)),
    [jobs],
  );
  const ingestJobsByHash = useMemo(() => {
    const linked = new Map<string, JobView>();
    for (const job of orderedJobs) {
      if (job.job_type !== "library.ingest") continue;
      for (const resource of job.resources) {
        if (resource.resource_type === "download" && !linked.has(resource.resource_id)) {
          linked.set(resource.resource_id.toLowerCase(), job);
        }
      }
    }
    return linked;
  }, [orderedJobs]);
  const visibleJobs = view === "all" || view === "jobs" ? orderedJobs : [];
  const visibleDownloads = useMemo(
    () => (view === "all" || view === "downloads" ? downloadTasks : []),
    [downloadTasks, view],
  );
  const visibleDownloadGroups = useMemo(
    () => groupDownloadTasks(visibleDownloads),
    [visibleDownloads],
  );
  const activeTotal = activeJobs.length + downloadTasks.length;
  const attentionTotal = attentionJobs.length + attentionTasks.length;
  const failedSources = sources.filter((source) => source.status !== "active");
  const healthySourceCount = sources.length - failedSources.length;

  return (
    <div className="scroll-thin scroll-safe h-full overflow-y-auto pb-10">
      <div className="mx-auto w-full max-w-[1180px] px-6 pt-7 max-md:px-4 max-md:pt-4">
        <header>
          <div>
            <div className="flex items-center gap-2.5">
              <ClockIcon className="size-6 text-[#b9e8ff]" />
              <h1 className="text-on-image text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
                任务中心
              </h1>
            </div>
            <p className="text-on-image mt-1.5 max-w-2xl text-ui leading-6 text-[var(--text-muted)]">
              在一个页面观察 MovieClaw 后台作业、下载器实时任务和订阅投递进度。
            </p>
          </div>
        </header>

        <div className="mt-4 flex min-h-10 flex-wrap items-center gap-x-3 gap-y-2 border-y border-white/[0.06] py-2.5 text-caption text-[var(--text-muted)]">
          <span className="flex items-center gap-2">
            <span
              className={`size-1.5 rounded-full ${activeTotal > 0 ? "bg-[#7dd3fc]" : "bg-white/25"}`}
            />
            {activeTotal > 0 ? (
              <span>
                <span className="tnum font-semibold text-white/80">{activeTotal}</span> 个进行中
              </span>
            ) : (
              "当前无进行中任务"
            )}
          </span>
          <span aria-hidden="true" className="h-3 w-px bg-white/10" />
          {attentionTotal > 0 ? (
            <span className="flex items-center gap-1.5 text-[#fca5a5]">
              <span className="size-1.5 rounded-full bg-[#fca5a5]" />
              <span className="tnum font-semibold">{attentionTotal}</span> 个需要处理
            </span>
          ) : (
            <span className="flex items-center gap-1.5 text-white/45">
              <span className="size-1.5 rounded-full bg-[#86efac]" />
              无需处理
            </span>
          )}
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
                {item.id === "jobs" && activeJobs.length > 0 && (
                  <span className="tnum ml-1.5 text-caption text-white/45">
                    {activeJobs.length}
                  </span>
                )}
                {item.id === "downloads" && downloadTasks.length > 0 && (
                  <span className="tnum ml-1.5 text-caption text-white/45">
                    {downloadTasks.length}
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

        {visibleJobs.length > 0 && (
          <TaskSection
            title="后台作业"
            icon={<ClockIcon className="size-4" />}
            count={visibleJobs.length}
            description="来自网页、CLI、Agent 与调度器；进行中优先，完成、失败和取消记录随后"
          >
            <div className="space-y-2.5">
              {visibleJobs.map((job) => (
                <JobCard key={job.id} job={job} onNavigate={() => undefined} />
              ))}
            </div>
          </TaskSection>
        )}

        {visibleDownloads.length > 0 && (
          <TaskSection
            title="下载任务"
            icon={<DownloadIcon className="size-4" />}
            count={visibleDownloadGroups.length}
            description={`${visibleDownloads.length} 个资源；已识别内容按作品合并展示`}
          >
            <div className="space-y-2.5">
              {visibleDownloadGroups.map((group) => (
                <DownloadTaskGroupCard
                  key={group.key}
                  group={group}
                  ingestJobsByHash={ingestJobsByHash}
                  deletingTaskId={deletingTaskId}
                  onDelete={setPendingDeleteTask}
                />
              ))}
            </div>
          </TaskSection>
        )}

        {visibleJobs.length === 0 && visibleDownloads.length === 0 && !loading && (
          <EmptyView view={view} />
        )}

        {loading && visibleJobs.length === 0 && visibleDownloads.length === 0 && (
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
            关联订阅会暂时标记该任务缺失，并在后续巡检中重新寻找资源。
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

function TaskSection({
  title,
  description,
  count,
  icon,
  children,
}: {
  title: string;
  description: string;
  count: number;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-6">
      <div className="mb-3 flex items-end gap-2">
        <span className="mb-0.5 text-[#b9e8ff]">{icon}</span>
        <h2 className="text-title-sm font-semibold text-white/90">{title}</h2>
        <span className="tnum text-caption text-white/40">{count}</span>
        <p className="ml-2 min-w-0 truncate text-caption text-[var(--text-faint)] max-md:hidden">
          {description}
        </p>
      </div>
      {children}
    </section>
  );
}

const DOWNLOAD_STATE_META: Record<
  DownloadTask["state"],
  { label: string; color: string; dot: string }
> = {
  downloading: {
    label: "下载中",
    color: "#7dd3fc",
    dot: "animate-pulse bg-[#7dd3fc]",
  },
  stalled: {
    label: "等待连接",
    color: "#f5c451",
    dot: "bg-[#fcd34d]",
  },
  paused: {
    label: "已暂停",
    color: "#c4b5fd",
    dot: "bg-[#c4b5fd]",
  },
  completed: {
    label: "等待入库",
    color: "#86efac",
    dot: "bg-[#86efac]",
  },
  error: {
    label: "下载异常",
    color: "#fca5a5",
    dot: "bg-[#fca5a5]",
  },
  missing: {
    label: "任务缺失",
    color: "#fca5a5",
    dot: "bg-[#fca5a5]",
  },
  unknown: {
    label: "状态未知",
    color: "#cbd5e1",
    dot: "bg-white/40",
  },
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

function DownloadTaskGroupCard({
  group,
  ingestJobsByHash,
  deletingTaskId,
  onDelete,
}: {
  group: DownloadTaskGroup;
  ingestJobsByHash: Map<string, JobView>;
  deletingTaskId: string | null;
  onDelete: (task: DownloadTask) => void;
}) {
  if (group.mediaItemId == null) {
    const task = group.tasks[0];
    return (
      <DownloadTaskCard
        task={task}
        ingestJob={ingestJobsByHash.get(task.info_hash.toLowerCase()) ?? null}
        deleting={deletingTaskId === task.id}
        onDelete={onDelete}
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
          <h3 className="truncate text-ui font-semibold text-white/90">{group.title}</h3>
          <p className="mt-0.5 text-caption text-white/40">
            {isTv ? "剧集" : "电影"}
            <span aria-hidden="true"> · </span>
            {group.tasks.length} 个下载资源
          </p>
        </div>
        {firstSubscription && (
          <Link
            href={`/subscriptions/${firstSubscription.id}` as Route}
            className="shrink-0 rounded-lg border border-white/10 px-3 py-1.5 text-caption font-medium text-white/60 hover:bg-white/[0.06] hover:text-white max-md:hidden"
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
            onDelete={onDelete}
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
  onDelete,
}: {
  task: DownloadTask;
  ingestJob: JobView | null;
  grouped?: boolean;
  deleting: boolean;
  onDelete: (task: DownloadTask) => void;
}) {
  const ingestActive =
    ingestJob !== null &&
    ["queued", "running", "retry_wait", "cancelling", "waiting"].includes(ingestJob.status);
  const ingestNeedsAttention =
    ingestJob !== null && ["blocked", "failed"].includes(ingestJob.status);
  const meta = ingestNeedsAttention
    ? {
        label: "入库待处理",
        color: "#fca5a5",
        dot: "bg-[#fca5a5]",
      }
    : ingestActive
      ? {
          label: "正在入库",
          color: "#86efac",
          dot: "animate-pulse bg-[#86efac]",
        }
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
  const displayPercent =
    ingestActive || ingestNeedsAttention
      ? ingestJob?.progress.percent == null
        ? null
        : Math.floor(ingestJob.progress.percent)
      : percent;
  const progressLabel = ingestActive || ingestNeedsAttention ? "入库进度" : "下载进度";
  const firstSubscription = task.subscriptions[0];
  const needsAttention =
    ingestNeedsAttention || task.state === "error" || task.state === "missing";

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
            <h3
              title={title}
              className="min-w-0 truncate text-ui font-semibold leading-5 text-white/90"
            >
              {title}
            </h3>
          </div>
          {task.downloader_id != null && (
            <DownloadTaskActionsMenu task={task} deleting={deleting} onDelete={onDelete} />
          )}
        </div>
        <div
          className={`mt-2.5 text-sub leading-5 ${
            needsAttention
              ? "rounded-xl border border-[#fca5a5]/15 bg-[#fca5a5]/[0.06] px-3 py-2.5 text-[#fecaca]"
              : "text-white/60"
          }`}
        >
          <p className="line-clamp-2 min-h-10 break-words">
            {needsAttention && <span className="mr-1.5 font-semibold">需要处理：</span>}
            {torrentName && <span>{torrentName} · </span>}
            {downloadTaskNote(task, percent, ingestJob)}
          </p>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-white/40">
          <span>{sourceLabel}</span>
          <span>{task.downloader_name || "所有下载器均未找到"}</span>
          {task.size_bytes != null && <span>{formatBytes(task.size_bytes)}</span>}
          {task.dlspeed_bytes != null && task.dlspeed_bytes > 0 && (
            <span>{formatBytes(task.dlspeed_bytes)}/s</span>
          )}
          {task.eta_seconds != null && <span>剩余约 {formatDuration(task.eta_seconds)}</span>}
          {firstSubscription && <span>{formatUnits(firstSubscription.units)}</span>}
        </div>
      </div>
      {displayPercent != null && task.state !== "missing" && (
        <div className="mt-3">
          <div className="mb-1.5 flex items-center justify-between gap-3 text-caption">
            <span className="font-medium text-white/55">{progressLabel}</span>
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
      {(firstSubscription || ["error", "missing"].includes(task.state)) && (
        <footer className="mt-3 flex flex-wrap items-center justify-end gap-2 border-t border-white/[0.06] pt-3">
          {firstSubscription && (
            <Link
              href={`/subscriptions/${firstSubscription.id}` as Route}
              className="rounded-lg border border-white/10 px-3 py-1.5 text-caption font-medium text-white/65 hover:bg-white/[0.06] hover:text-white"
            >
              查看订阅
            </Link>
          )}
          {["error", "missing"].includes(task.state) && (
            <Link
              href={"/settings/downloaders" as Route}
              className="rounded-lg border border-[#fca5a5]/25 bg-[#fca5a5]/[0.07] px-3 py-1.5 text-caption font-medium text-[#fecaca]"
            >
              检查下载器
            </Link>
          )}
        </footer>
      )}
    </article>
  );
}

/** 下载任务的破坏性操作统一收进卡片右上角菜单，避免抢占状态展示空间。 */
function DownloadTaskActionsMenu({
  task,
  deleting,
  onDelete,
}: {
  task: DownloadTask;
  deleting: boolean;
  onDelete: (task: DownloadTask) => void;
}) {
  return (
    <TaskActionsMenu
      ariaLabel={`${task.name || task.info_hash}的更多操作`}
      disabled={deleting}
      items={[
        {
          id: "delete",
          label: deleting ? "删除中…" : "删除任务",
          onSelect: () => onDelete(task),
          disabled: deleting,
          tone: "danger",
        },
      ]}
    />
  );
}

function downloadTaskNote(
  task: DownloadTask,
  percent: number | null,
  ingestJob: JobView | null,
): string {
  if (ingestJob && ["blocked", "failed"].includes(ingestJob.status)) {
    return ingestJob.error?.message ?? ingestJob.progress.message;
  }
  if (
    ingestJob &&
    ["queued", "running", "retry_wait", "cancelling", "waiting"].includes(ingestJob.status)
  ) {
    const ingestPercent = ingestJob.progress.percent;
    return `${ingestJob.progress.message}${ingestPercent != null ? ` · ${Math.round(ingestPercent)}%` : ""}`;
  }
  if (task.state === "missing") return "已投递记录仍在，但下载器中找不到对应任务";
  if (task.state === "completed") return "下载已完成，等待监听导入或媒体库扫描收尾";
  if (task.state === "error") return `${percent ?? 0}% · 下载器报告任务异常`;
  if (task.state === "paused") return `${percent ?? 0}% · 已在下载器中暂停`;
  if (task.state === "stalled") return `${percent ?? 0}% · 暂无可用连接，等待继续下载`;
  if (task.state === "unknown") return `${percent ?? 0}% · 下载器未返回可识别的状态`;
  return `${percent ?? 0}% · 正在下载`;
}

function formatUnits(units: DownloadTask["subscriptions"][number]["units"]): string {
  if (units.some((unit) => unit.season_number === 0 && unit.episode_number === 0)) return "正片";
  const labels = units.map(
    (unit) =>
      `S${String(unit.season_number).padStart(2, "0")}E${String(unit.episode_number).padStart(2, "0")}`,
  );
  return labels.length <= 3 ? labels.join("、") : `${labels.slice(0, 3).join("、")} 等 ${labels.length} 集`;
}

function EmptyView({ view }: { view: TaskView }) {
  const copy: Record<TaskView, { title: string; note: string }> = {
    all: { title: "当前没有任务", note: "新的后台作业或下载任务会自动出现在这里。" },
    jobs: { title: "还没有后台作业", note: "后台作业及其历史记录会统一显示在这里。" },
    downloads: { title: "当前没有下载任务", note: "下载器中的未完成任务会自动汇总到这里。" },
  };
  return (
    <div className="mt-8 rounded-2xl border border-white/[0.07] bg-black/20 px-6 py-16 text-center backdrop-blur-xl">
      <p className="text-title-sm font-semibold text-white/75">{copy[view].title}</p>
      <p className="mt-2 text-ui text-[var(--text-muted)]">{copy[view].note}</p>
    </div>
  );
}
