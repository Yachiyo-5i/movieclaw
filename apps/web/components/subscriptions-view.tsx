"use client";

import { useCallback, useEffect, useState } from "react";

import type { Route } from "next";
import Link from "next/link";

import { ContentEmptyState } from "@/components/content-empty-state";
import { CompassIcon } from "@/components/icons";
import { PosterCardVisual, type PosterVisualItem } from "@/components/poster-card";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import { getPipelineHealth, type Subscription } from "@/lib/api/subscriptions";
import { cachedImageUrl } from "@/lib/image-proxy";
import { usePageChrome } from "@/lib/page-chrome";
import { usePermissions } from "@/lib/permissions";
import {
  subscriptionProgressNote,
  subscriptionStatusMeta,
} from "@/lib/subscription-ui";
import { useIsMobile } from "@/lib/use-media-query";
import { useScrollRestoration } from "@/lib/use-scroll-restoration";

/**
 * 订阅页：用户全部订阅的海报墙。
 *
 * 数据直接消费 SubscribeEntryProvider 的全站订阅列表（唯一数据源）：
 * 弹层里取消订阅后 context 刷新，这里的墙面即时同步，无需各自维护快照。
 * 进入本页时主动 refresh 一次，保证工单进度是新鲜的。
 *
 * 结构：页头（标题 + 订阅总数说明）+ 自适应海报网格。
 * 每格复用发现页的 PosterCard，并在其下追加一行订阅状态
 * （彩色状态点 + 进度说明，如「追更中 · 缺 3 集」），
 * 让用户不点进详情也能扫读全部订阅的追踪进度。
 */
export function SubscriptionsView() {
  const { canManageSubscriptions, canSubscribe } = usePermissions();
  const scrollRef = useScrollRestoration("subscriptions");
  const [mediaType, setMediaType] = useState<"movie" | "tv">("movie");
  const { subscriptions, refresh } = useSubscribeEntry();
  const [failed, setFailed] = useState(false);
  // 链路体检整体为 error 时顶部亮警示横幅（提醒推到用户在的地方，
  // 全景与修复入口在 设置 → 订阅）。拉取失败静默——横幅只是提示层
  const [healthIssue, setHealthIssue] = useState<{ libraryErrors: number } | null>(null);

  const reload = useCallback(() => {
    setFailed(false);
    void refresh().then((ok) => setFailed(!ok));
    if (canManageSubscriptions) {
      void getPipelineHealth()
        .then((h) =>
          setHealthIssue(h.status === "error" ? { libraryErrors: h.error_count } : null),
        )
        .catch(() => setHealthIssue(null));
    } else {
      setHealthIssue(null);
    }
  }, [canManageSubscriptions, refresh]);

  useEffect(() => {
    reload();
  }, [reload]);

  // 电影/剧集切换在移动端挂进全局顶栏右上角（字标与搜索之间那段空位），
  // 与发现页的数据源切换同一套安放方式——窄屏页头不再为它单独堆一行。
  // 桌面端维持页头右侧的原位不变。
  const chrome = usePageChrome();
  const isMobile = useIsMobile();
  const setTopBarActions = chrome?.setTopBarActions;
  useEffect(() => {
    if (!isMobile || !setTopBarActions) return;
    return setTopBarActions(<MediaTypeSwitcher value={mediaType} onChange={setMediaType} />);
  }, [isMobile, setTopBarActions, mediaType]);

  const visible = (subscriptions ?? []).filter((s) => s.media.kind === mediaType);

  return (
    <div ref={scrollRef} className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      <div className="flex items-start justify-between gap-4 px-6 pt-2 max-md:px-4">
        <div>
          <h2 className="text-on-image text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[21px]">
            我的订阅
          </h2>
          <p className="text-on-image mt-1.5 text-ui text-[var(--text-muted)] max-md:mt-1 max-md:text-sub">
            共 {visible.length} 部{mediaType === "movie" ? "电影" : "剧集"} ·
            movieclaw 会持续追踪并在新资源放出后自动入库
          </p>
        </div>

        {!isMobile && <MediaTypeSwitcher value={mediaType} onChange={setMediaType} />}
      </div>

      {/* 链路警示横幅：只在体检整体为 error 时出现——订阅不会丢（工单退避
          重试），但在修好之前无法自动下载入库。点击进订阅设定看全景与修复 */}
      {canManageSubscriptions && healthIssue && (
        <Link
          href={"/settings/subscription" as Route}
          className="mx-6 mt-4 block rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sub leading-relaxed text-amber-200 transition hover:bg-amber-500/15 max-md:mx-4"
        >
          {healthIssue.libraryErrors > 0
            ? `${healthIssue.libraryErrors} 个媒体库的入库链路有问题，相关订阅暂时无法自动下载入库（已下达的任务会自动重试）`
            : "订阅链路尚未就绪（缺少可用的资源站点或下载器），订阅暂时只能记录意愿"}
          ——点击查看体检详情与修复入口 →
        </Link>
      )}

      {subscriptions === null && !failed && (
        <div className="mt-16 flex items-center justify-center gap-2.5 text-ui text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在加载订阅…
        </div>
      )}

      {failed && (
        <div className="mt-16 flex flex-col items-center gap-3 text-center">
          <p className="text-ui text-[var(--text-muted)]">订阅列表加载失败</p>
          <button
            type="button"
            onClick={reload}
            className="btn-glass px-4 py-2 text-ui font-medium text-[var(--text)]"
          >
            重试
          </button>
        </div>
      )}

      {subscriptions !== null && !failed && visible.length === 0 && (
        <ContentEmptyState
          variant="subscription"
          title={
            subscriptions.length === 0
              ? "从一部想看的作品开始"
              : `还没有${mediaType === "movie" ? "电影" : "剧集"}订阅`
          }
          description={
            canSubscribe
              ? `去发现页挑选一部${mediaType === "movie" ? "电影" : "剧集"}，打开详情并点击「订阅追踪」，有合适资源时会自动下载入库。`
              : "当前账号暂未开启订阅权限，请联系管理员为你开启。"
          }
          action={
            canSubscribe ? (
              <Link
                href={`/discover/${mediaType}` as Route}
                className="btn-accent flex items-center gap-1.5 rounded-full px-4 py-2 text-ui font-semibold"
              >
                <CompassIcon className="size-4" />
                去发现{mediaType === "movie" ? "电影" : "剧集"}
              </Link>
            ) : undefined
          }
        />
      )}

      {visible.length > 0 && (
        <div className="mt-6 grid gap-x-4 gap-y-7 px-6 [grid-template-columns:repeat(auto-fill,minmax(148px,1fr))] max-md:mt-4 max-md:gap-x-3 max-md:gap-y-5 max-md:px-4 max-md:[grid-template-columns:repeat(auto-fill,minmax(140px,1fr))]">
          {visible.map((sub) => (
            <SubscriptionCell key={sub.id} sub={sub} />
          ))}
        </div>
      )}
    </div>
  );
}

/** 订阅类型切换：沿用发现页的数据源切换样式，让同类操作保持一致。 */
function MediaTypeSwitcher({
  value,
  onChange,
}: {
  value: "movie" | "tv";
  onChange: (type: "movie" | "tv") => void;
}) {
  return (
    <div
      className="flex shrink-0 rounded-full border border-white/10 bg-black/35 p-1 backdrop-blur-xl"
      aria-label="订阅类型"
    >
      {(["movie", "tv"] as const).map((type) => (
        <button
          key={type}
          type="button"
          aria-pressed={value === type}
          onClick={() => onChange(type)}
          className={`rounded-full px-4 py-1.5 text-sub font-semibold transition ${
            value === type
              ? "bg-white/15 text-white shadow-sm"
              : "text-[var(--text-muted)] hover:text-white"
          }`}
        >
          {type === "movie" ? "电影" : "剧集"}
        </button>
      ))}
    </div>
  );
}

/** 把订阅条目摘要适配成海报卡片的视觉契约（带 type，悬浮层自动显示「已订阅」）。 */
function toVisualItem(sub: Subscription): PosterVisualItem {
  return {
    id: String(sub.media.tmdb_id),
    source: "tmdb",
    type: sub.media.kind,
    title: sub.media.title,
    year: sub.media.year ?? undefined,
    rating: 0,
    posterUrl: sub.media.poster_url ? cachedImageUrl(sub.media.poster_url) : "",
  };
}

/** 海报墙单元格：点击进订阅详情分析页（追踪明细 + 活动时间线），而非影片详情。 */
function SubscriptionCell({ sub }: { sub: Subscription }) {
  const meta = subscriptionStatusMeta[sub.status];
  return (
    <div>
      <PosterCardVisual
        item={toVisualItem(sub)}
        href={`/subscriptions/${sub.id}` as Route}
      />
      <p className="text-on-image mt-1.5 flex items-center gap-1.5 truncate text-caption text-[var(--text-muted)]">
        <span
          className="size-1.5 shrink-0 rounded-full"
          style={{ backgroundColor: meta.color }}
        />
        <span className="truncate">
          {meta.label} · {subscriptionProgressNote(sub)}
        </span>
      </p>
    </div>
  );
}
