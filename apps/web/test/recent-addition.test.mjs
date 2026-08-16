import assert from "node:assert/strict";
import test from "node:test";

import {
  buildRecentAdditionOverlay,
  formatRecentAddition,
} from "../lib/recent-addition.ts";

function addition({
  season_count = 1,
  episode_count = 1,
  season_number = 2,
  first_episode_number = 7,
  last_episode_number = first_episode_number,
  complete_season = false,
} = {}) {
  return {
    season_count,
    episode_count,
    season_number,
    first_episode_number,
    last_episode_number,
    complete_season,
  };
}

test("单集与同季连续多集保留精确集号", () => {
  assert.equal(
    formatRecentAddition(addition()),
    "S02E07",
  );
  assert.equal(
    formatRecentAddition(
      addition({ episode_count: 3, first_episode_number: 7, last_episode_number: 9 }),
    ),
    "S02E07–09",
  );
});

test("零散集与跨季批次改用紧凑计数", () => {
  assert.equal(
    formatRecentAddition(
      addition({ episode_count: 3, first_episode_number: null, last_episode_number: null }),
    ),
    "S02 · 新增3集",
  );
  assert.equal(
    formatRecentAddition(
      addition({
        season_count: 2,
        episode_count: 2,
        season_number: null,
        first_episode_number: null,
        last_episode_number: null,
      }),
    ),
    "新增2季 · 2集",
  );
});

test("官方集数确认完整时展示整季文案", () => {
  assert.equal(
    formatRecentAddition(
      addition({
        episode_count: 3,
        first_episode_number: 1,
        last_episode_number: 3,
        complete_season: true,
      }),
    ),
    "第2季 · 全3集",
  );
});

test("空批次不制造摘要", () => {
  assert.equal(formatRecentAddition(addition({ season_count: 0, episode_count: 0 })), null);
});

test("剧集 hover 分两行展示本批季集与入库时间", () => {
  assert.deepEqual(
    buildRecentAdditionOverlay(
      "tv",
      addition({ episode_count: 3, first_episode_number: 7, last_episode_number: 9 }),
      "2小时前入库",
    ),
    { primary: "S02E07–09", secondary: "2小时前入库" },
  );
});

test("电影与旧剧集没有季集摘要时只展示入库时间", () => {
  assert.deepEqual(buildRecentAdditionOverlay("movie", null, "2小时前入库"), {
    primary: "2小时前入库",
  });
  assert.deepEqual(buildRecentAdditionOverlay("tv", null, "2小时前入库"), {
    primary: "2小时前入库",
  });
  assert.equal(buildRecentAdditionOverlay("tv", null, null), undefined);
});
