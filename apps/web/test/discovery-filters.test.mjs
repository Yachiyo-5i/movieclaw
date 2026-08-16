import assert from "node:assert/strict";
import test from "node:test";

import {
  discoveryFilterCount,
  discoveryFiltersQuery,
  parseDiscoveryFilters,
} from "../lib/discovery-filters.ts";

test("六维筛选可从分享 URL 安全恢复并重新序列化", () => {
  const filters = parseDiscoveryFilters({
    genres: "878,28,878,bad",
    country: "jp",
    year: "2025",
    rating: "7",
    runtime: "90",
    sort: "rating",
  });

  assert.deepEqual(filters, {
    genreIds: [878, 28],
    originCountry: "JP",
    year: 2025,
    ratingGte: 7,
    runtimeLte: 90,
    sort: "rating",
  });
  assert.equal(discoveryFilterCount(filters), 6);
  assert.equal(
    discoveryFiltersQuery(filters),
    "genres=878%2C28&country=JP&year=2025&rating=7&runtime=90&sort=rating",
  );
});

test("非法筛选值被忽略并回退热门排序", () => {
  assert.deepEqual(
    parseDiscoveryFilters({
      genres: "-1,0,nope",
      country: "Japan",
      year: "2200",
      rating: "11",
      runtime: "0",
      sort: "unknown",
    }),
    { genreIds: [], sort: "popular" },
  );
});
