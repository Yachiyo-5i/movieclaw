import assert from "node:assert/strict";
import test from "node:test";

import {
  RESOURCE_TIMING_WINDOW_SECONDS,
  shouldShowResourceTiming,
} from "../lib/resource-timing.ts";

test("发布到提交不超过七天时展示资源耗时", () => {
  assert.equal(shouldShowResourceTiming(RESOURCE_TIMING_WINDOW_SECONDS), true);
});

test("发布到提交超过七天时隐藏整行资源耗时", () => {
  assert.equal(shouldShowResourceTiming(RESOURCE_TIMING_WINDOW_SECONDS + 1), false);
});

test("缺少资源发布时间时仍展示可用的索引到提交耗时", () => {
  assert.equal(shouldShowResourceTiming(null), true);
});
