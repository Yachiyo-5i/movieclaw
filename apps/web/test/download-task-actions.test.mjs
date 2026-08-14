import assert from "node:assert/strict";
import test from "node:test";

import { shouldOfferInlineReplacement } from "../lib/download-task-actions.ts";

test("15 分钟无进度且仍有下载器任务时在提示区提供立即换种", () => {
  assert.equal(
    shouldOfferInlineReplacement({ can_replace: true, downloader_id: 3 }),
    true,
  );
});

test("尚未进入救援窗口时不提前展示立即换种", () => {
  assert.equal(
    shouldOfferInlineReplacement({ can_replace: false, downloader_id: 3 }),
    false,
  );
});

test("无法定位下载器任务时不展示不可执行的换种按钮", () => {
  assert.equal(
    shouldOfferInlineReplacement({ can_replace: true, downloader_id: null }),
    false,
  );
});
