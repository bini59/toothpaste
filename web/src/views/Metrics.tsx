import { useEffect, useMemo, useState, type FormEvent } from "react";
import { getJson, postJson } from "../api";
import { Select } from "../components/Select";
import { Button, Card, Input, cn } from "../ui";
import { ChatCompactions } from "./ChatCompactions";
import { MetricChart } from "./metrics/MetricChart";
import { MetricsDialog, MetricsStatus, MetricsTable } from "./metrics/MetricsTable";
import { Method } from "./metrics/Method";
import { charts, dateRange, day, display, integer, isPartial, markerDate, production, sample,
  type Chart, type Marker, type Period, type Report, type Series } from "./metrics/weeklyData";

const sections = [
  { key: "overview", label: "개요" },
  { key: "weekly", label: "주간 추이" },
  { key: "comparison", label: "시점 비교" },
  { key: "chat-compactions", label: "채팅 압축" },
] as const;
type Section = (typeof sections)[number]["key"];
type Comparison = NonNullable<Report["comparison"]>;
const comparisonRows = charts.flatMap((chart) => chart.series.map((series) => ({
  series, label: series.unit === "hours" ? `${chart.title} · ${series.label}` : series.label,
})));

export function Metrics({ sub }: { sub: string }) {
  const section = sections.find((item) => item.key === sub)?.key ?? "overview";
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
      <h1 className="shrink-0 text-xl font-bold text-text-main sm:text-2xl">지표</h1>
      <nav aria-label="지표 탭" className="grid shrink-0 grid-cols-4 border-b border-border sm:flex sm:gap-2">
        {sections.map(({ key, label }) => (
          <a key={key} href={key === "overview" ? "#metrics" : `#metrics/${key}`}
            aria-current={key === section ? "page" : undefined}
            className={cn("whitespace-nowrap border-b-2 px-1 py-2.5 text-center text-sm font-semibold sm:px-4",
              key === section ? "border-primary text-primary" : "border-transparent text-text-muted hover:text-text-main")}>
            {label}
          </a>
        ))}
      </nav>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-y-auto">
        {section === "chat-compactions" ? <ChatCompactions /> : <PipelineMetrics section={section} />}
      </div>
    </div>
  );
}

function PipelineMetrics({ section }: { section: Exclude<Section, "chat-compactions"> }) {
  const [report, setReport] = useState<Report | null>(null);
  const [reportError, setReportError] = useState("");
  const [reload, setReload] = useState(0);
  const [overviewIndex, setOverviewIndex] = useState(0);
  const [chartIndex, setChartIndex] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const [days, setDays] = useState("14");
  const [appliedDays, setAppliedDays] = useState(14);
  const [result, setResult] = useState<{ request: string; comparison?: Comparison; error?: string }>();
  const [dialog, setDialog] = useState<"mark" | "method" | null>(null);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [markError, setMarkError] = useState("");
  const query = selected === null ? "" : `?marker=${selected}&days=${appliedDays}`;
  const request = `${query}#${reload}`;
  const activeResult = result?.request === request ? result : undefined;

  useEffect(() => {
    let current = true;
    getJson<Report>("/api/metrics").then((data) => {
      if (current) { setReport(data); setReportError(""); }
    }).catch((err: Error) => { if (current) setReportError(err.message); });
    return () => { current = false; };
  }, [reload]);

  useEffect(() => {
    if (section !== "comparison" || !query || activeResult) return;
    let current = true;
    getJson<Report>(`/api/metrics${query}`).then((data) => {
      if (current) setResult(data.comparison ? { request, comparison: data.comparison } : { request, error: "비교 결과가 없어요." });
    }).catch((err: Error) => { if (current) setResult({ request, error: err.message }); });
    return () => { current = false; };
  }, [section, query, request, activeResult]);

  function refresh() {
    setReportError("");
    setReload((value) => value + 1);
  }

  function chooseMarker(value: string) {
    setSelected(value ? Number(value) : null);
    const nextDays = Number(days);
    if (Number.isInteger(nextDays) && nextDays >= 1 && nextDays <= 90) setAppliedDays(nextDays);
  }

  async function mark(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const from = location.hash;
    setBusy(true);
    setMarkError("");
    try {
      const marker = await postJson<Marker>("/api/metrics/mark", { label });
      setReport((prev) => prev && { ...prev, markers: [marker, ...prev.markers] });
      setSelected(marker.id);
      setLabel("");
      setDialog(null);
      if (location.hash === from) location.hash = "#metrics/comparison";
      refresh();
    } catch (err) {
      setMarkError(err instanceof Error ? err.message : "시점을 기록하지 못했어요");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium text-text-muted sm:text-sm">
          {section === "comparison" ? "시점 전후 비교 · UTC" : "최근 12주 · UTC 월요일 시작"}
        </p>
        <div className="flex flex-wrap gap-1.5">
          <Button size="sm" variant="secondary" onClick={() => setDialog("method")}>계산 방법</Button>
          <Button size="sm" variant="secondary" onClick={refresh}>새로고침</Button>
          <Button size="sm" onClick={() => { setMarkError(""); setDialog("mark"); }}>시점 기록</Button>
        </div>
      </div>
      {reportError && <MetricsStatus error text={`지표를 불러오지 못했어요. ${reportError}`} />}
      {!report && !reportError && <MetricsStatus text="지표를 불러오는 중…" />}
      {report && section === "overview" && (
        <>
          <Select size="sm" className="w-full shrink-0 sm:hidden" aria-label="개요 핵심 지표" value={overviewIndex}
            onChange={(e) => setOverviewIndex(Number(e.target.value))}>
            {production.map((s, i) => <option key={s.key} value={i}>{s.label}</option>)}
          </Select>
          <div className="grid shrink-0 gap-3 sm:grid-cols-2">
            {production.map((s, i) => (
              <Card key={s.key} className={cn("min-w-0 p-3 sm:block sm:p-4", overviewIndex !== i && "hidden")}>
                <h2 className="text-sm font-semibold text-text-muted">{s.label}</h2>
                <p className="mt-1 text-2xl font-bold tabular-nums text-text-main">{display(report.period.values[s.key].value, s.unit)}
                  {s.unit === "tokens" && <span className="ml-1 text-xs font-medium text-text-muted">토큰</span>}
                </p>
                <p className="mt-1 text-xs text-text-muted">{dateRange(report.period.start, report.period.end)}</p>
                <p className="mt-1 text-xs text-text-muted">{sample(report.period, s)}
                  {s.unit === "tokens" && report.period.linked_runs > 0 && ` · 사용량 기록 ${report.period.token_runs}/${report.period.linked_runs}개`}
                </p>
              </Card>
            ))}
          </div>
          {(!report.initialized || report.pending > 0) && <p className="shrink-0 text-xs text-text-muted">
            {report.initialized ? `실행 중 또는 집계 대기 ${integer.format(report.pending)}개` : "첫 tick에서 이전 실행의 사용량 집계를 시작해요."}
          </p>}
          <div className="grid min-h-[218px] min-w-0 flex-1 gap-3 sm:grid-cols-2">
            {charts.slice(0, production.length).map((chart, i) => (
              <div key={chart.title} className={cn("min-h-0 min-w-0 flex-col sm:flex", overviewIndex === i ? "flex" : "hidden")}>
                <WeeklyChart chart={chart} report={report} />
              </div>
            ))}
          </div>
        </>
      )}
      {report && section === "weekly" && (
        <>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <Select aria-label="주간 추이 지표" size="sm" className="w-full sm:max-w-64" value={chartIndex}
              onChange={(e) => setChartIndex(Number(e.target.value))}>
              {charts.map((chart, i) => <option key={chart.title} value={i}>{chart.title}</option>)}
            </Select>
            <div className="flex min-w-0 flex-1 gap-2">
              <MarkerSelect markers={report.markers} selected={selected} onChange={chooseMarker} />
              <Button size="sm" variant="secondary" disabled={selected === null}
                onClick={() => { location.hash = "#metrics/comparison"; }}>비교 열기</Button>
            </div>
          </div>
          {charts[chartIndex].series.some((s) => s.key === "missing_ws_set") && (
            <p className="shrink-0 text-xs text-text-muted">과거 실행은 종료·시간 초과 이벤트가 남은 누락만 확인할 수 있어요.</p>
          )}
          <div className="flex min-h-[218px] min-w-0 flex-1 flex-col">
            <WeeklyChart key={chartIndex} chart={charts[chartIndex]} report={report} selected={selected} />
          </div>
        </>
      )}
      {report && section === "comparison" && (
        <>
          <form className="flex shrink-0 flex-wrap items-end gap-x-2 gap-y-1" onSubmit={(event) => {
            event.preventDefault();
            setAppliedDays(Number(days));
          }}>
            <div className="min-w-0 basis-full sm:flex-1">
              <label htmlFor="metrics-marker" className="block text-xs font-medium text-text-main">시점</label>
              <MarkerSelect markers={report.markers} selected={selected} onChange={chooseMarker} id="metrics-marker" />
            </div>
            <label className="w-24 text-xs font-medium text-text-main">
              전후 각 일수
              <Input className="mt-1 w-full" type="number" required min={1} max={90} value={days} onChange={(e) => setDays(e.target.value)} />
            </label>
            <Button type="submit" variant="secondary" disabled={selected === null}>기간 적용</Button>
          </form>
          {selected === null ? <MetricsStatus text={report.markers.length ? "비교할 시점을 선택하세요."
            : "시점 기록 버튼 또는 tp mark로 비교할 시점을 추가하세요."} />
            : activeResult?.comparison ? <PeriodComparison key={request} comparison={activeResult.comparison} />
            : <MetricsStatus error={!!activeResult?.error}
                text={activeResult?.error ? `비교를 불러오지 못했어요. ${activeResult.error}` : "비교를 불러오는 중…"} />}
        </>
      )}
      {dialog === "method" && <MetricsDialog title="지표 계산 방법" onClose={() => setDialog(null)}><Method /></MetricsDialog>}
      {dialog === "mark" && (
        <MetricsDialog title="시점 기록" onClose={() => setDialog(null)}>
          <form onSubmit={mark} className="space-y-3">
            <label className="block text-sm font-medium">
              시점 이름
              <Input className="mt-1 w-full" autoFocus required maxLength={120} value={label}
                onChange={(e) => setLabel(e.target.value)} placeholder="예: brief에 screenshot 사용법 추가" />
            </label>
            {markError && <p role="alert" className="text-sm text-danger-700">{markError}</p>}
            <Button type="submit" loading={busy} disabled={!label.trim()}>기록</Button>
          </form>
        </MetricsDialog>
      )}
    </>
  );
}

function MarkerSelect({ markers, selected, onChange, id }: {
  markers: Marker[]; selected: number | null; onChange: (value: string) => void; id?: string;
}) {
  return (
    <Select id={id} aria-label={id ? undefined : "버전 또는 수동 시점"} size={id ? "md" : "sm"}
      className={cn("w-full min-w-0", id && "mt-1")} value={selected ?? ""} onChange={(e) => onChange(e.target.value)}>
      <option value="">비교할 시점 선택</option>
      {markers.map((m) => (
        <option key={m.id} value={m.id}>
          {m.kind === "version" ? "버전" : "수동"} · {m.label} · {markerDate.format(new Date(m.at))} UTC
        </option>
      ))}
    </Select>
  );
}

function WeeklyChart({ chart, report, selected }: { chart: Chart; report: Report; selected?: number | null }) {
  const { weeks, markers } = report;
  const start = Date.parse(weeks[0].start);
  const end = Date.parse(weeks[weeks.length - 1].end);
  const unit = chart.series[0].unit;
  const points = useMemo(() => weeks.map((week) => ({
    at: (Date.parse(week.start) + Date.parse(week.observed_until ?? week.end)) / 2,
    tick: day(week.start),
    label: `${dateRange(week.start, week.end)}${isPartial(week) ? " · 관측 중" : ""}`,
    values: chart.series.map((s) => week.values[s.key].value),
    notes: chart.series.map((s) => sample(week, s)),
  })), [weeks, chart]);
  const markerLines = useMemo(() => markers.map((m) => ({
    at: Date.parse(m.at), active: m.id === selected,
  })).filter((m) => m.at >= start && m.at < end), [markers, selected, start, end]);
  const series = useMemo(() => chart.series.map((s) => ({
    label: s.label, format: (value: number | null) => display(value, s.unit) + (s.unit === "tokens" && value !== null ? " 토큰" : ""),
  })), [chart]);
  const axis = useMemo(() => (value: number) => display(value, unit, true), [unit]);
  return <MetricChart title={chart.title} start={start} end={end} points={points} series={series}
    axis={axis} percent={unit === "percent"} integerTicks={unit === "count"} markers={markerLines} periodLabel="주간 값" />;
}

function PeriodComparison({ comparison: { before, after, complete } }: { comparison: Comparison }) {
  const [detail, setDetail] = useState<(typeof comparisonRows)[number] | null>(null);
  const periods = [{ label: "이전", period: before }, { label: "이후", period: after }];
  return (
    <>
      <div className="grid shrink-0 grid-cols-2 gap-2 text-xs text-text-muted">
        {periods.map(({ label, period }) => (
          <Card key={label} className="min-w-0 px-2 py-1.5 sm:px-3">
            <p className="font-semibold text-text-main">{label}{label === "이후" && !complete && " · 관측 중"}</p>
            <p>{markerDate.format(new Date(period.start))} ~ {markerDate.format(new Date(period.end))}</p>
          </Card>
        ))}
        <p className="col-span-2">표본 10개 미만인 비율·평균·중앙값은 흐리게 표시해요.
          {!complete && " 관측한 만큼 양쪽을 비교해요."}</p>
      </div>
      <MetricsTable label="시점 비교 지표" rows={comparisonRows} rowKey={({ series }) => series.key} columns={[
        { label: "지표", className: "w-2/5 px-2 sm:px-3" },
        { label: "이전", className: "px-1 sm:px-2" },
        { label: "이후", className: "px-1 sm:px-2" },
        { label: "상세", className: "w-16 px-1 sm:w-20 sm:px-2" },
      ]} renderRow={(row) => (
        <>
          <th scope="row" className="px-2 font-medium sm:px-3"><span className="block truncate" title={row.label}>{row.label}</span></th>
          {periods.map(({ label, period }) => <td key={label} className="px-1 sm:px-2">
            <PeriodValue period={period} series={row.series} />
          </td>)}
          <td className="px-1 sm:px-2">
            <Button size="sm" variant="secondary" aria-label={`${row.label} 자세히`} onClick={() => setDetail(row)}>상세</Button>
          </td>
        </>
      )} />
      {detail && <MetricsDialog title={detail.label} onClose={() => setDetail(null)}>
        <dl className="grid grid-cols-2 gap-3 text-sm">
          {periods.map(({ label, period }) => <div key={label} className="min-w-0">
            <dt className="mb-1 font-semibold">{label}{label === "이후" && !complete && " · 관측 중"}</dt>
            <dd><PeriodValue period={period} series={detail.series} expanded /></dd>
          </div>)}
        </dl>
      </MetricsDialog>}
    </>
  );
}

function PeriodValue({ period, series, expanded }: { period: Period; series: Series; expanded?: boolean }) {
  const { value, n } = period.values[series.key];
  const count = sample(period, series);
  const low = series.unit !== "count" && n < 10;
  return (
    <>
      <p className={cn("tabular-nums", expanded ? "[overflow-wrap:anywhere]" : "truncate", low ? "text-text-muted" : "font-semibold text-text-main")}>
        {display(value, series.unit)}{series.unit === "tokens" && value !== null && " 토큰"}
      </p>
      {count && <p className={cn("text-xs text-text-muted", !expanded && "hidden truncate sm:block")}>{count}</p>}
      {expanded && series.unit === "tokens" && period.linked_runs > 0 &&
        <p className="text-xs text-text-muted">사용량 기록 {period.token_runs}/{period.linked_runs}개</p>}
    </>
  );
}
