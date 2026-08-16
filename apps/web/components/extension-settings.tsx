"use client";

import { useCallback, useEffect, useState } from "react";

import { useConfirm } from "@/components/feedback";
import { CheckIcon, CopyIcon, DownloadIcon, PuzzleIcon, ShieldIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import { EXTENSION_ZIP_URL, useExtensionInstalled } from "@/lib/extension-install";
import {
  type SyncTokenView,
  generateSyncToken,
  getSyncToken,
  revokeSyncToken,
} from "@/lib/api/extension";
import { formatDateTime } from "@/lib/time";

/**
 * 浏览器插件卡片：嵌在「资源站点」分区底部（插件是站点 Cookie 同步的配套工具，
 * 不单设分区）。
 * - 安装引导：自动检测是否已安装（见 lib/extension-install.ts），未安装时提供 zip
 *   下载与加载步骤（Chrome 政策不允许商店外插件一键静默安装，下载后需在
 *   chrome://extensions 手动加载，卡片把步骤讲清楚）；
 * - 同步令牌：设完即用、极少回访的配置，不占第一屏——收进「同步令牌」按钮的
 *   弹窗里管理（生成 / 查看 / 复制 / 重新生成 / 关闭）。
 * 各站点的同步与验证状态直接看上方站点列表，不在这里重复展示。
 */
export function ExtensionCard() {
  const { installed } = useExtensionInstalled();
  const [tokenOpen, setTokenOpen] = useState(false);

  const badge =
    installed === null
      ? { label: "检测中…", color: "#c0c4cc" }
      : installed
        ? { label: "已安装", color: "var(--ok)" }
        : { label: "未检测到", color: "#c0c4cc" };

  return (
    <section className="css-glass !rounded-2xl p-6">
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-start gap-3.5">
          <span className="icon-chip size-10 shrink-0 !rounded-xl">
            <PuzzleIcon className="size-5" />
          </span>
          <div>
            <h2 className="text-body-lg font-semibold">MovieClaw 浏览器插件</h2>
            <p className="mt-0.5 text-sub leading-5 text-[var(--text-muted)]">
              在站点页面一键读取登录 Cookie（含 httpOnly）并同步到本服务，免去手动复制粘贴，
              还能随 Cookie 变化自动保持最新。
            </p>
          </div>
        </div>
        <span className="flex shrink-0 items-center gap-1.5 text-sub text-[var(--text-muted)]">
          <span
            className={`size-2 rounded-full ${installed === null ? "animate-pulse" : ""}`}
            style={{ background: badge.color }}
          />
          {badge.label}
        </span>
      </div>

      {installed ? (
        <p className="mt-4 rounded-xl bg-white/[0.03] px-4 py-3 text-body text-[var(--text-muted)]">
          <CheckIcon className="mr-1.5 inline size-4 text-[var(--ok)]" />
          插件已就绪。打开支持的站点页面，点击浏览器工具栏的 MovieClaw
          图标即可同步该站 Cookie；首次使用请先点下方「同步令牌」生成并填入插件。
        </p>
      ) : (
        <ol className="mt-4 space-y-2 rounded-xl bg-white/[0.03] px-4 py-3.5 text-ui leading-6 text-[var(--text-muted)]">
          <li>
            <b className="text-[var(--text)]">1.</b> 点击下方按钮下载插件包，解压得到{" "}
            <code className="rounded bg-white/[0.06] px-1 font-mono text-sub">chrome-mv3</code> 文件夹。
          </li>
          <li>
            <b className="text-[var(--text)]">2.</b> 浏览器打开{" "}
            <code className="rounded bg-white/[0.06] px-1 font-mono text-sub">chrome://extensions</code>
            ，右上角开启「开发者模式」。
          </li>
          <li>
            <b className="text-[var(--text)]">3.</b>{" "}
            点「加载已解压的扩展程序」选择该文件夹，切回本页即自动识别为「已安装」。
          </li>
        </ol>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-3">
        {!installed && (
          <a
            href={EXTENSION_ZIP_URL}
            download
            className="btn-accent flex items-center gap-1.5 rounded-full px-4 py-2 text-sub font-semibold"
          >
            <DownloadIcon className="size-4" />
            下载插件包
          </a>
        )}
        <button
          type="button"
          onClick={() => setTokenOpen(true)}
          className="btn-glass flex items-center gap-1.5 px-4 py-2 text-sub font-medium"
        >
          <ShieldIcon className="size-4" />
          同步令牌
        </button>
        {!installed && (
          <p className="text-caption text-[var(--text-faint)]">
            支持 Chrome / Edge 等 Chromium 内核浏览器；安装检测同样仅对 Chromium 生效。
          </p>
        )}
      </div>

      <TokenModal open={tokenOpen} onClose={() => setTokenOpen(false)} />
    </section>
  );
}

/* —— 子组件 —— */

/**
 * 同步令牌管理弹窗：打开时才拉取令牌（低频配置不随页面加载）。
 * 生成 / 查看 / 复制 / 重新生成 / 关闭同步都收在这里。
 */
function TokenModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const confirm = useConfirm();
  const [token, setToken] = useState<SyncTokenView | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setToken(await getSyncToken());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  async function onGenerate() {
    if (
      token?.enabled &&
      !(await confirm({
        title: "重新生成同步令牌？",
        description: "重新生成将使旧令牌立即失效，已配置的插件需要更新令牌。",
        confirmLabel: "重新生成",
        tone: "danger",
      }))
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setToken(await generateSyncToken());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function onRevoke() {
    if (
      !(await confirm({
        title: "关闭 Cookie 同步？",
        description: "关闭同步将撤销令牌，所有插件都将无法再同步。",
        confirmLabel: "关闭同步",
        tone: "danger",
      }))
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setToken(await revokeSyncToken());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} label="同步令牌" width="lg">
      <div className="space-y-4 p-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-title font-bold text-[var(--text)]">同步令牌</h2>
            <p className="mt-1 text-sub leading-5 text-[var(--text-muted)]">
              在浏览器插件的设置里填入此令牌，即可把站点 Cookie 同步到本服务。令牌长期有效，除非你重新生成。
            </p>
          </div>
          <StatusDot on={Boolean(token?.enabled)} />
        </div>

        {error && (
          <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
            {error}
          </div>
        )}

        {loading ? (
          <div className="h-11 animate-pulse rounded-xl bg-white/[0.04]" />
        ) : token?.enabled ? (
          <TokenRow token={token.token ?? ""} createdAt={token.created_at} />
        ) : (
          <p className="rounded-xl bg-white/[0.03] px-4 py-3 text-body text-[var(--text-muted)]">
            尚未启用同步。点击下方「生成令牌」创建一个。
          </p>
        )}

        <div className="flex flex-wrap gap-3 pt-1">
          <button
            type="button"
            onClick={onGenerate}
            disabled={busy || loading}
            className="btn-accent rounded-full px-4 py-2 text-sub font-semibold disabled:opacity-60"
          >
            {token?.enabled ? "重新生成" : "生成令牌"}
          </button>
          {token?.enabled && (
            <button
              type="button"
              onClick={onRevoke}
              disabled={busy || loading}
              className="btn-glass px-4 py-2 text-sub font-medium !text-[var(--danger)] hover:!border-[#ff6b6b]/40"
            >
              关闭同步
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            className="btn-glass ml-auto px-4 py-2 text-sub font-medium"
          >
            完成
          </button>
        </div>
      </div>
    </Modal>
  );
}

function StatusDot({ on }: { on: boolean }) {
  return (
    <span className="flex shrink-0 items-center gap-1.5 text-sub text-[var(--text-muted)]">
      <span
        className="size-2 rounded-full"
        style={{ background: on ? "var(--ok)" : "#c0c4cc" }}
      />
      {on ? "已启用" : "未启用"}
    </span>
  );
}

function TokenRow({ token, createdAt }: { token: string; createdAt: string | null }) {
  const [revealed, setRevealed] = useState(false);
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 忽略：某些环境无剪贴板权限，用户可手动选择复制 */
    }
  }

  const display = revealed ? token : "•".repeat(Math.min(token.length, 28));

  return (
    <div>
      <div className="flex items-center gap-2 rounded-xl bg-white/[0.04] px-3 py-2.5">
        <code className="min-w-0 flex-1 truncate font-mono text-ui text-[var(--text)]">
          {display}
        </code>
        <button
          type="button"
          onClick={() => setRevealed((v) => !v)}
          className="btn-glass shrink-0 px-2.5 py-1 text-sub font-medium"
        >
          {revealed ? "隐藏" : "显示"}
        </button>
        <button
          type="button"
          onClick={copy}
          aria-label="复制令牌"
          className="btn-glass shrink-0 px-2 py-1 text-sub font-medium"
        >
          {copied ? <CheckIcon className="size-4 text-[var(--ok)]" /> : <CopyIcon className="size-4" />}
        </button>
      </div>
      {createdAt && (
        <p className="mt-1.5 text-caption text-[var(--text-faint)]">
          生成于 {formatDateTime(createdAt)}
        </p>
      )}
    </div>
  );
}
