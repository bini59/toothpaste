import { useEffect, useState, type FormEvent } from "react";
import { getJson, postJson } from "../api";
import { Select } from "../components/Select";
import { Button, Card, Input, cn } from "../ui";

type Metric = { value: number | null; n: number };
type Period = {
  start: string; end: string; observed_until: string | null;
  values: Record<string, Metric>; token_sources: string[]; token_runs: number; linked_runs: number; execution_count: number;
};
type Marker = { id: number; at: string; label: string; kind: "version" | "manual" };
type Report = {
  period: Period; weeks: Period[]; markers: Marker[]; pending: number; initialized: boolean;
  comparison: { before: Period; after: Period; complete: boolean } | null;
};

type Unit = "percent" | "tokens" | "hours" | "rounds" | "calls" | "count";
type Series = { key: string; label: string; unit: Unit; sample: string; color?: number };
type Chart = { title: string; series: Series[] };
const production: Series[] = [
  { key: "intervention_free", label: "무개입 프로덕션 도달률", unit: "percent", sample: "티켓" },
  { key: "tokens_per_ticket", label: "프로덕션 티켓당 토큰", unit: "tokens", sample: "티켓" },
];
const targets = ["milestone", "staging", "main", "기타"];
function durationSeries(key: string): Series[] {
  return targets.map((label, color) => ({
    key: `${key}_${color === 3 ? "other" : label}`, label, unit: "hours", sample: "PR", color,
  }));
}
const charts: Chart[] = [
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
    { key: "missing_ws_set", label: "ws-set 없이 종료", unit: "count", sample: "", color: 1 },
    { key: "conflict_rework", label: "충돌 수정 실행", unit: "count", sample: "", color: 2 },
  ] },
];
const palette = [
  "stroke-primary fill-primary", "stroke-secondary fill-secondary", "stroke-success-700 fill-success-700",
  "stroke-warning-700 fill-warning-700",
];
const number = new Intl.NumberFormat("ko", { maximumFractionDigits: 1 });
const integer = new Intl.NumberFormat("ko", { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("ko", { notation: "compact", maximumFractionDigits: 1 });
const shortDate = new Intl.DateTimeFormat("ko", { month: "numeric", day: "numeric", timeZone: "UTC" });
const markerDate = new Intl.DateTimeFormat("ko", {
  year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
});
const day = (s: string) => shortDate.format(new Date(s));

function display(value: number | null, unit: Unit, axis = false) {
  if (value === null) return "—";
  if (unit === "percent") return `${number.format(value * 100)}%`;
  if (unit === "hours") return `${number.format(value)}시간`;
  if (unit === "tokens") return (axis ? compact : integer).format(value);
  return number.format(value);
}

function tokenScope(period: Period) {
  const { token_sources: sources } = period;
  if (sources.length === 1) return sources[0] === "claude" ? "Claude만" : "Codex만";
  return sources.length ? "Claude · Codex" : "";
}

function sample(period: Period, series: Series) {
  const n = period.values[series.key].n;
  if (series.key === "tokens_per_ticket") {
    return `티켓 ${n}/${period.values.intervention_free.n}개${tokenScope(period) ? ` · ${tokenScope(period)}` : ""}`;
  }
  if (series.sample === "실행") return `실행 ${integer.format(n)}/${integer.format(period.execution_count)}개`;
  if (!series.sample && period.observed_until) {
    const observed = (Date.parse(period.observed_until) - Date.parse(period.start)) / 86_400_000;
    return `관측 ${observed < 0.1 ? "0.1일 미만" : `${number.format(observed)}일`}`;
  }
  return series.sample ? `${series.sample} ${integer.format(n)}개` : "";
}

function Trend({ chart, report, selected, onSelect }: {
  chart: Chart; report: Report; selected: number | null; onSelect: (id: number) => void;
}) {
  const left = 59, right = 478;
  const weeks = report.weeks;
  const start = Date.parse(weeks[0].start);
  const end = Date.parse(weeks[weeks.length - 1].end);
  const x = (at: number) => left + (right - left) * (at - start) / (end - start);
  const lastInLane: number[] = [];
  const markers = [...report.markers].reverse().filter((m) => Date.parse(m.at) >= start && Date.parse(m.at) < end)
    .map((m) => {
      const line = x(Date.parse(m.at));
      const label = Array.from(m.label).length > 15 ? `${Array.from(m.label).slice(0, 14).join("")}…` : m.label;
      const width = label.length * 11 + 7;
      const labelX = Math.min(line + 4, right - width);
      const available = lastInLane.findIndex((edge) => edge + 4 < labelX);
      const lane = available < 0 ? lastInLane.length : available;
      lastInLane[lane] = labelX + width;
      return { ...m, line, labelX, lane, shortLabel: label };
    });
  const top = 15 + lastInLane.length * 15, bottom = top + 111;
  const unit = chart.series[0].unit;
  const maxValue = Math.max(
    unit === "count" || unit === "tokens" ? 2 : 1,
    ...chart.series.flatMap((s) => weeks.map((w) => w.values[s.key].value ?? 0)),
  );
  const step = 10 ** Math.floor(Math.log10(maxValue / 2));
  const max = unit === "percent" ? 1 : Math.ceil(maxValue / (step * 2)) * step * 2;
  const y = (value: number) => bottom - (bottom - top) * value / max;
  const hasData = chart.series.some((s) => weeks.some((w) => w.values[s.key].value != null));
  return (
    <Card className="min-w-0 p-4">
      <h3 className="text-sm font-bold text-text-main">{chart.title}</h3>
      <div className="overflow-x-auto">
        <svg viewBox={`0 0 500 ${bottom + 30}`} className="mt-3 w-full min-w-[290px] overflow-visible" aria-label={chart.title}>
          {[max, max / 2, 0].map((tick) => (
            <g key={tick} className="fill-text-muted text-[11px]">
              <line x1={left} x2={right} y1={y(tick)} y2={y(tick)} className="stroke-border" />
              <text x={left - 7} y={y(tick) + 4} textAnchor="end">{display(tick, unit, true)}</text>
            </g>
          ))}
          {weeks.map((w, i) => (i % 3 === 0 || i === weeks.length - 1) &&
            <text key={w.start} x={x(Date.parse(w.start))} y={bottom + 19} className="fill-text-muted text-[11px]">
              {day(w.start)}
            </text>)}
          {!hasData && <text x={(left + right) / 2} y={(top + bottom) / 2 - 7} textAnchor="middle"
            className="fill-text-muted text-[13px]">기록이 없어요</text>}
          {markers.map((m) => (
            <g key={m.id} className={cn("cursor-pointer text-[11px]", selected === m.id ? "fill-primary" : "fill-text-muted")}
              onClick={() => onSelect(m.id)}>
              <title>{m.label} · {markerDate.format(new Date(m.at))} UTC</title>
              <line x1={m.line} x2={m.line} y1={m.lane * 15 + 13} y2={bottom}
                className={selected === m.id ? "stroke-primary" : "stroke-grey-400"} strokeDasharray="4 3" />
              <text x={m.labelX} y={m.lane * 15 + 10}>{m.shortLabel}</text>
            </g>
          ))}
          {chart.series.map((s) => {
            let previous: { x: number; y: number } | null = null;
            return <g key={s.key} className={palette[s.color ?? 0]}>
              {weeks.map((w) => {
                const value = w.values[s.key].value;
                if (value === null) { previous = null; return null; }
                const point = { x: x((Date.parse(w.start) + Date.parse(w.observed_until ?? w.end)) / 2), y: y(value) };
                const preceding = previous;
                previous = point;
                return <g key={w.start}>
                  {preceding && <line x1={preceding.x} x2={point.x} y1={preceding.y} y2={point.y} strokeWidth={2} />}
                  <circle cx={point.x} cy={point.y} r={3}>
                    <title>{day(w.start)} 주 · {s.label} {display(value, s.unit)} {sample(w, s)}</title>
                  </circle>
                </g>;
              })}
            </g>;
          })}
        </svg>
      </div>
      {chart.series.length > 1 && (
        <ul className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-text-muted">
          {chart.series.map((s) => <li key={s.key} className="flex items-center gap-1">
            <svg width="14" height="9" aria-hidden className={palette[s.color ?? 0]}><line x1="1" x2="13" y1="4" y2="4" strokeWidth="2" /></svg>
            {s.label}
          </li>)}
        </ul>
      )}
    </Card>
  );
}

function Comparison({ before, after, complete }: NonNullable<Report["comparison"]>) {
  const span = (p: Period) => `${markerDate.format(new Date(p.start))} ~ ${markerDate.format(new Date(p.end))}`;
  return (
    <div className="mt-4 overflow-x-auto">
      <table className="w-full min-w-[460px] text-left text-sm">
        <thead className="border-b border-border text-text-muted">
          <tr>
            <th scope="col" className="py-2 pr-3">지표</th>
            <th scope="col" className="w-[33%] py-2 pr-3">
              이전 <span className="block text-xs font-normal">{span(before)}</span>
            </th>
            <th scope="col" className="w-[33%] py-2">
              이후 {!complete && <span className="font-normal">· 관측 중</span>}
              <span className="block text-xs font-normal">{span(after)}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {charts.flatMap((chart) => chart.series.map((s) => (
            <tr key={s.key} className="border-b border-border last:border-b-0">
              <th scope="row" className="py-2 pr-3 font-medium text-text-main">
                {s.unit === "hours" ? `${chart.title} · ${s.label}` : s.label}
              </th>
              {[before, after].map((p) => {
                const { value, n } = p.values[s.key];
                const low = s.unit !== "count" && n < 10;
                const count = sample(p, s);
                return (
                  <td key={p.start} className={cn("py-2 pr-3 tabular-nums", low ? "text-text-muted" : "font-semibold text-text-main")}>
                    {display(value, s.unit)} {count && <span className="block text-xs font-normal text-text-muted">{count}</span>}
                    {s.unit === "tokens" && p.linked_runs > 0 && (
                      <span className="block text-xs font-normal text-text-muted">사용량 기록 {p.token_runs}/{p.linked_runs}개</span>
                    )}
                  </td>
                );
              })}
            </tr>
          )))}
        </tbody>
      </table>
    </div>
  );
}

export function Metrics() {
  const [report, setReport] = useState<Report | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [days, setDays] = useState("14");
  const [appliedDays, setAppliedDays] = useState(14);
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let current = true;
    setError("");
    const query = selected == null ? "" : `?marker=${selected}&days=${appliedDays}`;
    getJson<Report>(`/api/metrics${query}`).then((data) => {
      if (current) setReport(data);
    }).catch((err: Error) => { if (current) setError(err.message); });
    return () => { current = false; };
  }, [selected, appliedDays, reload]);

  async function mark(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const marker = await postJson<Marker>("/api/metrics/mark", { label });
      setReport((prev) => prev && { ...prev, markers: [marker, ...prev.markers], comparison: null });
      setSelected(marker.id);
      setLabel("");
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "시점을 기록하지 못했어요");
    } finally {
      setBusy(false);
    }
  }

  const period = report?.period;
  const select = (id: number | null) => {
    if (selected !== id) setReport((prev) => prev && { ...prev, comparison: null });
    setSelected(id);
  };
  return (
    <div className="mx-auto max-w-7xl space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-text-main">지표</h1>
          <p className="mt-1 text-sm text-text-muted">최근 12주 · 월요일 시작 · UTC</p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" onClick={() => setReload((n) => n + 1)}>새로고침</Button>
          <Button size="sm" onClick={() => setEditing((open) => !open)} aria-expanded={editing}>시점 기록</Button>
        </div>
      </div>
      {editing && (
        <Card className="p-4">
          <form onSubmit={mark} className="flex flex-wrap items-end gap-2">
            <label className="min-w-56 flex-1 text-sm font-medium text-text-main">
              시점 이름
              <Input className="mt-1 w-full" autoFocus required maxLength={120} value={label}
                onChange={(e) => setLabel(e.target.value)} placeholder="예: brief에 screenshot 사용법 추가" />
            </label>
            <Button type="submit" loading={busy} disabled={!label.trim()}>기록</Button>
            <Button variant="secondary" onClick={() => setEditing(false)}>취소</Button>
          </form>
        </Card>
      )}
      {error && <p role="alert" className="text-sm text-danger-700">{error}</p>}
      {!report && !error && <p className="text-sm text-text-muted">지표를 불러오는 중…</p>}
      {period && report && <>
        <div className="grid gap-3 sm:grid-cols-2">
          {production.map((s) => (
            <Card key={s.key} className="p-5">
              <h2 className="text-sm font-semibold text-text-muted">{s.label}</h2>
              <p className="mt-2 text-3xl font-bold tabular-nums text-text-main">{display(period.values[s.key].value, s.unit)}</p>
              <p className="mt-2 text-sm text-text-muted">{sample(period, s)}
                {s.unit === "tokens" && period.linked_runs > 0 && ` · 사용량 기록 ${period.token_runs}/${period.linked_runs}개`}
              </p>
            </Card>
          ))}
        </div>
        {(!report.initialized || report.pending > 0) && (
          <p className="text-sm text-text-muted">
            {report.initialized ? `실행 중 또는 집계 대기 ${integer.format(report.pending)}개` : "첫 tick에서 이전 실행의 사용량 집계를 시작해요."}
          </p>
        )}
        <details className="text-sm text-text-muted">
          <summary className="cursor-pointer font-semibold text-text-main">지표 계산 방법</summary>
          <div className="mt-3 max-w-4xl space-y-2 leading-relaxed">
            <p>티켓은 PR의 첫 프로덕션 확인 시각(<code>prs.prod_at</code>)에 한 번 셉니다. 그 시각까지 BLOCKED로 제출한 <code>ws-set --status todo</code>(과거에는 수신함과 작업 상태), 오너 답장·재개(<code>reply</code>, <code>unblock</code>), 리뷰·검사 override(<code>review_override</code>, <code>check_override</code>), 수동 수정 요청(<code>review-save --verdict changes</code>)이 없으면 무개입으로 집계합니다.</p>
            <p>티켓당 토큰은 첫 프로덕션 확인까지 tp가 시작한 작업·수정·충돌 해결·리뷰 세션의 입력(캐시 포함)과 출력 토큰을 더한 값입니다. 읽을 수 있는 사용량이 있는 티켓으로 평균을 내고, 실행 기록 범위를 함께 표시합니다. 여러 티켓을 처리한 리뷰는 PR별 시작 또는 리뷰 저장 시각으로 사용량을 배분합니다.</p>
            <p>첫 AI 리뷰 승인율은 그 주에 처음 AI 리뷰를 받은 PR 기준입니다. 평균 수정 라운드는 그 주에 리뷰·검사 또는 수정 실행 기록이 있는 PR당 실제로 다시 보낸 라운드 수입니다. 소요 시간은 머지 또는 프로덕션 확인이 일어난 주의 중앙값이며 PR 머지 대상별로 표시합니다.</p>
            <p>도구 호출과 반복 패턴은 읽을 수 있는 실행의 종료 주에 집계합니다. 한 도구 호출에서 <code>tp screenshot --help</code> 또는 <code>tp guide</code>, dev 서버 직접 실행·재시작, <code>sleep</code>·<code>for … in $(seq …)</code>·<code>until</code>을 각각 최대 한 번 셉니다. 오류는 기간 안의 같은 메시지를 하나로 묶습니다. <code>ws-set</code> 없이 종료한 작업 실행과 충돌 수정 실행도 집계합니다.</p>
          </div>
        </details>
        <Card className="p-4">
          <h2 className="text-base font-bold text-text-main">시점 전후 비교</h2>
          <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={(event) => {
            event.preventDefault();
            if (appliedDays !== Number(days)) setReport((prev) => prev && { ...prev, comparison: null });
            setAppliedDays(Number(days));
          }}>
            <div className="min-w-48 flex-1">
              <label htmlFor="metrics-marker" className="text-sm font-medium text-text-main">시점</label>
              <Select id="metrics-marker" className="mt-1 w-full" value={selected ?? ""} onChange={(e) => {
                select(e.target.value ? Number(e.target.value) : null);
                const nextDays = Number(days);
                if (Number.isInteger(nextDays) && nextDays >= 1 && nextDays <= 90) setAppliedDays(nextDays);
              }}>
                <option value="">비교할 시점 선택</option>
                {report.markers.map((m) => <option key={m.id} value={m.id}>
                  {m.label} · {markerDate.format(new Date(m.at))} UTC
                </option>)}
              </Select>
            </div>
            <label className="w-24 text-sm font-medium text-text-main">
              전후 각 일수
              <Input className="mt-1 w-full" type="number" required min={1} max={90} value={days} onChange={(e) => setDays(e.target.value)} />
            </label>
            <Button type="submit" variant="secondary" disabled={selected === null}>기간 적용</Button>
          </form>
          {report.comparison && <>
            <p className="mt-3 text-xs text-text-muted">UTC · 표본이 10개 미만인 비율과 평균·중앙값은 흐리게 표시합니다.
              {!report.comparison.complete && " 이후 기간이 끝날 때까지 양쪽을 관측한 만큼만 비교합니다."}
            </p>
            <Comparison {...report.comparison} />
          </>}
          {selected !== null && !report.comparison && !error && <p className="mt-3 text-sm text-text-muted">비교를 불러오는 중…</p>}
          {report.markers.length === 0 && <p className="mt-3 text-sm text-text-muted">시점 기록 버튼 또는 <code>tp mark</code>로 비교할 시점을 추가하세요.</p>}
        </Card>
        <section aria-labelledby="metrics-trends">
          <h2 id="metrics-trends" className="mb-3 text-base font-bold text-text-main">주간 추이</h2>
          <div className="grid gap-3 lg:grid-cols-2">
            {charts.map((chart) => <Trend key={chart.title} chart={chart} report={report} selected={selected} onSelect={(id) => {
              select(id);
              document.getElementById("metrics-marker")?.focus();
            }} />)}
          </div>
        </section>
      </>}
    </div>
  );
}
