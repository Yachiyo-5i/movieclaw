import assert from "node:assert/strict";
import test from "node:test";

import {
  activeWallInitialAtViewport,
  wallInitialAtOffset,
} from "../lib/library-wall-index.ts";

const index = [
  { initial: "A", offset: 0 },
  { initial: "B", offset: 12 },
  { initial: "M", offset: 40 },
  { initial: "#", offset: 80 },
];

test("分页窗口起点映射到所在字母档", () => {
  assert.equal(wallInitialAtOffset(index, 0), "A");
  assert.equal(wallInitialAtOffset(index, 25), "B");
  assert.equal(wallInitialAtOffset(index, 80), "#");
  assert.equal(wallInitialAtOffset(index, -1), null);
});

test("滚动时选中最后一个越过视口顶边的字母锚点", () => {
  const markers = [
    { initial: "A", top: -500 },
    { initial: "B", top: -20 },
    { initial: "M", top: 260 },
  ];
  assert.equal(activeWallInitialAtViewport(markers, 0, "A"), "B");
  assert.equal(activeWallInitialAtViewport(markers, 300, "A"), "M");
});

test("尚未滚到首个字母锚点时保留当前分页档", () => {
  assert.equal(
    activeWallInitialAtViewport([{ initial: "M", top: 400 }], 0, "M"),
    "M",
  );
});
