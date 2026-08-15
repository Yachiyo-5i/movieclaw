import assert from "node:assert/strict";
import test from "node:test";

import {
  formatRuntimeMinutes,
  formatVideoResolution,
} from "../lib/format.ts";

test("电影片长按小时和剩余分钟展示", () => {
  assert.equal(formatRuntimeMinutes(45), "45 分钟");
  assert.equal(formatRuntimeMinutes(120), "2 小时");
  assert.equal(formatRuntimeMinutes(126), "2 小时 6 分钟");
});

test("分辨率使用准确的消费级标签", () => {
  assert.equal(formatVideoResolution("2160p"), "4K");
  assert.equal(formatVideoResolution("1440p"), "2K");
  assert.equal(formatVideoResolution("1080p"), "1080p");
  assert.equal(formatVideoResolution("2160"), "4K");
});
