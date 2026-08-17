"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { DirectoryPicker } from "@/components/directory-picker";
import { useConfirm } from "@/components/feedback";
import { DownloadIcon, FolderIcon, MoreIcon, PlusIcon, XIcon } from "@/components/icons";
import { useBackdrop } from "@/lib/backdrop";
import { Modal } from "@/components/modal";
import {
  type ConfiguredDownloader,
  type DownloaderClientType,
  type DownloaderLimits,
  type DownloaderPayload,
  type DownloaderStatus,
  type PathMapping,
  createDownloader,
  deleteDownloader,
  getDownloaderLimits,
  listDownloaders,
  reverifyDownloader,
  setDefaultDownloader,
  setDownloaderEnabled,
  setDownloaderLimits,
  updateDownloader,
} from "@/lib/api/downloaders";
import { formatRelativeTime } from "@/lib/time";
import { useVisiblePolling } from "@/lib/use-visible-polling";
import { LiquidGlassButton } from "@/vendor/liquid-glass";

/** 连接状态 → 展示文案与颜色（与站点配置同语言） */
const STATUS_META: Record<DownloaderStatus, { label: string; color: string }> = {
  active: { label: "已连接", color: "var(--ok)" },
  verifying: { label: "测试中", color: "var(--info)" },
  pending: { label: "待测试", color: "#c0c4cc" },
  failed: { label: "连接失败", color: "var(--danger)" },
};

/** 下载器类型 → 展示名 */
const TYPE_LABEL: Record<DownloaderClientType, string> = {
  qbittorrent: "qBittorrent",
  transmission: "Transmission",
};

/** 各类型的地址占位提示（qB 是 WebUI 地址，Tr 是 RPC 地址，端口不同） */
const URL_PLACEHOLDER: Record<DownloaderClientType, string> = {
  qbittorrent: "http://192.168.1.10:8080",
  transmission: "http://192.168.1.10:9091",
};

/** 需要轮询测试进度的中间态 */
const IN_PROGRESS: DownloaderStatus[] = ["pending", "verifying"];

/**
 * 「下载器」设置分区。
 *
 * 与站点配置同构：列表展示已接入的下载器，「添加下载器」展开表单；
 * 保存后后端异步测试连接，前端对中间态（pending/verifying）轮询刷新，
 * 直到 active / failed。搜索结果里的"提交下载"以这里配置的实例为目标。
 */
export function DownloaderConfigSection() {
  const [downloaders, setDownloaders] = useState<ConfiguredDownloader[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 是否展开「添加下载器」面板
  const [adding, setAdding] = useState(false);
  // 当前展开编辑表单的下载器 id（null 表示没有展开的编辑表单）
  const [editing, setEditing] = useState<number | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setDownloaders(await listDownloaders());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 体检修复卡跳转带来的映射建议（?suggest_mapping=/xx）：自动展开默认
  // 下载器的编辑表单并预填一条映射的本机侧，用户只需补下载器视角的路径
  const [suggestMapping, setSuggestMapping] = useState<string | null>(null);
  useEffect(() => {
    const suggested = new URLSearchParams(window.location.search).get("suggest_mapping");
    if (suggested) setSuggestMapping(suggested);
  }, []);
  const suggestConsumed = useRef(false);
  useEffect(() => {
    if (!suggestMapping || loading || suggestConsumed.current || downloaders.length === 0) return;
    suggestConsumed.current = true;
    const target = downloaders.find((d) => d.is_default) ?? downloaders[0];
    setEditing(target.id);
  }, [suggestMapping, loading, downloaders]);

  // 有下载器处于 pending/verifying 时轮询刷新，直到全部落定（页面隐藏时暂停）
  const hasInProgress = downloaders.some((d) => IN_PROGRESS.includes(d.status));
  useVisiblePolling(
    () => {
      void listDownloaders()
        .then(setDownloaders)
        .catch(() => {
          /* 轮询失败静默重试，不打断页面 */
        });
    },
    hasInProgress ? 2000 : null,
  );

  // 原地替换已有条目、新条目追加到末尾（保持列表顺序稳定，避免操作后跳位）
  const upsert = useCallback((next: ConfiguredDownloader) => {
    setDownloaders((prev) => {
      const idx = prev.findIndex((d) => d.id === next.id);
      if (idx === -1) return [...prev, next];
      const copy = [...prev];
      copy[idx] = next;
      return copy;
    });
  }, []);

  const usableCount = downloaders.filter((d) => d.usable).length;

  return (
    <div className="space-y-5">
      {error && (
        <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
          {error}
        </div>
      )}

      <div className="flex items-center justify-between gap-4">
        <p className="text-sub text-[var(--text-muted)]">
          {loading
            ? "加载中…"
            : downloaders.length === 0
              ? "接入你自己部署的下载软件，资源将由它们完成下载。"
              : `已接入 ${downloaders.length} 个下载器，${usableCount} 个可用。保存后系统会自动测试连接。`}
        </p>
        <div className="flex shrink-0 items-center gap-2.5">
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="btn-glass px-3.5 py-1.5 text-sub font-medium"
          >
            刷新
          </button>
          <button
            type="button"
            onClick={() => setAdding((v) => !v)}
            disabled={loading}
            className="btn-accent flex items-center gap-1 rounded-full py-1.5 pl-2.5 pr-3.5 text-sub font-semibold disabled:opacity-60"
          >
            <PlusIcon className="size-4" />
            添加下载器
          </button>
        </div>
      </div>

      {/* 「添加下载器」面板 */}
      {adding && (
        <div className="css-glass !rounded-xl p-4">
          <DownloaderForm
            downloader={null}
            onSubmit={async (payload) => {
              upsert(await createDownloader(payload));
              setAdding(false);
            }}
            onCancel={() => setAdding(false)}
            onError={setError}
          />
        </div>
      )}

      {/* 已配置下载器列表 */}
      <div className="space-y-2.5">
        {loading ? (
          <>
            <div className="h-[72px] animate-pulse rounded-xl bg-white/[0.04]" />
            <div className="h-[72px] animate-pulse rounded-xl bg-white/[0.04]" />
          </>
        ) : downloaders.length === 0 ? (
          <div className="css-glass flex flex-col items-center gap-3 !rounded-2xl px-6 py-12 text-center">
            <span className="icon-chip size-12 !rounded-2xl">
              <DownloadIcon className="size-6" />
            </span>
            <div>
              <p className="text-body font-medium text-[var(--text)]">还没有接入任何下载器</p>
              <p className="mt-1 text-sub text-[var(--text-muted)]">
                点击右上角「添加下载器」，支持 qBittorrent 和 Transmission。
              </p>
            </div>
          </div>
        ) : (
          downloaders.map((downloader) => (
            <DownloaderCard
              key={downloader.id}
              downloader={downloader}
              suggestMapping={suggestMapping}
              expanded={editing === downloader.id}
              onToggleForm={(open) => setEditing(open ? downloader.id : null)}
              onChanged={upsert}
              onDeleted={(id) => {
                setDownloaders((prev) => prev.filter((d) => d.id !== id));
                setEditing((cur) => (cur === id ? null : cur));
                // 删除默认时后端会把默认让给另一台，整体刷新拿到新归属
                void load();
              }}
              onRefresh={() => void load()}
              onError={setError}
            />
          ))
        )}
      </div>
    </div>
  );
}

/* —— 单个下载器卡片：折叠态展示状态 + 操作，展开态是连接表单 —— */

interface DownloaderCardProps {
  downloader: ConfiguredDownloader;
  /** 体检跳转建议预填的映射本机侧路径（无建议时为 null） */
  suggestMapping: string | null;
  expanded: boolean;
  onToggleForm: (open: boolean) => void;
  onChanged: (downloader: ConfiguredDownloader) => void;
  onDeleted: (id: number) => void;
  /** 需要整体刷新列表的操作（如设默认会同时改动其他条目）之后调用 */
  onRefresh: () => void;
  onError: (message: string) => void;
}

function DownloaderCard({
  downloader,
  suggestMapping,
  expanded,
  onToggleForm,
  onChanged,
  onDeleted,
  onRefresh,
  onError,
}: DownloaderCardProps) {
  const { backdrop } = useBackdrop();
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [limitsOpen, setLimitsOpen] = useState(false);
  const meta = STATUS_META[downloader.status];

  async function guard(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // 副标题：失败时给出原因，正常时展示 类型 + 版本 + 上次检查时间
  const subtitle =
    downloader.status === "failed" && downloader.last_error
      ? downloader.last_error
      : [
          TYPE_LABEL[downloader.client_type],
          downloader.version,
          `上次检查 ${formatRelativeTime(downloader.last_checked_at)}`,
        ]
          .filter(Boolean)
          .join(" · ");

  return (
    <div className="css-glass !rounded-xl">
      <div className="flex items-center gap-3.5 p-4">
        <span className="icon-chip size-10 !rounded-xl">
          <DownloadIcon className="size-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="truncate text-body font-semibold text-[var(--text)]">{downloader.name}</p>
            {downloader.is_default && (
              <span className="shrink-0 rounded-full border border-white/[0.12] bg-[var(--accent-soft)] px-2 py-0.5 text-caption font-semibold text-[var(--accent)]">
                默认
              </span>
            )}
            <span
              className="flex shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 text-caption font-medium"
              style={{ background: `color-mix(in oklab, ${meta.color} 12%, transparent)`, color: meta.color }}
            >
              <span className="size-1.5 rounded-full" style={{ background: meta.color }} />
              {meta.label}
            </span>
          </div>
          <p className="mt-0.5 truncate text-caption text-[var(--text-faint)]" title={downloader.url}>
            {subtitle}
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {/* 启用开关：与站点卡片同款受控 WebGL 液态玻璃开关 */}
          <LiquidGlassButton
            backgroundImage={backdrop}
            variant="dark"
            checked={downloader.enabled}
            disabled={busy}
            aria-label={downloader.enabled ? "已启用，点击停用" : "已停用，点击启用"}
            onCheckedChange={(enabled) =>
              void guard(async () => onChanged(await setDownloaderEnabled(downloader.id, enabled)))
            }
            className="!min-h-0 !w-auto !gap-0 !bg-transparent !p-0"
          >
            <span className="sr-only">{downloader.enabled ? "已启用" : "已停用"}</span>
          </LiquidGlassButton>
          <DownloaderActionsMenu
            expanded={expanded}
            busy={busy}
            isDefault={downloader.is_default}
            canReverify={!IN_PROGRESS.includes(downloader.status)}
            onEdit={() => onToggleForm(!expanded)}
            onLimits={() => setLimitsOpen(true)}
            onSetDefault={() =>
              void guard(async () => {
                await setDefaultDownloader(downloader.id);
                // 原默认的标记同时被清掉，整体刷新一次拿到全量新状态
                onRefresh();
              })
            }
            onReverify={() =>
              void guard(async () => onChanged(await reverifyDownloader(downloader.id)))
            }
            onDelete={() =>
              void guard(async () => {
                if (
                  !(await confirm({
                    title: `删除下载器「${downloader.name}」？`,
                    description: "下载器中的任务不受影响，只是 movieclaw 不再向它投递。",
                    confirmLabel: "删除",
                    tone: "danger",
                  }))
                ) {
                  return;
                }
                await deleteDownloader(downloader.id);
                onDeleted(downloader.id);
              })
            }
          />
        </div>
      </div>

      {/* 连接信息带：地址、默认保存目录与路径映射，一眼可核对 */}
      <div className="flex flex-wrap gap-x-7 gap-y-2 border-t border-white/[0.06] px-4 py-3">
        <InfoStat label="地址" value={downloader.url} />
        {downloader.username && <InfoStat label="用户名" value={downloader.username} />}
        <InfoStat label="默认保存目录" value={downloader.save_path ?? "下载器默认"} />
        {(downloader.path_mappings?.length ?? 0) > 0 && (
          <InfoStat
            label="路径映射"
            value={downloader.path_mappings!.map((m) => `${m.local} → ${m.remote}`).join("；")}
          />
        )}
      </div>

      {/* 展开态：编辑表单 */}
      {expanded && (
        <div className="border-t border-white/[0.06] p-4">
          <DownloaderForm
            downloader={downloader}
            suggestMapping={suggestMapping}
            onSubmit={async (payload) => {
              onChanged(await updateDownloader(downloader.id, payload));
              onToggleForm(false);
            }}
            onCancel={() => onToggleForm(false)}
            onError={onError}
          />
        </div>
      )}

      <DownloaderLimitsModal
        open={limitsOpen}
        downloader={downloader}
        onClose={() => setLimitsOpen(false)}
      />
    </div>
  );
}

/* —— 限速与队列弹窗：实时读写下载器的全局限速与任务队列上限 ——
   队列上限（qB 最大活动种子数 / Tr 下载与做种队列）决定同时活动的任务数，
   刷流做种多时最容易撞上：任务进 queued 排队、免费窗口内可能下不完。 */

const KIB = 1024;

function DownloaderLimitsModal({
  open,
  downloader,
  onClose,
}: {
  open: boolean;
  downloader: ConfiguredDownloader;
  onClose: () => void;
}) {
  const { backdrop } = useBackdrop();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 表单态：限速以 KiB/s 字符串承载（空 = 不限速）；队列数值空 = 保持现状
  const [dlKib, setDlKib] = useState("");
  const [upKib, setUpKib] = useState("");
  const [altSpeed, setAltSpeed] = useState(false);
  const [queueEnabled, setQueueEnabled] = useState(false);
  const [maxDown, setMaxDown] = useState("");
  const [maxUp, setMaxUp] = useState("");
  const [maxTotal, setMaxTotal] = useState("");
  // Transmission 没有「最大活动种子数」概念（读回 null 时隐藏该输入）
  const [supportsMaxTotal, setSupportsMaxTotal] = useState(true);

  const applyLimits = useCallback((limits: DownloaderLimits) => {
    setDlKib(
      limits.download_limit_bytes != null
        ? String(Math.round(limits.download_limit_bytes / KIB))
        : "",
    );
    setUpKib(
      limits.upload_limit_bytes != null
        ? String(Math.round(limits.upload_limit_bytes / KIB))
        : "",
    );
    setAltSpeed(Boolean(limits.alt_speed_enabled));
    setQueueEnabled(Boolean(limits.queue_enabled));
    setMaxDown(limits.max_active_downloads != null ? String(limits.max_active_downloads) : "");
    setMaxUp(limits.max_active_uploads != null ? String(limits.max_active_uploads) : "");
    setMaxTotal(
      limits.max_active_torrents != null ? String(limits.max_active_torrents) : "",
    );
    setSupportsMaxTotal(limits.max_active_torrents != null);
  }, []);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    getDownloaderLimits(downloader.id)
      .then((limits) => applyLimits(limits))
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [open, downloader.id, applyLimits]);

  function parseSpeed(raw: string): number | null {
    if (!raw.trim()) return null; // 空 = 不限速
    const kib = Math.round(Number(raw.trim()));
    return Number.isFinite(kib) && kib > 0 ? kib * KIB : null;
  }

  function parseCount(raw: string): number | null {
    if (!raw.trim()) return null; // 空 = 保持现状
    const value = Math.round(Number(raw.trim()));
    return Number.isFinite(value) && value >= 0 ? value : null;
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const applied = await setDownloaderLimits(downloader.id, {
        download_limit_bytes: parseSpeed(dlKib),
        upload_limit_bytes: parseSpeed(upKib),
        alt_speed_enabled: altSpeed,
        queue_enabled: queueEnabled,
        max_active_downloads: parseCount(maxDown),
        max_active_uploads: parseCount(maxUp),
        max_active_torrents: supportsMaxTotal ? parseCount(maxTotal) : null,
      });
      applyLimits(applied); // 回读生效值（下载器可能钳制），表单即时对齐
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} label="限速与队列" width="lg">
      <div className="space-y-4 p-6">
        <div>
          <h2 className="text-title font-bold text-[var(--text)]">
            限速与队列 · {downloader.name}
          </h2>
          <p className="mt-1 text-sub leading-5 text-[var(--text-muted)]">
            实时读写下载器的全局设置。限速留空 = 不限；队列上限决定同时活动的任务数，
            开着刷流大量做种时建议调大做种/活动上限，避免新任务排队。
          </p>
        </div>

        {error && (
          <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
            {error}
          </div>
        )}

        {loading ? (
          <div className="space-y-2">
            <div className="h-11 animate-pulse rounded-xl bg-white/[0.04]" />
            <div className="h-11 animate-pulse rounded-xl bg-white/[0.04]" />
          </div>
        ) : (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
              <LimitInput
                label="全局下载限速"
                value={dlKib}
                unit="KiB/s"
                placeholder="不限"
                disabled={busy}
                onChange={setDlKib}
              />
              <LimitInput
                label="全局上传限速"
                value={upKib}
                unit="KiB/s"
                placeholder="不限"
                disabled={busy}
                onChange={setUpKib}
              />
            </div>

            <label className="flex items-center justify-between gap-3">
              <span className="text-sub text-[var(--text-muted)]">
                备用限速档（计划任务/手动一键慢速时生效的那组限速）
              </span>
              <LiquidGlassButton
                backgroundImage={backdrop}
                variant="dark"
                checked={altSpeed}
                disabled={busy}
                aria-label="备用限速档"
                onCheckedChange={setAltSpeed}
                className="!min-h-0 !w-auto shrink-0 !gap-0 !bg-transparent !p-0"
              >
                <span className="sr-only">备用限速档</span>
              </LiquidGlassButton>
            </label>

            <label className="flex items-center justify-between gap-3">
              <span className="text-sub text-[var(--text-muted)]">
                任务队列（超出上限的任务排队等待）
              </span>
              <LiquidGlassButton
                backgroundImage={backdrop}
                variant="dark"
                checked={queueEnabled}
                disabled={busy}
                aria-label="任务队列"
                onCheckedChange={setQueueEnabled}
                className="!min-h-0 !w-auto shrink-0 !gap-0 !bg-transparent !p-0"
              >
                <span className="sr-only">任务队列</span>
              </LiquidGlassButton>
            </label>

            {queueEnabled && (
              <div className="grid grid-cols-3 gap-3 max-md:grid-cols-1">
                <LimitInput
                  label="最大同时下载数"
                  value={maxDown}
                  disabled={busy}
                  onChange={setMaxDown}
                />
                <LimitInput
                  label="最大做种数"
                  value={maxUp}
                  disabled={busy}
                  onChange={setMaxUp}
                />
                {supportsMaxTotal && (
                  <LimitInput
                    label="最大活动种子数"
                    value={maxTotal}
                    disabled={busy}
                    onChange={setMaxTotal}
                  />
                )}
              </div>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2.5 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="btn-glass px-4 py-2 text-sub font-medium"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void save()}
            disabled={busy || loading}
            className="btn-accent rounded-full px-4 py-2 text-sub font-semibold disabled:opacity-60"
          >
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** 带单位后缀的数字输入（与 feedback 层 prompt 的单位内嵌设计同款）。 */
function LimitInput({
  label,
  value,
  unit,
  placeholder,
  disabled,
  onChange,
}: {
  label: string;
  value: string;
  unit?: string;
  placeholder?: string;
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <div>
      <label className="mb-1.5 block text-caption font-medium text-[var(--text-muted)]">
        {label}
      </label>
      <div className="relative">
        <input
          type="number"
          min={0}
          value={value}
          placeholder={placeholder}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          className={`w-full rounded-lg border border-white/10 bg-white/[0.06] px-3 py-2 text-ui text-white outline-none transition [appearance:textfield] focus:border-white/25 [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none ${
            unit ? "pr-14" : ""
          }`}
        />
        {unit && (
          <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-caption font-medium text-[var(--text-faint)]">
            {unit}
          </span>
        )}
      </div>
    </div>
  );
}

function InfoStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <p className="text-micro text-[var(--text-faint)]">{label}</p>
      <p className="mt-0.5 truncate text-ui font-semibold text-[var(--text)]">{value}</p>
    </div>
  );
}

/* —— 下载器操作折叠菜单（与站点卡片同款 Radix DropdownMenu） —— */

interface DownloaderActionsMenuProps {
  expanded: boolean;
  busy: boolean;
  /** 已是默认时「设为默认」置灰 */
  isDefault: boolean;
  canReverify: boolean;
  onEdit: () => void;
  onLimits: () => void;
  onSetDefault: () => void;
  onReverify: () => void;
  onDelete: () => void;
}

function DownloaderActionsMenu({
  expanded,
  busy,
  isDefault,
  canReverify,
  onEdit,
  onLimits,
  onSetDefault,
  onReverify,
  onDelete,
}: DownloaderActionsMenuProps) {
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-sub font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="更多操作"
          className="glass-row !w-auto p-1.5 data-[state=open]:!bg-[var(--glass-fill-active)] data-[state=open]:!text-[var(--text)]"
        >
          <MoreIcon className="size-4" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[9rem] !rounded-xl p-1"
        >
          <DropdownMenu.Item onSelect={onEdit} className={itemClass}>
            {expanded ? "收起编辑" : "编辑配置"}
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={onLimits} disabled={busy} className={itemClass}>
            限速与队列…
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={onSetDefault}
            disabled={busy || isDefault}
            className={itemClass}
          >
            设为默认
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={onReverify}
            disabled={busy || !canReverify}
            className={itemClass}
          >
            重新测试连接
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={onDelete}
            disabled={busy}
            className={`${itemClass} !text-[#ff6b6b] data-[highlighted]:!bg-[#ff6b6b]/10`}
          >
            删除配置
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/* —— 连接表单：类型 + 名称 + 地址 + 可选凭证/保存目录，新增与编辑共用 —— */

interface DownloaderFormProps {
  /** 编辑对象；null 表示新增 */
  downloader: ConfiguredDownloader | null;
  /** 体检跳转建议预填的映射本机侧路径（无建议时不传） */
  suggestMapping?: string | null;
  onSubmit: (payload: DownloaderPayload) => Promise<void>;
  onCancel: () => void;
  onError: (message: string) => void;
}

/** 已有映射按前缀已覆盖建议路径时不再追加，避免预填出冗余行。 */
function withSuggestedMapping(existing: PathMapping[], suggest: string | null): PathMapping[] {
  if (!suggest) return existing;
  const norm = (p: string) => p.trim().replace(/\/+$/, "") || "/";
  const target = norm(suggest);
  const covered = existing.some((m) => {
    const local = norm(m.local);
    return local !== "/" && (target === local || target.startsWith(local + "/"));
  });
  return covered ? existing : [...existing, { local: suggest, remote: "" }];
}

function DownloaderForm({
  downloader,
  suggestMapping = null,
  onSubmit,
  onCancel,
  onError,
}: DownloaderFormProps) {
  const [busy, setBusy] = useState(false);
  const [clientType, setClientType] = useState<DownloaderClientType>(
    downloader?.client_type ?? "qbittorrent",
  );
  const [name, setName] = useState(downloader?.name ?? "");
  const [url, setUrl] = useState(downloader?.url ?? "");
  const [username, setUsername] = useState(downloader?.username ?? "");
  // 出于安全后端不回传密码，编辑时留空需重新填写（未开鉴权则保持留空）
  const [password, setPassword] = useState("");
  const [savePath, setSavePath] = useState(downloader?.save_path ?? "");
  // 路径映射：跨容器部署时 movieclaw 与下载器看同一块盘的两个名字。
  // 体检跳转带建议时预填一行本机侧（右侧留给用户按下载器视角补齐）
  const [mappings, setMappings] = useState<PathMapping[]>(
    withSuggestedMapping(downloader?.path_mappings ?? [], suggestMapping),
  );
  // 已有映射的编辑态或带预填建议时默认展开，否则折叠（绝大多数部署用不到）
  const [mappingsOpen, setMappingsOpen] = useState(
    (downloader?.path_mappings?.length ?? 0) > 0 || suggestMapping !== null,
  );
  // 目录弹窗当前服务的字段："save" = 默认保存目录，数字 = 第 N 条映射的左列
  const [pickerTarget, setPickerTarget] = useState<"save" | number | null>(null);

  // 映射行要么删掉要么填完整：下载器侧必须是绝对路径
  const mappingsComplete = mappings.every(
    (m) => m.local.trim().length > 0 && m.remote.trim().startsWith("/"),
  );
  // 两端各自查重（尾部斜杠归一后比较）：同一 movieclaw 路径两条映射翻译结果
  // 看遍历顺序，两条映射指向同一下载器路径同样是错配
  const normPath = (p: string) => p.trim().replace(/\/+$/, "") || "/";
  const localPaths = mappings.map((m) => normPath(m.local)).filter((p) => p !== "/");
  const remotePaths = mappings.map((m) => normPath(m.remote)).filter((p) => p !== "/");
  const mappingsUnique =
    new Set(localPaths).size === localPaths.length &&
    new Set(remotePaths).size === remotePaths.length;
  const canSubmit =
    name.trim().length > 0 &&
    /^https?:\/\/.+/.test(url.trim()) &&
    mappingsComplete &&
    mappingsUnique;

  function submit() {
    setBusy(true);
    void onSubmit({
      name: name.trim(),
      client_type: clientType,
      url: url.trim(),
      username: username.trim() || null,
      password: password || null,
      save_path: savePath.trim() || null,
      path_mappings: mappings.length
        ? mappings.map((m) => ({ local: m.local.trim(), remote: m.remote.trim() }))
        : null,
      enabled: downloader?.enabled ?? true,
    })
      .catch((e) => onError((e as Error).message))
      .finally(() => setBusy(false));
  }

  function setMapping(index: number, patch: Partial<PathMapping>) {
    setMappings((prev) => prev.map((m, i) => (i === index ? { ...m, ...patch } : m)));
  }

  const inputClass =
    "w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-ui " +
    "text-[var(--text)] outline-none focus:border-[var(--accent)]/60";
  const labelClass = "mb-1.5 block text-sub font-medium text-[var(--text-muted)]";

  return (
    <div className="space-y-4">
      {/* 下载器类型 */}
      <div>
        <label className={labelClass}>下载器类型</label>
        <div className="flex flex-wrap gap-2">
          {(Object.keys(TYPE_LABEL) as DownloaderClientType[]).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setClientType(t)}
              data-active={clientType === t}
              className="glass-row nav-item !w-auto px-3 py-1.5 text-sub font-medium"
            >
              {TYPE_LABEL[t]}
            </button>
          ))}
        </div>
      </div>

      <div>
        <label className={labelClass}>名称</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="如：家里的 qBittorrent"
          autoComplete="off"
          className={inputClass}
        />
      </div>

      <div>
        <label className={labelClass}>
          {clientType === "qbittorrent" ? "WebUI 地址" : "RPC 地址"}
        </label>
        <input
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder={URL_PLACEHOLDER[clientType]}
          autoComplete="off"
          className={inputClass}
        />
      </div>

      {/* 凭证：未开鉴权的下载器可整体留空 */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={labelClass}>用户名（可选）</label>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="off"
            className={inputClass}
          />
        </div>
        <div>
          <label className={labelClass}>密码（可选）</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={downloader ? "出于安全，请重新填写" : ""}
            autoComplete="new-password"
            className={inputClass}
          />
        </div>
      </div>

      <div>
        <label className={labelClass}>默认保存目录（可选）</label>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setPickerTarget("save")}
            className="flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-left transition-colors hover:border-[var(--accent)]/50"
          >
            <FolderIcon className="size-4 shrink-0 text-[var(--accent)]/80" />
            {savePath ? (
              <span dir="rtl" className="min-w-0 flex-1 truncate font-mono text-ui text-[var(--text)]">
                {"‎" + savePath + "‎"}
              </span>
            ) : (
              <span className="text-ui text-[var(--text-faint)]">浏览服务器目录并选择…</span>
            )}
          </button>
          {savePath && (
            <button
              type="button"
              onClick={() => setSavePath("")}
              aria-label="清除默认保存目录"
              className="glass-row !w-auto shrink-0 p-2"
            >
              <XIcon className="size-4" />
            </button>
          )}
        </div>
        <p className="mt-1.5 text-caption leading-relaxed text-[var(--text-faint)]">
          提交下载时文件的保存位置，从 movieclaw 能看到的目录里选择；下载器看到的路径不同时，
          配合下方「路径映射」翻译。留空则使用下载器自己设置的默认下载目录
          ——下载器与 movieclaw 没有共享目录的部署请留空。
        </p>
      </div>

      {/* 路径映射：默认折叠，仅跨容器/跨主机部署需要展开配置 */}
      <div>
        <button
          type="button"
          onClick={() => setMappingsOpen((v) => !v)}
          className="flex items-center gap-1.5 text-sub font-medium text-[var(--text-muted)] transition-colors hover:text-[var(--text)]"
        >
          <span
            className="inline-block transition-transform"
            style={{ transform: mappingsOpen ? "rotate(90deg)" : undefined }}
          >
            ›
          </span>
          路径映射（可选）
          {!mappingsOpen && mappings.length > 0 && (
            <span className="text-[var(--text-faint)]">已配置 {mappings.length} 条</span>
          )}
        </button>
        {mappingsOpen && (
          <div className="mt-2.5 space-y-2.5">
            <p className="text-caption leading-relaxed text-[var(--text-faint)]">
              movieclaw 与下载器不在同一容器/主机、同一块盘两边路径不同时才需要：
              提交下载前会把保存目录按前缀翻译成下载器视角。例如 movieclaw 看到的下载区是
              <code className="mx-0.5 font-mono">/data/downloads</code>、下载器容器里是
              <code className="mx-0.5 font-mono">/downloads</code>，则添加一条对照。留空表示两边路径一致。
              注意：配了映射后，所有下载保存目录（含媒体库目录）都必须被某条映射覆盖，
              否则会拒绝投递以防下载进下载器容器内的孤立路径；下载器能以相同路径直达的目录，
              添加一条两边相同的映射即可。
            </p>
            {suggestMapping && (
              <p className="rounded-lg bg-[var(--accent-soft)] px-3 py-2 text-caption leading-relaxed text-[var(--accent)]">
                已按体检建议预填映射的本机侧 {suggestMapping}——右侧填下载器视角的对应路径；
                下载器可直达同名路径时，点中间的 → 把左侧复制过去即可。
              </p>
            )}
            {mappings.map((mapping, index) => (
              <div key={index} className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setPickerTarget(index)}
                  title="movieclaw 上的路径（浏览选择）"
                  className="flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-left transition-colors hover:border-[var(--accent)]/50"
                >
                  <FolderIcon className="size-4 shrink-0 text-[var(--accent)]/80" />
                  {mapping.local ? (
                    <span dir="rtl" className="min-w-0 flex-1 truncate font-mono text-ui text-[var(--text)]">
                      {"‎" + mapping.local + "‎"}
                    </span>
                  ) : (
                    <span className="truncate text-ui text-[var(--text-faint)]">movieclaw 上的路径…</span>
                  )}
                </button>
                {/* 箭头即按钮：两边路径一致时点一下把左侧复制到右侧，省去手动输入 */}
                <button
                  type="button"
                  onClick={() => setMapping(index, { remote: mapping.local })}
                  disabled={!mapping.local}
                  title="将左侧路径复制到右侧（两边路径一致时使用）"
                  aria-label="将左侧路径复制到右侧"
                  className="shrink-0 rounded-lg px-1.5 py-1 text-[var(--text-faint)] transition-colors enabled:hover:bg-white/[0.06] enabled:hover:text-[var(--accent)] disabled:cursor-default"
                >
                  →
                </button>
                <input
                  type="text"
                  value={mapping.remote}
                  onChange={(e) => setMapping(index, { remote: e.target.value })}
                  placeholder="下载器上的路径，如 /downloads"
                  autoComplete="off"
                  className={`${inputClass} flex-1 font-mono`}
                />
                <button
                  type="button"
                  onClick={() => setMappings((prev) => prev.filter((_, i) => i !== index))}
                  aria-label="删除这条映射"
                  className="glass-row !w-auto shrink-0 p-2"
                >
                  <XIcon className="size-4" />
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() => setMappings((prev) => [...prev, { local: "", remote: "" }])}
              className="btn-glass flex items-center gap-1 px-3 py-1.5 text-sub font-medium"
            >
              <PlusIcon className="size-3.5" />
              添加映射
            </button>
            {!mappingsComplete ? (
              <p className="text-caption text-[#ffb46b]">
                每条映射两边都要填：左边浏览选择，右边填下载器上以 / 开头的绝对路径；不需要的行请删除。
              </p>
            ) : !mappingsUnique ? (
              <p className="text-caption text-[#ffb46b]">
                映射的路径不能重复：同一 movieclaw 路径或同一下载器路径只能出现一次，请修改或删除重复的行。
              </p>
            ) : null}
          </div>
        )}
      </div>

      <div className="flex items-center justify-end gap-3 pt-1">
        <button type="button" onClick={onCancel} className="btn-glass px-3.5 py-2 text-ui font-medium">
          取消
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={busy || !canSubmit}
          className="btn-accent rounded-full px-4.5 py-2 text-ui font-semibold disabled:opacity-40"
        >
          {busy ? "保存中…" : "保存"}
        </button>
      </div>

      <DirectoryPicker
        open={pickerTarget !== null}
        initialPath={
          pickerTarget === "save"
            ? savePath || undefined
            : typeof pickerTarget === "number"
              ? mappings[pickerTarget]?.local || undefined
              : undefined
        }
        onClose={() => setPickerTarget(null)}
        onSelect={(path) => {
          if (pickerTarget === "save") setSavePath(path);
          else if (typeof pickerTarget === "number") setMapping(pickerTarget, { local: path });
          setPickerTarget(null);
        }}
      />
    </div>
  );
}
