type Metric = { value: number | null; n: number };
export type Period = {
  start: string; end: string; observed_until: string | null;
  values: Record<string, Metric>; token_sources: string[]; token_runs: number; linked_runs: number;
  execution_count: number; work_execution_count: number;
};
export type Marker = { id: number; at: string; label: string; kind: "version" | "manual" };
export type Report = {
  period: Period; weeks: Period[]; markers: Marker[]; pending: number; initialized: boolean;
  comparison: { before: Period; after: Period; complete: boolean } | null;
};

type Unit = "percent" | "tokens" | "hours" | "rounds" | "calls" | "count";
export type Series = { key: string; label: string; unit: Unit; sample: string; color?: number };
export type Chart = { title: string; series: Series[] };
export const production: Series[] = [
  { key: "intervention_free", label: "무개입 프로덕션 도달률", unit: "percent", sample: "티켓" },
  { key: "tokens_per_ticket", label: "프로덕션 티켓당 토큰", unit: "tokens", sample: "티켓" },
];
const targets = ["milestone", "staging", "main", "기타"];
function durationSeries(key: string): Series[] {
  return targets.map((label, color) => ({
    key: `${key}_${color === 3 ? "other" : label}`, label, unit: "hours", sample: "PR", color,
  }));
}
export const charts: Chart[] = [
  ...production.map((metric) => ({ title: metric.label, series: [metric] })),
  { title: "첫 AI 리뷰 승인율", series: [{ key: "first_review_approval", label: "첫 AI 리뷰 승인율", unit: "percent", sample: "PR" }] },
  { title: "PR당 평균 수정 라운드", series: [{ key: "rework_rounds", label: "PR당 평균 수정 라운드", unit: "rounds", sample: "PR" }] },
  { title: "PR 생성 → 머지 중앙값", series: durationSeries("pr_to_merge") },
  { title: "머지 → 프로덕션 중앙값", series: durationSeries("merge_to_prod") },
  { title: "에이전트 실행당 도구 호출", series: [{ key: "tools_per_run", label: "에이전트 실행당 도구 호출", unit: "calls", sample: "실행" }] },
  { title: "반복 패턴", series: [
    { key: "help_calls", label: "도움말 재조회", unit: "count", sample: "실행", color: 0 },
    { key: "dev_calls", label: "dev 서버 직접 실행·재시작", unit: "count", sample: "실행", color: 1 },
    { key: "wait_calls", label: "대기 루프", unit: "count", sample: "실행", color: 2 },
  ] },
  { title: "안정성", series: [
    { key: "errors", label: "고유 오류", unit: "count", sample: "오류 이벤트", color: 0 },
    { key: "missing_ws_set", label: "ws-set 없이 종료 (확인된 건수)", unit: "count", sample: "", color: 1 },
    { key: "conflict_rework", label: "충돌 수정 실행", unit: "count", sample: "", color: 2 },
  ] },
];
const number = new Intl.NumberFormat("ko", { maximumFractionDigits: 1 });
const axisNumber = new Intl.NumberFormat("ko", { maximumFractionDigits: 3 });
export const integer = new Intl.NumberFormat("ko", { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("ko", { notation: "compact", maximumFractionDigits: 1 });
const shortDate = new Intl.DateTimeFormat("ko", { month: "numeric", day: "numeric", timeZone: "UTC" });
export const markerDate = new Intl.DateTimeFormat("ko", {
  year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
});
export const day = (s: string) => shortDate.format(new Date(s));

export function display(value: number | null, unit: Unit, axis = false) {
  if (value === null) return "—";
  const format = axis ? axisNumber : number;
  if (unit === "percent") return `${format.format(value * 100)}%`;
  if (unit === "hours") return `${format.format(value)}시간`;
  if (unit === "tokens") return (axis ? compact : integer).format(value);
  return format.format(value);
}

function tokenScope(period: Period) {
  const { token_sources: sources } = period;
  if (sources.length === 1) return sources[0] === "claude" ? "Claude만" : "Codex만";
  return sources.length ? "Claude · Codex" : "";
}

export function sample(period: Period, series: Series) {
  const n = period.values[series.key].n;
  if (series.key === "tokens_per_ticket") {
    return `티켓 ${n}/${period.values.intervention_free.n}개${tokenScope(period) ? ` · ${tokenScope(period)}` : ""}`;
  }
  if (series.key === "missing_ws_set" && period.observed_until) {
    return `ws-set 직접 추적 ${integer.format(n)}/${integer.format(period.work_execution_count)}개`;
  }
  if (series.sample === "실행") return `실행 ${integer.format(n)}/${integer.format(period.execution_count)}개`;
  if (!series.sample && period.observed_until) {
    const observed = (Date.parse(period.observed_until) - Date.parse(period.start)) / 86_400_000;
    return `관측 ${observed < 0.1 ? "0.1일 미만" : `${number.format(observed)}일`}`;
  }
  return series.sample ? `${series.sample} ${integer.format(n)}개` : "";
}

export const dateRange = (start: string, end: string) => `${start.slice(0, 10)} ~ ${end.slice(0, 10)} UTC`;
export const isPartial = (period: Period) => period.observed_until !== null && period.observed_until < period.end;
