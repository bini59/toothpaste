import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

process.env.TZ = "Asia/Seoul";

const server = await createServer({
  configFile: false,
  esbuild: { jsx: "automatic" },
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, ws: false, watch: null },
});
const { UsageCard } = await server.ssrLoadModule("/src/components/UsageCard.tsx").finally(() => server.close());

const render = (usage, collapsed = false) => renderToStaticMarkup(createElement(UsageCard, { usage, collapsed }));
const labels = (html) => [...html.matchAll(/<p class="([^"]+)">([^<]+)<\/p>/g)];
const now = new Date("2026-11-27T16:00:00Z"); // 11/28 01:00 in Seoul
const usage = {
  claude: {
    five_hour: { utilization: 50, resets_at: "2026-11-28T09:30:00Z" },
    seven_day: { utilization: 20, resets_at: "2026-11-28T16:05:00Z" },
    error: null,
  },
  codex: {
    buckets: [{ windows: [
      { window: "primary", windowDurationMins: 300, remainingPercent: 75, resetAt: Date.parse("2026-11-28T09:45:00Z") / 1000 },
      { window: "secondary", windowDurationMins: 10080, remainingPercent: 85, resetAt: Date.parse("2026-11-29T09:30:00Z") / 1000 },
    ] }],
    error: null,
  },
};

test("shows each reset in browser local time and keeps the collapsed row tooltips", ({ mock }) => {
  mock.timers.enable({ apis: ["Date"], now });
  const expected = ["18:30 리셋", "11/29 01:05 리셋", "18:45 리셋", "11/29 18:30 리셋"];
  const expanded = labels(render(usage));
  assert.deepEqual(expanded.map(([, , text]) => text), expected);
  assert.ok(expanded.every(([, classes]) => !classes.includes("hidden")));

  const collapsed = render(usage, true);
  const compact = labels(collapsed);
  assert.deepEqual(compact.map(([, , text]) => text), expected);
  assert.ok(compact.every(([, classes]) => classes.endsWith(" lg:hidden")));
  assert.match(collapsed, /title="Claude 5시간 18:30 리셋 · 7일 11\/29 01:05 리셋/);
  assert.match(collapsed, /title="Codex 5시간 18:45 리셋 · 주간 11\/29 18:30 리셋/);
});

test("omits missing and invalid reset labels while keeping valid neighbors", ({ mock }) => {
  mock.timers.enable({ apis: ["Date"], now });
  const invalid = structuredClone(usage);
  invalid.claude.five_hour.resets_at = "invalid";
  invalid.claude.seven_day.resets_at = null;
  invalid.codex.buckets[0].windows[0].resetAt = 9e12; // finite seconds outside JavaScript's date range
  assert.deepEqual(labels(render(invalid)).map(([, , text]) => text), ["11/29 18:30 리셋"]);

  invalid.claude.five_hour.resets_at = true;
  invalid.codex.buckets[0].windows[0].resetAt = "1795973400";
  assert.deepEqual(labels(render(invalid)).map(([, , text]) => text), ["11/29 18:30 리셋"]);
  assert.deepEqual(labels(render({ claude: null, codex: null })), []);
});
