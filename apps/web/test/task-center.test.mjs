import assert from "node:assert/strict";
import test from "node:test";

import { taskCenterViewFromQuery } from "../lib/task-center.ts";

test("任务中心下载中入口深链选择进行中 tab", () => {
  assert.equal(taskCenterViewFromQuery("active"), "active");
});

test("任务中心非法视图安全回退全部", () => {
  assert.equal(taskCenterViewFromQuery("downloads"), "all");
  assert.equal(taskCenterViewFromQuery("unknown"), "all");
  assert.equal(taskCenterViewFromQuery(undefined), "all");
});
