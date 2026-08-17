"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";

import { useConfirm } from "@/components/feedback";
import { MoreIcon, PlusIcon, ServerIcon } from "@/components/icons";
import { ExtensionCard } from "@/components/extension-settings";
import { SearchSection } from "@/components/search-settings";
import { useBackdrop } from "@/lib/backdrop";
import type { ConfiguredSite, SiteAuthType, SiteStatus } from "@/lib/api/extension";
import {
  type AuthTypeRequirement,
  type CatalogItem,
  type SiteBoostStats,
  type SiteConfigPayload,
  type SiteSyncStats,
  configureSite,
  deleteSite,
  listConfiguredSites,
  listSiteBoostStats,
  listSiteCatalog,
  listSiteSyncStats,
  reverifySite,
  setSiteEnabled,
  setSiteProtection,
  setSiteRatioBoost,
  updateSite,
} from "@/lib/api/sites";
import { formatBytes, formatCompact, formatDuration, formatRatio } from "@/lib/format";
import { formatDateTime, formatRelativeTime } from "@/lib/time";
import { useVisiblePolling } from "@/lib/use-visible-polling";
import { LiquidGlassButton, LiquidGlassInput } from "@/vendor/liquid-glass";

/** 站点验证状态 → 展示文案与颜色 */
const STATUS_META: Record<SiteStatus, { label: string; color: string }> = {
  active: { label: "已验证", color: "var(--ok)" },
  verifying: { label: "验证中", color: "var(--info)" },
  pending: { label: "待验证", color: "#c0c4cc" },
  failed: { label: "验证失败", color: "var(--danger)" },
};

/** 授权类型 → 中文名 */
const AUTH_TYPE_LABEL: Record<SiteAuthType, string> = {
  cookie: "Cookie",
  apikey: "API 密钥",
  credential: "用户名密码",
};

/** 表单字段名 → 中文标签 & 输入类型 */
const FIELD_META: Record<string, { label: string; kind: "text" | "password" | "textarea" }> = {
  cookie: { label: "Cookie 字符串", kind: "textarea" },
  api_key: { label: "API 密钥", kind: "password" },
  username: { label: "用户名", kind: "text" },
  password: { label: "密码", kind: "password" },
};

/** 需要轮询验证进度的中间态 */
const IN_PROGRESS: SiteStatus[] = ["pending", "verifying"];

/** 本分区的两档内容（见 SiteConfigSection 顶部的胶囊标签） */
const TABS = [
  { id: "sites", label: "站点接入" },
  { id: "search", label: "搜索分类" },
] as const;

type SiteTab = (typeof TABS)[number]["id"];

/** 目录中缺失该站点时的兜底展示（例如站点已从系统下架但仍有历史配置） */
function fallbackItem(siteId: string): CatalogItem {
  return { site_id: siteId, display_name: siteId, base_url: "", supported_auth_types: [] };
}

/**
 * 「资源站点」设置分区，两档胶囊标签：
 *   - 站点接入：已配置站点列表 + 添加入口 + 浏览器插件卡（Cookie 同步工具，
 *     与站点授权表单同屏才有意义，不单独成档）；
 *   - 搜索分类：搜索面板分类栏的配置（见 search-settings.tsx）——分类筛的正是
 *     这里接入的站点，故同页，但使用节奏不同（接入一次性、分类常调），分档。
 *
 * 交互取舍：站点目录可能很多，全部平铺不现实。因此主列表**只展示用户已配置的站点**，
 * 通过「添加站点」入口从目录（GET /sites/catalog）里按需挑选未配置的站点再填表。
 *
 * 数据来自两个接口：
 * - GET /sites/catalog —— 系统支持的可配置站点（含每种授权类型的必填字段）。
 * - GET /sites         —— 用户已配置的站点及其验证状态。
 *
 * 配置/更新后后端异步验证，前端对中间态（pending/verifying）轮询刷新，直到 active / failed。
 */
export function SiteConfigSection() {
  const [tab, setTab] = useState<SiteTab>("sites");
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  const [configured, setConfigured] = useState<ConfiguredSite[]>([]);
  // 各站点的种子缓存统计（定时同步任务维护），key 为 site_id；从未同步过的站点没有条目
  const [syncStats, setSyncStats] = useState<Record<string, SiteSyncStats>>({});
  // 各站点的刷流运行统计；从未刷流且未开启刷流的站点没有条目
  const [boostStats, setBoostStats] = useState<Record<string, SiteBoostStats>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // 是否展开「添加站点」面板
  const [adding, setAdding] = useState(false);
  // 当前正在编辑表单的已配置站点 site_id（null 表示没有展开的编辑表单）
  const [editing, setEditing] = useState<string | null>(null);

  const catalogMap = useMemo(() => new Map(catalog.map((c) => [c.site_id, c])), [catalog]);

  // 已配置集合，供"添加"面板过滤掉已接入的站点
  const configuredIds = useMemo(
    () => new Set(configured.map((s) => s.site_id)),
    [configured],
  );
  const availableItems = useMemo(
    () => catalog.filter((c) => !configuredIds.has(c.site_id)),
    [catalog, configuredIds],
  );

  // 全部站点的种子缓存总量——头部一句话让用户对"本地存了多少"有整体感知
  const totalCached = useMemo(
    () => Object.values(syncStats).reduce((sum, s) => sum + s.torrent_count, 0),
    [syncStats],
  );

  const load = useCallback(async () => {
    setError(null);
    try {
      const [cat, cfg, stats, boost] = await Promise.all([
        listSiteCatalog(),
        listConfiguredSites(),
        listSiteSyncStats(),
        listSiteBoostStats(),
      ]);
      setCatalog(cat);
      setConfigured(cfg);
      setSyncStats(stats);
      setBoostStats(boost);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 有站点处于 pending/verifying 时轮询刷新，直到全部落定（页面隐藏时暂停）
  const hasInProgress = configured.some((s) => IN_PROGRESS.includes(s.status));
  useVisiblePolling(
    () => {
      void listConfiguredSites()
        .then(setConfigured)
        .catch(() => {
          /* 轮询失败静默重试，不打断页面 */
        });
    },
    hasInProgress ? 2500 : null,
  );

  // 有站点开着刷流时轻轮询运行统计（引擎 5 分钟一个 tick，30 秒刷新足够跟上），
  // 让「已用 / 累计上传」在页面停留期间自己长，不需要用户手动刷新
  const anyBoosting = configured.some((s) => s.boost_enabled);
  useVisiblePolling(
    () => {
      void listSiteBoostStats()
        .then(setBoostStats)
        .catch(() => {
          /* 轮询失败静默重试，不打断页面 */
        });
    },
    anyBoosting ? 30000 : null,
  );

  // 原地替换已有站点、新站点追加到末尾。
  // 注意：切换启用开关等操作也会走这里，必须保持列表顺序不变，否则被操作的站点会跳位，体验割裂。
  const upsertConfigured = useCallback((next: ConfiguredSite) => {
    setConfigured((prev) => {
      const idx = prev.findIndex((s) => s.site_id === next.site_id);
      if (idx === -1) return [...prev, next];
      const copy = [...prev];
      copy[idx] = next;
      return copy;
    });
  }, []);

  return (
    <div className="space-y-5">
      {/* 胶囊标签切换：与「外观」分区（背景图 / 界面质感）同一交互语言。
          只分两档——插件是站点 Cookie 的填写工具，必须与站点表单同屏，
          所以跟着「站点接入」；搜索分类的使用节奏不同（接入是一次性、
          分类是常调），单独一档，也免得两块内容挤成一条长滚动。 */}
      <div className="flex gap-1.5">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            aria-pressed={t.id === tab}
            onClick={() => setTab(t.id)}
            className={`rounded-full px-3.5 py-1.5 text-sub font-medium transition-colors ${
              t.id === tab
                ? "bg-white/[0.14] text-white"
                : "text-[var(--text-muted)] hover:bg-white/[0.07] hover:text-[var(--text)]"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "search" ? (
        <SearchSection />
      ) : (
        <>
          {error && (
            <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
              {error}
            </div>
          )}

          <div className="flex items-center justify-between gap-4">
            <p className="text-sub text-[var(--text-muted)]">
              {loading
                ? "加载中…"
                : `已接入 ${configured.length} 个站点 · 本地累计缓存 ${totalCached.toLocaleString("zh-CN")} 条种子，保存后系统会自动验证有效性。`}
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
              {/* 主操作：亮银胶囊，与次级玻璃按钮拉开主次 */}
              <button
                type="button"
                onClick={() => setAdding((v) => !v)}
                disabled={loading}
                className="btn-accent flex items-center gap-1 rounded-full py-1.5 pl-2.5 pr-3.5 text-sub font-semibold disabled:opacity-60"
              >
                <PlusIcon className="size-4" />
                添加站点
              </button>
            </div>
          </div>

          {/* 「添加站点」面板：从目录里挑选未配置的站点 */}
          {adding && (
            <AddSitePanel
              available={availableItems}
              onCreated={(site) => {
                upsertConfigured(site);
                setAdding(false);
              }}
              onCancel={() => setAdding(false)}
              onError={setError}
            />
          )}

          {/* 已配置站点列表 */}
          <div className="space-y-2.5">
            {loading ? (
              <>
                <div className="h-[72px] animate-pulse rounded-xl bg-white/[0.04]" />
                <div className="h-[72px] animate-pulse rounded-xl bg-white/[0.04]" />
              </>
            ) : configured.length === 0 ? (
              <div className="css-glass flex flex-col items-center gap-3 !rounded-2xl px-6 py-12 text-center">
                <span className="icon-chip size-12 !rounded-2xl">
                  <ServerIcon className="size-6" />
                </span>
                <div>
                  <p className="text-body font-medium text-[var(--text)]">还没有配置任何站点</p>
                  <p className="mt-1 text-sub text-[var(--text-muted)]">
                    点击右上角「添加站点」开始接入。
                  </p>
                </div>
              </div>
            ) : (
              configured.map((site) => (
                <SiteCard
                  key={site.site_id}
                  item={catalogMap.get(site.site_id) ?? fallbackItem(site.site_id)}
                  site={site}
                  stats={syncStats[site.site_id]}
                  boost={boostStats[site.site_id]}
                  expanded={editing === site.site_id}
                  onToggleForm={(open) => setEditing(open ? site.site_id : null)}
                  onChanged={upsertConfigured}
                  onDeleted={(siteId) => {
                    setConfigured((prev) => prev.filter((s) => s.site_id !== siteId));
                    setEditing((cur) => (cur === siteId ? null : cur));
                  }}
                  onError={setError}
                />
              ))
            )}
          </div>

          {/* 浏览器插件：站点 Cookie 同步的配套工具，安装引导与同步令牌都在这张卡片组里 */}
          <ExtensionCard />
        </>
      )}
    </div>
  );
}

/* —— 添加站点：先选站点（带搜索），再填授权表单 —— */

interface AddSitePanelProps {
  available: CatalogItem[];
  onCreated: (site: ConfiguredSite) => void;
  onCancel: () => void;
  onError: (message: string) => void;
}

function AddSitePanel({ available, onCreated, onCancel, onError }: AddSitePanelProps) {
  const { backdrop } = useBackdrop();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<CatalogItem | null>(null);
  const [busy, setBusy] = useState(false);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return available;
    return available.filter(
      (c) =>
        c.display_name.toLowerCase().includes(q) ||
        c.site_id.toLowerCase().includes(q) ||
        c.base_url.toLowerCase().includes(q),
    );
  }, [available, query]);

  // 已选定站点 → 展示授权表单
  if (selected) {
    return (
      <div className="css-glass !rounded-xl p-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="truncate text-body font-semibold text-[var(--text)]">
              {selected.display_name}
            </p>
            <p className="truncate text-caption text-[var(--text-faint)]">{selected.base_url}</p>
          </div>
          <button
            type="button"
            onClick={() => setSelected(null)}
            disabled={busy}
            className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium"
          >
            重新选择
          </button>
        </div>
        <SiteForm
          item={selected}
          site={null}
          busy={busy}
          onSubmit={async (payload) => {
            setBusy(true);
            try {
              onCreated(await configureSite(selected.site_id, payload));
            } catch (e) {
              onError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        />
      </div>
    );
  }

  // 未选定 → 搜索 + 站点列表
  return (
    <div className="css-glass !rounded-xl p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        {/* 液态玻璃搜索框：与全站其余玻璃组件共享同一背景大图（useBackdrop），折射下方内容 */}
        <LiquidGlassInput
          backgroundImage={backdrop}
          variant="frosted"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索站点名称 / 地址"
          autoFocus
        />
        <button
          type="button"
          onClick={onCancel}
          className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium"
        >
          取消
        </button>
      </div>

      <div className="scroll-thin max-h-64 space-y-1 overflow-y-auto">
        {available.length === 0 ? (
          <p className="px-2 py-6 text-center text-body text-[var(--text-muted)]">
            所有支持的站点都已配置。
          </p>
        ) : filtered.length === 0 ? (
          <p className="px-2 py-6 text-center text-body text-[var(--text-muted)]">
            没有匹配「{query}」的站点。
          </p>
        ) : (
          filtered.map((item) => (
            <button
              key={item.site_id}
              type="button"
              onClick={() => setSelected(item)}
              className="glass-row nav-item w-full items-center justify-between gap-3 px-3 py-2.5 text-left"
            >
              <span className="min-w-0">
                <span className="block truncate text-ui font-medium text-[var(--text)]">
                  {item.display_name}
                </span>
                <span className="block truncate text-caption text-[var(--text-faint)]">
                  {item.base_url}
                </span>
              </span>
              <span className="shrink-0 text-caption text-[var(--text-muted)]">
                {item.supported_auth_types.map((a) => AUTH_TYPE_LABEL[a.auth_type]).join(" / ")}
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

/* —— 单个已配置站点卡片：折叠态展示状态 + 操作，展开态是授权表单 —— */

interface SiteCardProps {
  item: CatalogItem;
  site: ConfiguredSite;
  /** 该站点的种子缓存统计；定时同步任务从未跑过该站时为 undefined */
  stats?: SiteSyncStats;
  /** 该站点的刷流运行统计；从未刷流且未开启时为 undefined */
  boost?: SiteBoostStats;
  expanded: boolean;
  onToggleForm: (open: boolean) => void;
  onChanged: (site: ConfiguredSite) => void;
  onDeleted: (siteId: string) => void;
  onError: (message: string) => void;
}

function SiteCard({
  item,
  site,
  stats,
  boost,
  expanded,
  onToggleForm,
  onChanged,
  onDeleted,
  onError,
}: SiteCardProps) {
  const { backdrop } = useBackdrop();
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const meta = STATUS_META[site.status];

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

  return (
    <div className="css-glass !rounded-xl">
      {/* 顶部行：首字母徽标 + 站点信息 + 状态 + 操作 */}
      <div className="flex items-center gap-3.5 p-4">
        {item.base_url ? (
          <a
            href={item.base_url}
            target="_blank"
            rel="noreferrer"
            aria-label={`在新窗口打开 ${item.display_name}`}
            title="在新窗口打开站点"
            className="icon-chip size-10 cursor-pointer !rounded-xl text-body font-semibold outline-none hover:bg-white/[0.1] hover:text-[var(--text)] focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]"
          >
            {item.display_name.charAt(0).toUpperCase()}
          </a>
        ) : (
          <span className="icon-chip size-10 !rounded-xl text-body font-semibold">
            {item.display_name.charAt(0).toUpperCase()}
          </span>
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            {item.base_url ? (
              <a
                href={item.base_url}
                target="_blank"
                rel="noreferrer"
                title="在新窗口打开站点"
                className="truncate text-body font-semibold text-[var(--text)] underline decoration-transparent underline-offset-4 transition-colors hover:cursor-pointer hover:text-white/80 hover:decoration-white/50 focus-visible:outline-none focus-visible:decoration-white/50"
              >
                {item.display_name}
              </a>
            ) : (
              <p className="truncate text-body font-semibold text-[var(--text)]">{item.display_name}</p>
            )}
            <span
              className="flex shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 text-caption font-medium"
              style={{ background: `color-mix(in oklab, ${meta.color} 12%, transparent)`, color: meta.color }}
            >
              <span className="size-1.5 rounded-full" style={{ background: meta.color }} />
              {meta.label}
            </span>
            {site.protected && (
              <span
                className="shrink-0 rounded-full px-2 py-0.5 text-caption font-medium"
                style={{
                  background: "color-mix(in oklab, var(--info) 12%, transparent)",
                  color: "var(--info)",
                }}
                title="保护中：订阅不会自动从该站拉种，手动搜索下载不受影响"
              >
                保护中
              </span>
            )}
          </div>
          <p className="mt-0.5 truncate text-caption text-[var(--text-faint)]">
            {site.status === "failed" && site.last_error
              ? site.last_error
              : `${AUTH_TYPE_LABEL[site.auth_type]} · 上次检查 ${formatRelativeTime(site.last_checked_at)}`}
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {/* 启用开关：真实 WebGL 液态玻璃开关（LiquidGlassButton 本质就是 toggle）。
              受控于 site.enabled——切换后先请求后端、拿到结果再回写；请求期间 disabled 防抖，
              失败时 site.enabled 不变，受控 prop 会把开关动画回弹到原位。
              用 !w-auto/!p-0/!bg-transparent/!gap-0 剥掉组件默认的整行外壳，只留 64×32 开关本体。 */}
          <LiquidGlassButton
            backgroundImage={backdrop}
            variant="dark"
            checked={site.enabled}
            disabled={busy}
            aria-label={site.enabled ? "已启用，点击停用" : "已停用，点击启用"}
            onCheckedChange={(enabled) =>
              void guard(async () => onChanged(await setSiteEnabled(site.site_id, enabled)))
            }
            className="!min-h-0 !w-auto !gap-0 !bg-transparent !p-0"
          >
            <span className="sr-only">{site.enabled ? "已启用" : "已停用"}</span>
          </LiquidGlassButton>
          {/* 编辑/验证/删除收进折叠菜单，顶部行只留启用开关，避免操作平铺显得杂乱 */}
          <SiteActionsMenu
            expanded={expanded}
            busy={busy}
            canReverify={!IN_PROGRESS.includes(site.status)}
            onEdit={() => onToggleForm(!expanded)}
            onReverify={() => void guard(async () => onChanged(await reverifySite(site.site_id)))}
            onDelete={() =>
              void guard(async () => {
                if (
                  !(await confirm({
                    title: `删除「${item.display_name}」的配置？`,
                    description: "该站点将不再参与搜索与订阅投递，可随时重新接入。",
                    confirmLabel: "删除",
                    tone: "danger",
                  }))
                ) {
                  return;
                }
                await deleteSite(item.site_id);
                onDeleted(item.site_id);
              })
            }
          />
        </div>
      </div>

      {/* 用户资料统计带：验证成功时抓取（见后端 verify_site），验证失败保留上一次
          成功的旧快照继续展示 —— 数据新鲜度通过 tooltip 的「资料更新于」体现 */}
      {site.profile && (
        <div
          className="flex flex-wrap gap-x-7 gap-y-2 border-t border-white/[0.06] px-4 py-3"
          title={`资料更新于 ${formatRelativeTime(site.profile.fetched_at)}`}
        >
          <ProfileStat label="账号" value={site.profile.username} />
          {site.profile.user_class && <ProfileStat label="等级" value={site.profile.user_class} />}
          <ProfileStat label="上传量" value={formatBytes(site.profile.uploaded_bytes)} />
          <ProfileStat label="下载量" value={formatBytes(site.profile.downloaded_bytes)} />
          <ProfileStat label="分享率" value={formatRatio(site.profile.ratio)} />
          {site.profile.bonus != null && (
            <ProfileStat label="魔力" value={formatCompact(site.profile.bonus)} />
          )}
          <ProfileStat label="做种" value={String(site.profile.seeding_count)} />
        </div>
      )}

      {/* 种子缓存统计带：定时同步任务维护的本地缓存感知——存了多少、上次/下次
          什么时候同步。从未同步过的站点没有这条带（stats 缺失） */}
      {stats && (
        <div
          className="flex flex-wrap gap-x-7 gap-y-2 border-t border-white/[0.06] px-4 py-3"
          title={`开始跟踪于 ${formatDateTime(stats.tracking_since)}`}
        >
          <ProfileStat label="已缓存种子" value={stats.torrent_count.toLocaleString("zh-CN")} />
          <ProfileStat label="上次同步" value={formatRelativeTime(stats.last_sync_at)} />
          <ProfileStat label="下次同步" value={nextSyncLabel(stats.next_sync_at)} />
          {stats.sync_interval_seconds != null && (
            <ProfileStat label="同步间隔" value={formatDuration(stats.sync_interval_seconds)} />
          )}
          {stats.last_new_count != null && (
            <ProfileStat label="上次新增" value={String(stats.last_new_count)} />
          )}
        </div>
      )}
      {stats?.last_error && (
        <p className="border-t border-white/[0.06] px-4 py-2.5 text-caption text-[#ff6b6b]">
          上次同步失败：{stats.last_error}
        </p>
      )}

      {/* 保护与刷流带：新站养号的两个机制（docs/design/site-protection-ratio-boost.md）。
          开关即时提交（与启用开关同款节奏），预算失焦即存。 */}
      <SiteGuardBand site={site} boost={boost} onChanged={onChanged} onError={onError} />

      {/* 展开态：授权表单 */}
      {expanded && (
        <div className="border-t border-white/[0.06] p-4">
          <SiteForm
            item={item}
            site={site}
            busy={busy}
            onSubmit={(payload) =>
              guard(async () => {
                onChanged(await updateSite(item.site_id, payload));
                onToggleForm(false);
              })
            }
          />
        </div>
      )}
    </div>
  );
}

/* —— 保护与刷流带：站点保护开关 + 自动刷分享率（开关 / 预算 / 运行统计） —— */

const GIB = 1024 ** 3;

interface SiteGuardBandProps {
  site: ConfiguredSite;
  boost?: SiteBoostStats;
  onChanged: (site: ConfiguredSite) => void;
  onError: (message: string) => void;
}

function SiteGuardBand({ site, boost, onChanged, onError }: SiteGuardBandProps) {
  const { backdrop } = useBackdrop();
  const [busy, setBusy] = useState(false);
  // 预算输入（GiB 为单位）：失焦即存；site 变化（别处保存成功）时回同步
  const [budgetGib, setBudgetGib] = useState(() => String(Math.round(site.boost_budget_bytes / GIB)));
  useEffect(() => {
    setBudgetGib(String(Math.round(site.boost_budget_bytes / GIB)));
  }, [site.boost_budget_bytes]);

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

  function commitBudget() {
    const gib = Math.round(Number(budgetGib));
    if (!Number.isFinite(gib) || gib < 1) {
      // 非法输入回弹为当前生效值，不发请求
      setBudgetGib(String(Math.round(site.boost_budget_bytes / GIB)));
      return;
    }
    const bytes = gib * GIB;
    if (bytes === site.boost_budget_bytes) return;
    void guard(async () =>
      onChanged(await setSiteRatioBoost(site.site_id, site.boost_enabled, bytes)),
    );
  }

  return (
    <div className="space-y-3 border-t border-white/[0.06] px-4 py-3">
      {/* 站点保护开关 */}
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <p className="text-ui font-medium text-[var(--text)]">站点保护</p>
          <p className="mt-0.5 text-caption text-[var(--text-faint)]">
            打开后订阅不会自动从该站拉种（下载量归零），手动搜索和下载不受影响。适合分享率还没养起来的新站。
          </p>
        </div>
        <LiquidGlassButton
          backgroundImage={backdrop}
          variant="dark"
          checked={site.protected}
          disabled={busy}
          aria-label={site.protected ? "保护中，点击取消保护" : "未保护，点击开启保护"}
          onCheckedChange={(next) =>
            void guard(async () => onChanged(await setSiteProtection(site.site_id, next)))
          }
          className="!min-h-0 !w-auto shrink-0 !gap-0 !bg-transparent !p-0"
        >
          <span className="sr-only">{site.protected ? "保护中" : "未保护"}</span>
        </LiquidGlassButton>
      </div>

      {/* 自动刷分享率开关 */}
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <p className="text-ui font-medium text-[var(--text)]">自动刷分享率</p>
          <p className="mt-0.5 text-caption text-[var(--text-faint)]">
            自动抢该站新发布的免费种做种提升分享率；在预算内自动汰换上传效率低的，保留期 72 小时内绝不删。
          </p>
        </div>
        <LiquidGlassButton
          backgroundImage={backdrop}
          variant="dark"
          checked={site.boost_enabled}
          disabled={busy}
          aria-label={site.boost_enabled ? "刷流已开启，点击关闭" : "刷流已关闭，点击开启"}
          onCheckedChange={(next) =>
            void guard(async () => onChanged(await setSiteRatioBoost(site.site_id, next)))
          }
          className="!min-h-0 !w-auto shrink-0 !gap-0 !bg-transparent !p-0"
        >
          <span className="sr-only">{site.boost_enabled ? "刷流已开启" : "刷流已关闭"}</span>
        </LiquidGlassButton>
      </div>

      {/* 开启后：预算设置 + 运行统计 */}
      {site.boost_enabled && (
        <div className="flex flex-wrap items-center gap-x-7 gap-y-2">
          <div className="flex items-center gap-2">
            <span className="text-micro text-[var(--text-faint)]">存储预算</span>
            <input
              type="number"
              min={1}
              value={budgetGib}
              disabled={busy}
              onChange={(e) => setBudgetGib(e.target.value)}
              onBlur={commitBudget}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
              }}
              className="w-20 rounded-lg border border-white/[0.08] bg-white/[0.04] px-2 py-1 text-right text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
            />
            <span className="text-caption text-[var(--text-muted)]">GiB</span>
          </div>
          {boost && (
            <>
              <ProfileStat
                label="已用 / 预算"
                value={`${formatBytes(boost.used_bytes)} / ${formatBytes(boost.budget_bytes)}`}
              />
              <ProfileStat label="在池做种" value={String(boost.active_count)} />
              <ProfileStat label="累计上传" value={formatBytes(boost.uploaded_bytes_total)} />
              {boost.evicted_count > 0 && (
                <ProfileStat label="已汰换" value={String(boost.evicted_count)} />
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** 「下次同步」文案：null（立即到期）或时刻已过（等待 tick 扫描）都显示「即将开始」，
 *  避免出现"下次同步：3 分钟前"这种矛盾表述。 */
function nextSyncLabel(iso: string | null): string {
  if (!iso || new Date(iso).getTime() <= Date.now()) return "即将开始";
  return formatRelativeTime(iso);
}

/* —— 资料统计单元：label 小字 + 数值 semibold，构成卡片内的一行 stat tiles —— */

function ProfileStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <p className="text-micro text-[var(--text-faint)]">{label}</p>
      <p className="mt-0.5 truncate text-ui font-semibold text-[var(--text)]">{value}</p>
    </div>
  );
}

/* —— 站点操作折叠菜单：把编辑/重新验证/删除收进 ⋯ 里，点击外部或选中任一项后关闭 —— */

interface SiteActionsMenuProps {
  expanded: boolean;
  busy: boolean;
  /** 处于 pending/verifying 中间态时不允许再次触发验证 */
  canReverify: boolean;
  onEdit: () => void;
  onReverify: () => void;
  onDelete: () => void;
}

function SiteActionsMenu({
  expanded,
  busy,
  canReverify,
  onEdit,
  onReverify,
  onDelete,
}: SiteActionsMenuProps) {
  // 用 Radix DropdownMenu：菜单渲染进 body Portal 并由 Floating UI 做碰撞检测，
  // 靠近视口边缘时自动翻转/收边，绝不会像绝对定位那样把父容器/页面撑开。
  // 开合状态、点击外部关闭、键盘导航与焦点管理都由 Radix 托管。
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
          <DropdownMenu.Item
            onSelect={onReverify}
            disabled={busy || !canReverify}
            className={itemClass}
          >
            重新验证
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

/* —— 授权表单：根据所选授权类型渲染必填字段 —— */

interface SiteFormProps {
  item: CatalogItem;
  site: ConfiguredSite | null;
  busy: boolean;
  onSubmit: (payload: SiteConfigPayload) => void;
}

function SiteForm({ item, site, busy, onSubmit }: SiteFormProps) {
  const options = item.supported_auth_types;
  // 默认选中：已配置的沿用其类型，否则取第一个支持项
  const [authType, setAuthType] = useState<SiteAuthType>(
    site?.auth_type ?? options[0]?.auth_type ?? "cookie",
  );
  // 各字段值。编辑时出于安全后端不回传敏感值，故一律留空，需用户重新填写。
  const [values, setValues] = useState<Record<string, string>>({});

  const current = options.find((o) => o.auth_type === authType) ?? options[0];
  const fields = current?.required_fields ?? [];

  const canSubmit = fields.length > 0 && fields.every((f) => values[f]?.trim());

  function submit() {
    const payload: SiteConfigPayload = { auth_type: authType, enabled: site?.enabled ?? true };
    for (const f of fields) {
      (payload as unknown as Record<string, unknown>)[f] = values[f]?.trim() ?? "";
    }
    onSubmit(payload);
  }

  return (
    <div className="space-y-4">
      {/* 授权类型选择（多于一种时才展示） */}
      {options.length > 1 && (
        <div>
          <label className="mb-1.5 block text-sub font-medium text-[var(--text-muted)]">
            授权方式
          </label>
          <div className="flex flex-wrap gap-2">
            {options.map((opt: AuthTypeRequirement) => (
              <button
                key={opt.auth_type}
                type="button"
                onClick={() => setAuthType(opt.auth_type)}
                data-active={authType === opt.auth_type}
                className="glass-row nav-item !w-auto px-3 py-1.5 text-sub font-medium"
              >
                {AUTH_TYPE_LABEL[opt.auth_type]}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* 必填字段 */}
      {fields.map((field) => {
        const fm = FIELD_META[field] ?? { label: field, kind: "text" as const };
        return (
          <div key={field}>
            <label className="mb-1.5 block text-sub font-medium text-[var(--text-muted)]">
              {fm.label}
            </label>
            {/* Cookie 恰是插件的用武之地：就地提一句，不打断手动粘贴的用户 */}
            {field === "cookie" && (
              <p className="mb-1.5 text-caption text-[var(--text-faint)]">
                手动粘贴的 Cookie 过期后需重填；推荐用本页下方的 MovieClaw 浏览器插件自动同步。
              </p>
            )}
            {fm.kind === "textarea" ? (
              <textarea
                value={values[field] ?? ""}
                onChange={(e) => setValues((v) => ({ ...v, [field]: e.target.value }))}
                rows={3}
                autoComplete="off"
                placeholder={site ? "出于安全，请重新填写" : ""}
                className="scroll-thin w-full resize-none rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
              />
            ) : (
              <input
                type={fm.kind}
                value={values[field] ?? ""}
                onChange={(e) => setValues((v) => ({ ...v, [field]: e.target.value }))}
                placeholder={site ? "出于安全，请重新填写" : ""}
                // Chrome 对 password 字段会无视 "off" 仍弹出已存密码，须用 "new-password" 抑制
                autoComplete={fm.kind === "password" ? "new-password" : "off"}
                className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
              />
            )}
          </div>
        );
      })}

      <div className="flex items-center justify-end gap-3 pt-1">
        <button
          type="button"
          onClick={submit}
          disabled={busy || !canSubmit}
          className="btn-accent rounded-full px-4.5 py-2 text-ui font-semibold disabled:opacity-40"
        >
          {busy ? "保存中…" : site ? "保存并重新验证" : "保存并验证"}
        </button>
      </div>
    </div>
  );
}
