import type { Subscription, SubscriptionStatus } from "@/lib/api/subscriptions";

/**
 * 订阅状态的展示元数据与进度文案（订阅页海报墙、详情页操作区共用）。
 * 颜色语义：蓝=追踪中、绿=已收齐、黄=已暂停。
 */
export const subscriptionStatusMeta: Record<
  SubscriptionStatus,
  { label: string; color: string }
> = {
  active: { label: "追踪中", color: "#6aa7ff" },
  completed: { label: "已收齐", color: "#4ade80" },
  paused: { label: "已暂停", color: "#f5c451" },
};

/** 进度说明：回答「还缺多少 / 入库了多少」——订阅信息里最高频的一眼答案。 */
export function subscriptionProgressNote(sub: Subscription): string {
  const { wanted, grabbed, downloaded, imported, total } = sub.progress;
  if (sub.status === "paused") return "暂停追踪";
  const inPipeline = grabbed + downloaded;
  if (wanted === 0) {
    if (sub.media.kind === "movie") {
      return imported > 0 ? "已入库" : inPipeline > 0 ? "下载安排中" : "已收齐";
    }
    if (inPipeline > 0) return `${inPipeline} 集下载中 · 已入库 ${imported}`;
    if (sub.status === "active") return "等待新集播出";
    return imported > 0 ? `全部 ${total} 集已入库` : `全部 ${total} 集已安排`;
  }
  if (sub.media.kind === "movie") return "正在寻找资源";
  const detail = [
    inPipeline > 0 ? `${inPipeline} 集下载中` : null,
    imported > 0 ? `已入库 ${imported}` : null,
  ].filter(Boolean);
  return detail.length > 0 ? `缺 ${wanted} 集 · ${detail.join(" · ")}` : `缺 ${wanted} 集`;
}

/**
 * 剧集订阅海报的追新阶段角标。
 *
 * - 还有缺口、已抓取或待入库单元：当前正在追新；
 * - 已知单元均已入库（包括创建时就已在库、因此工单总数为 0）：保留自动续订，
 *   等未来新集或新季被纳入追踪；
 * - 电影或未开启持续追新：不展示角标。
 */
export function subscriptionFollowRibbon(
  sub: Pick<Subscription, "follow_future" | "media" | "progress">,
): "追新中" | "自动续订" | undefined {
  if (sub.media.kind !== "tv" || !sub.follow_future) return undefined;
  const { wanted, grabbed, downloaded } = sub.progress;
  return wanted + grabbed + downloaded > 0 ? "追新中" : "自动续订";
}
