import assert from "node:assert/strict";
import { compactionTrend, sum, tokens } from "./compactionData.ts";

const row = (fields) => ({
  id: 1, title: null, finished_at: "2026-11-16T00:00:00Z", failed: false, continued: false,
  before_context_tokens: null, first_context_tokens: null, first_savings_tokens: null,
  cost_tokens: null, net_savings_tokens: null, turns: 0, measured: 0, ...fields,
});
const now = Date.parse("2026-11-18T11:00:00Z");
const rows = [
  row({ finished_at: "2026-11-16T08:59:59+09:00", cost_tokens: 40, net_savings_tokens: -40 }),
  row({ id: 2, cost_tokens: 20, net_savings_tokens: 0 }),
  row({ id: 3 }),
  row({ id: 4, finished_at: "2026-11-17T01:00:00Z", cost_tokens: 0 }),
];
const daily = compactionTrend(rows, "7", now);
assert.equal(daily.start, Date.parse("2026-11-11T00:00:00Z"));
assert.equal(daily.end, Date.parse("2026-11-19T00:00:00Z"));
assert.deepEqual(daily.counts.slice(-4).map((point) => point.values), [[1], [2], [1], [0]]);
assert.deepEqual(daily.usage.slice(-4).map((point) => point.values), [[40, -40], [20, 0], [0, null], [null, null]]);
assert.deepEqual(daily.usage.at(-3)?.notes, ["기록 1/2회", "기록 1/2회"]);

const weekly = compactionTrend(rows, "90", now);
assert.deepEqual(weekly.counts.slice(-2).map((point) => point.label), ["2026-11-09 ~ 2026-11-15 UTC", "2026-11-16 ~ 2026-11-22 UTC"]);
assert.deepEqual(weekly.usage.slice(-2).map((point) => point.values), [[40, -40], [20, 0]]);
const monthly = compactionTrend([row({ finished_at: "2025-12-31T23:59:59Z" }), ...rows], "all", now);
assert.equal(monthly.counts[0].label, "2025-12 UTC");
assert.equal(monthly.counts.at(-1)?.label, "2026-11 UTC");
assert.deepEqual(monthly.usage.at(-1)?.values, [60, -40]);
assert.equal(sum([], "net_savings_tokens"), null);
assert.equal(tokens(null, true), "—");
assert.equal(tokens(-40, true), "-40");
console.log("compaction trend checks ok");
