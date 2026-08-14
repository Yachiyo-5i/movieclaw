import assert from "node:assert/strict";
import test from "node:test";

import { subscriptionFollowRibbon } from "../lib/subscription-ui.ts";

function subscription({
  kind = "tv",
  followFuture = true,
  wanted = 0,
  grabbed = 0,
  downloaded = 0,
  imported = 0,
} = {}) {
  return {
    media: { kind },
    follow_future: followFuture,
    progress: {
      total: wanted + grabbed + downloaded + imported,
      wanted,
      grabbed,
      downloaded,
      imported,
    },
  };
}

test("有未入库或在途剧集时显示追新中", () => {
  assert.equal(subscriptionFollowRibbon(subscription({ wanted: 2 })), "追新中");
  assert.equal(subscriptionFollowRibbon(subscription({ grabbed: 1 })), "追新中");
  assert.equal(subscriptionFollowRibbon(subscription({ downloaded: 1 })), "追新中");
});

test("已知剧集全部入库后显示自动续订", () => {
  assert.equal(subscriptionFollowRibbon(subscription({ imported: 12 })), "自动续订");
});

test("创建时内容已在库且没有工单也显示自动续订", () => {
  assert.equal(subscriptionFollowRibbon(subscription()), "自动续订");
});

test("关闭持续追新或电影订阅不显示角标", () => {
  assert.equal(subscriptionFollowRibbon(subscription({ followFuture: false })), undefined);
  assert.equal(subscriptionFollowRibbon(subscription({ kind: "movie" })), undefined);
});
