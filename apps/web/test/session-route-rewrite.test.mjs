import assert from "node:assert/strict";
import test from "node:test";

import nextConfig from "../next.config.ts";

test("应用会话详情不被 Jellyfin Sessions rewrite 劫持", async () => {
  const rewrites = await nextConfig.rewrites();
  const sessionSources = rewrites
    .map((rule) => rule.source)
    .filter((source) => source.toLowerCase().startsWith("/sessions"));

  assert.ok(!sessionSources.some((source) => source.toLowerCase() === "/sessions/:path*"));
  assert.deepEqual(sessionSources.sort(), [
    "/Sessions",
    "/Sessions/Capabilities",
    "/Sessions/Capabilities/:path*",
    "/Sessions/Playing",
    "/Sessions/Playing/:path*",
  ]);
});
