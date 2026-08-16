import assert from "node:assert/strict";
import test from "node:test";

import { summarizeEpisodeUnits } from "../lib/episode-units.ts";

test("电影哨兵保持显示为正片", () => {
  assert.deepEqual(summarizeEpisodeUnits([{ season_number: 0, episode_number: 0 }]), {
    kind: "movie",
    label: "正片",
    fullLabel: "正片",
    episodeCount: 0,
    hiddenEpisodeCount: 0,
    seasons: [],
  });
});

test("连续集号排序去重后压缩为单个区间", () => {
  const summary = summarizeEpisodeUnits([
    { season_number: 1, episode_number: 3 },
    { season_number: 1, episode_number: 1 },
    { season_number: 1, episode_number: 2 },
    { season_number: 1, episode_number: 2 },
    { season_number: 1, episode_number: 4 },
    { season_number: 1, episode_number: 5 },
  ]);

  assert.equal(summary?.label, "S01E01–E05");
  assert.equal(summary?.episodeCount, 5);
  assert.deepEqual(summary?.seasons, [
    { seasonNumber: 1, episodes: [1, 2, 3, 4, 5] },
  ]);
});

test("缺集会拆成独立区间而不会被连续范围吞掉", () => {
  const summary = summarizeEpisodeUnits([
    ...[1, 2, 3, 4, 5, 7, 8, 9].map((episode_number) => ({
      season_number: 1,
      episode_number,
    })),
  ]);

  assert.equal(summary?.label, "S01E01–E05、E07–E09");
  assert.equal(summary?.fullLabel, "S01E01–E05、E07–E09");
  assert.equal(summary?.episodeCount, 8);
});

test("跨季区间分别保留季号", () => {
  const summary = summarizeEpisodeUnits([
    { season_number: 2, episode_number: 3 },
    { season_number: 1, episode_number: 2 },
    { season_number: 2, episode_number: 2 },
    { season_number: 1, episode_number: 1 },
    { season_number: 2, episode_number: 1 },
  ]);

  assert.equal(summary?.label, "S01E01–E02 · S02E01–E03");
  assert.deepEqual(summary?.seasons, [
    { seasonNumber: 1, episodes: [1, 2] },
    { seasonNumber: 2, episodes: [1, 2, 3] },
  ]);
});

test("零散区间过多时保留前两段并准确计算隐藏集数", () => {
  const summary = summarizeEpisodeUnits([
    ...[1, 2, 4, 6, 8, 9].map((episode_number) => ({
      season_number: 1,
      episode_number,
    })),
  ]);

  assert.equal(summary?.label, "S01E01–E02、E04 · 另 3 集");
  assert.equal(summary?.fullLabel, "S01E01–E02、E04、E06、E08–E09");
  assert.equal(summary?.hiddenEpisodeCount, 3);
  assert.equal(summary?.episodeCount, 6);
});

test("空集号列表不制造摘要", () => {
  assert.equal(summarizeEpisodeUnits([]), null);
});
