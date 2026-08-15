import assert from "node:assert/strict";
import test from "node:test";

import {
  nextSubscriptionVisibleCount,
  subscriptionOverviewSections,
} from "../lib/subscription-overview.ts";

function subscription(id, kind) {
  return { id, media: { kind } };
}

test("全部视图固定按剧集、电影分区并保留各自原始顺序", () => {
  const sections = subscriptionOverviewSections(
    [subscription(1, "movie"), subscription(2, "tv"), subscription(3, "tv")],
    "all",
  );

  assert.deepEqual(
    sections.map((section) => ({
      kind: section.kind,
      ids: section.subscriptions.map((sub) => sub.id),
    })),
    [
      { kind: "tv", ids: [2, 3] },
      { kind: "movie", ids: [1] },
    ],
  );
});

test("单分类视图只返回对应分区，空分类仍保留可识别的空分区", () => {
  const sections = subscriptionOverviewSections([subscription(1, "movie")], "tv");

  assert.equal(sections.length, 1);
  assert.equal(sections[0].kind, "tv");
  assert.deepEqual(sections[0].subscriptions, []);
});

test("无感追加按固定批次增长且最后一批不超过总数", () => {
  assert.equal(nextSubscriptionVisibleCount(24, 100), 48);
  assert.equal(nextSubscriptionVisibleCount(48, 50), 50);
});
