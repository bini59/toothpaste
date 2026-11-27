export type Compaction = {
  id: number;
  title: string | null;
  finished_at: string;
  failed: boolean;
  continued: boolean;
  before_context_tokens: number | null;
  first_context_tokens: number | null;
  first_savings_tokens: number | null;
  cost_tokens: number | null;
  net_savings_tokens: number | null;
  turns: number;
  measured: number;
};
export const periods = [
  { value: "7", label: "7일" },
  { value: "30", label: "30일" },
  { value: "90", label: "90일" },
  { value: "all", label: "전체" },
] as const;
export type CompactionPeriod = (typeof periods)[number]["value"];
type TokenKey = "cost_tokens" | "net_savings_tokens";
const DAY = 86_400_000;

export const fmt = new Intl.NumberFormat("ko-KR");
const signed = new Intl.NumberFormat("ko-KR", { signDisplay: "exceptZero" });
export const tokens = (n: number | null, delta = false) => n === null ? "—" : (delta ? signed : fmt).format(n);

export const sum = (rows: Compaction[], key: TokenKey) =>
  rows.some((row) => row[key] !== null) ? rows.reduce((total, row) => total + (row[key] ?? 0), 0) : null;

export function compactionTrend(rows: Compaction[], period: CompactionPeriod, now: number) {
  const first = rows.reduce((earliest, row) => Math.min(earliest, Date.parse(row.finished_at)), now);
  const last = rows.reduce((latest, row) => Math.max(latest, Date.parse(row.finished_at)), now);
  const unit = period === "all" ? "month" : period === "90" ? "week" : "day";
  const bucket = (at: number) => {
    const date = new Date(at);
    if (unit === "month") return Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1);
    const day = Math.floor(at / DAY) * DAY;
    return unit === "week" ? day - ((date.getUTCDay() + 6) % 7) * DAY : day;
  };
  const next = (at: number) => {
    if (unit !== "month") return at + (unit === "week" ? 7 : 1) * DAY;
    const date = new Date(at);
    return Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1);
  };
  const start = bucket(period === "all" ? first : Math.min(first, now - Number(period) * DAY));
  const end = next(bucket(last));
  const byBucket = new Map<number, Compaction[]>();
  for (const row of rows) {
    const key = bucket(Date.parse(row.finished_at));
    if (!byBucket.has(key)) byBucket.set(key, []);
    byBucket.get(key)!.push(row);
  }
  const counts = [], usage = [];
  for (let from = start; from < end; from = next(from)) {
    const to = next(from);
    const items = byBucket.get(from) ?? [];
    const at = (from + to) / 2;
    const date = new Date(from).toISOString().slice(0, 10);
    const tick = unit === "month" ? date.slice(0, 7) : date.slice(5);
    const label = unit === "week" ? `${date} ~ ${new Date(to - DAY).toISOString().slice(0, 10)} UTC`
      : `${unit === "month" ? tick : date} UTC`;
    counts.push({ at, label, tick, values: [items.length] });
    usage.push({
      at, label, tick, values: [sum(items, "cost_tokens"), sum(items, "net_savings_tokens")],
      notes: (["cost_tokens", "net_savings_tokens"] as const)
        .map((key) => `기록 ${fmt.format(items.filter((row) => row[key] !== null).length)}/${fmt.format(items.length)}회`),
    });
  }
  return { start, end, counts, usage };
}
