import assert from "node:assert/strict";
import test from "node:test";

import { subscribeMediaQueryChange } from "../lib/use-media-query.ts";

test("优先使用标准 MediaQueryList change 事件并正确解除订阅", () => {
  const calls = [];
  const listener = () => {};
  const mql = {
    addEventListener: (type, callback) => calls.push(["add", type, callback]),
    removeEventListener: (type, callback) => calls.push(["remove", type, callback]),
  };

  const unsubscribe = subscribeMediaQueryChange(mql, listener);
  unsubscribe();

  assert.deepEqual(calls, [
    ["add", "change", listener],
    ["remove", "change", listener],
  ]);
});

test("旧版 WebKit 降级到 addListener/removeListener", () => {
  const calls = [];
  const listener = () => {};
  const mql = {
    addListener: (callback) => calls.push(["add", callback]),
    removeListener: (callback) => calls.push(["remove", callback]),
  };

  const unsubscribe = subscribeMediaQueryChange(mql, listener);
  unsubscribe();

  assert.deepEqual(calls, [
    ["add", listener],
    ["remove", listener],
  ]);
});

test("无事件接口的极旧 WebView 保留首次判断且不抛错", () => {
  const unsubscribe = subscribeMediaQueryChange({}, () => {});
  assert.doesNotThrow(unsubscribe);
});
