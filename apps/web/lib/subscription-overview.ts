import type { Subscription } from "@/lib/api/subscriptions";

export type SubscriptionFilter = "all" | "movie" | "tv";
export type SubscriptionKind = Exclude<SubscriptionFilter, "all">;

export const SUBSCRIPTION_BATCH_SIZE = 24;

export interface SubscriptionOverviewSection {
  kind: SubscriptionKind;
  title: string;
  subscriptions: Subscription[];
}

/** 总览固定剧集在前、电影在后；单分类继续复用同一份分组结果。 */
export function subscriptionOverviewSections(
  subscriptions: Subscription[],
  filter: SubscriptionFilter,
): SubscriptionOverviewSection[] {
  const sections: SubscriptionOverviewSection[] = [
    {
      kind: "tv",
      title: "剧集订阅",
      subscriptions: subscriptions.filter((sub) => sub.media.kind === "tv"),
    },
    {
      kind: "movie",
      title: "电影订阅",
      subscriptions: subscriptions.filter((sub) => sub.media.kind === "movie"),
    },
  ];
  return filter === "all"
    ? sections.filter((section) => section.subscriptions.length > 0)
    : sections.filter((section) => section.kind === filter);
}

/** IntersectionObserver 每次只追加一批，并在最后一批精确收敛到总数。 */
export function nextSubscriptionVisibleCount(current: number, total: number): number {
  return Math.min(current + SUBSCRIPTION_BATCH_SIZE, total);
}
