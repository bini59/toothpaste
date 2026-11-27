import { useEffect, useMemo, useState } from "react";
import { getJson } from "../api";
import { Select } from "../components/Select";
import { Badge, Button, Card, cn } from "../ui";
import { MetricChart } from "./metrics/MetricChart";
import { MetricsDialog, MetricsStatus, MetricsTable } from "./metrics/MetricsTable";
import { compactionTrend, fmt, periods, sum, tokens, type Compaction, type CompactionPeriod } from "./metrics/compactionData";
import { display } from "./metrics/weeklyData";

const chartViews = ["압축 횟수", "토큰 추이"];
const countSeries = [{ label: "압축 횟수", format: (n: number | null) => `${tokens(n)}회` }];
const tokenSeries = [
  { label: "압축 비용", format: (n: number | null) => tokens(n) },
  { label: "추정 순절감량", format: (n: number | null) => tokens(n, true) },
];
const countAxis = (n: number) => display(n, "count", true);
const tokenAxis = (n: number) => display(n, "tokens", true);
const date = new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "short" });
const title = (row: Compaction) => row.title || "제목 없는 대화";

export function ChatCompactions() {
  const [days, setDays] = useState<CompactionPeriod>("30");
  const [view, setView] = useState<"trends" | "records">("trends");
  const [summary, setSummary] = useState(0);
  const [chart, setChart] = useState(0);
  const [reload, setReload] = useState(0);
  const [result, setResult] = useState<{ rows: Compaction[]; at: number } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const at = Date.now();
    getJson<{ rows: Compaction[] }>(`/api/chat/compactions?days=${days}`).then(
      (data) => { if (active) setResult({ rows: data.rows, at }); },
      (err: Error) => { if (active) setError(err.message); },
    );
    return () => { active = false; };
  }, [days, reload]);

  function changePeriod(period: CompactionPeriod) {
    if (period === days) return;
    setResult(null);
    setError("");
    setDays(period);
  }

  return (
    <>
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
        <div role="group" aria-label="압축 기간" className="flex flex-wrap gap-1">
          {periods.map(({ value, label }) => (
            <Button key={value} size="sm" variant={days === value ? "primary" : "secondary"}
              aria-pressed={days === value} onClick={() => changePeriod(value)}>{label}</Button>
          ))}
        </div>
        <div role="group" aria-label="압축 보기" className="flex gap-1">
          <Button size="sm" variant={view === "trends" ? "primary" : "secondary"}
            aria-pressed={view === "trends"} onClick={() => setView("trends")}>추이</Button>
          <Button size="sm" variant={view === "records" ? "primary" : "secondary"}
            aria-pressed={view === "records"} onClick={() => setView("records")}>압축 기록</Button>
        </div>
      </div>
      {error ? <>
        <MetricsStatus error text={`압축 기록을 불러오지 못했어요. ${error}`} />
        <Button size="sm" variant="secondary" className="self-start" onClick={() => {
          setError("");
          setReload((n) => n + 1);
        }}>다시 시도</Button>
      </> : !result ? <MetricsStatus text="압축 기록을 불러오는 중…" />
        : <CompactionReport rows={result.rows} at={result.at} period={days} view={view}
            summary={summary} onSummary={setSummary} chart={chart} onChart={setChart} />}
    </>
  );
}

function CompactionReport({ rows, at, period, view, summary, onSummary, chart, onChart }: {
  rows: Compaction[]; at: number; period: CompactionPeriod; view: "trends" | "records";
  summary: number; onSummary: (index: number) => void; chart: number; onChart: (index: number) => void;
}) {
  const trend = useMemo(() => compactionTrend(rows, period, at), [rows, period, at]);
  const noFollowup = rows.filter((row) => !row.failed && !row.continued);
  const failures = rows.filter((row) => row.failed).length;
  const missingCost = rows.filter((row) => row.cost_tokens === null).length;
  const missingEstimate = rows.filter((row) => row.net_savings_tokens === null).length;
  const missingNoFollowup = noFollowup.filter((row) => row.cost_tokens === null).length;
  const cards = [
    { label: "압축 횟수", value: `${fmt.format(rows.length)}회`,
      detail: `성공 ${fmt.format(rows.length - failures)}회 · 실패 ${fmt.format(failures)}회` },
    { label: "압축 비용", value: tokens(sum(rows, "cost_tokens")),
      detail: missingCost ? `토큰 · 비용 미기록 ${fmt.format(missingCost)}회` : "토큰" },
    { label: "추정 순절감량", value: tokens(sum(rows, "net_savings_tokens"), true),
      detail: missingEstimate ? `토큰 · 추정 불가 ${fmt.format(missingEstimate)}회` : "토큰" },
    { label: "후속 대화 없음", value: `${fmt.format(noFollowup.length)}회`,
      detail: `비용 ${tokens(sum(noFollowup, "cost_tokens"))} 토큰${missingNoFollowup ? ` · 미기록 ${fmt.format(missingNoFollowup)}회` : ""}` },
  ];
  const bucket = period === "all" ? "월별" : period === "90" ? "주별" : "일별";

  return (
    <>
      <div className={cn("shrink-0 space-y-2", view === "records" && "hidden lg:block")}>
        <Select aria-label="압축 요약 지표" size="sm" className="w-full lg:hidden" value={summary}
          onChange={(e) => onSummary(Number(e.target.value))}>
          {cards.map((card, i) => <option key={card.label} value={i}>{card.label}</option>)}
        </Select>
        <div className="grid gap-3 lg:grid-cols-4">
          {cards.map((card, i) => (
            <Card key={card.label} className={cn("min-w-0 p-3 lg:block", summary !== i && "hidden")}>
              <h2 className="text-sm font-semibold text-text-muted">{card.label}</h2>
              <p className="mt-1 text-2xl font-bold tabular-nums text-text-main [overflow-wrap:anywhere]">{card.value}</p>
              <p className="mt-1 text-xs text-text-muted">{card.detail}</p>
            </Card>
          ))}
        </div>
      </div>
      {rows.length === 0 ? <MetricsStatus text="이 기간에 완료된 압축이 없어요." />
        : view === "records" ? <CompactionRecords rows={rows} />
        : <>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-text-muted">압축 완료 시각 기준 · {bucket} · UTC</p>
            <Select aria-label="압축 추이 차트" size="sm" className="max-w-full sm:hidden" value={chart}
              onChange={(e) => onChart(Number(e.target.value))}>
              {chartViews.map((label, i) => <option key={label} value={i}>{label}</option>)}
            </Select>
          </div>
          <div className="grid min-h-[218px] min-w-0 flex-1 gap-3 sm:grid-cols-2">
            <div className={cn("min-h-0 min-w-0 flex-col sm:flex", chart === 0 ? "flex" : "hidden")}>
              <MetricChart title="압축 횟수" points={trend.counts} series={countSeries} start={trend.start} end={trend.end} axis={countAxis} integerTicks bars />
            </div>
            <div className={cn("min-h-0 min-w-0 flex-col sm:flex", chart === 1 ? "flex" : "hidden")}>
              <MetricChart title="압축 비용과 추정 순절감량 (토큰)" points={trend.usage} series={tokenSeries} start={trend.start} end={trend.end} axis={tokenAxis} />
            </div>
          </div>
        </>}
    </>
  );
}

function CompactionRecords({ rows }: { rows: Compaction[] }) {
  const [detail, setDetail] = useState<Compaction | null>(null);
  const values = detail && [
    { label: "압축 전", value: tokens(detail.before_context_tokens) },
    { label: "압축 후 첫 턴", value: tokens(detail.first_context_tokens) },
    { label: "첫 턴 절감", value: tokens(detail.first_savings_tokens, true) },
    { label: "압축 비용", value: tokens(detail.cost_tokens) },
    { label: "추정 순절감량", value: tokens(detail.net_savings_tokens, true) },
    ...(detail.turns > 0 ? [{ label: "측정 턴", value: `${fmt.format(detail.measured)}/${fmt.format(detail.turns)}턴` }] : []),
  ];
  return (
    <>
      <p className="shrink-0 text-xs text-text-muted">후속 턴에서 줄어든 입력 컨텍스트의 합에서 압축 비용을 빼요. 단위는 토큰이에요.</p>
      <MetricsTable label="압축 기록" rows={rows} rowKey={(row) => row.id} columns={[
        { label: "대화", className: "px-2 sm:px-3" },
        { label: "상태", className: "w-14 px-1 sm:w-36" },
        { label: "압축 비용", className: "hidden w-24 px-1 text-right sm:table-cell" },
        { label: "추정 순절감량", className: "hidden w-28 px-1 text-right sm:table-cell" },
        { label: "상세", className: "w-16 px-1 sm:w-20 sm:px-2" },
      ]} renderRow={(row) => (
        <>
          <th scope="row" className="px-2 font-medium sm:px-3">
            <span className="block truncate" title={title(row)}>{title(row)}</span>
            <time dateTime={row.finished_at} className="block truncate text-xs font-normal text-text-muted">{date.format(new Date(row.finished_at))}</time>
          </th>
          <td className="px-1">
            <Badge status={row.failed ? "danger" : "success"}>{row.failed ? "실패" : "성공"}</Badge>
            {!row.failed && !row.continued && <span className="ml-1 hidden text-xs text-text-muted sm:inline">후속 대화 없음</span>}
          </td>
          <td className="hidden px-1 text-right sm:table-cell"><span className="block truncate">{tokens(row.cost_tokens)}</span></td>
          <td className="hidden px-1 text-right sm:table-cell"><span className="block truncate">{tokens(row.net_savings_tokens, true)}</span></td>
          <td className="px-1 sm:px-2"><Button size="sm" variant="secondary" aria-label={`${title(row)} 자세히`}
            onClick={() => setDetail(row)}>상세</Button></td>
        </>
      )} />
      {detail && values && <MetricsDialog title={title(detail)} onClose={() => setDetail(null)}>
        <p className="mb-4 text-sm text-text-muted">
          <time dateTime={detail.finished_at}>{date.format(new Date(detail.finished_at))}</time>
          {detail.failed ? " · 실패" : " · 성공"}
          {!detail.failed && !detail.continued && " · 후속 대화 없음"}
        </p>
        <dl className="divide-y divide-border text-sm">
          {values.map(({ label, value }) => (
            <div key={label} className="flex justify-between gap-3 py-2">
              <dt className="text-text-muted">{label}</dt><dd className="text-right font-semibold tabular-nums [overflow-wrap:anywhere]">{value}</dd>
            </div>
          ))}
        </dl>
      </MetricsDialog>}
    </>
  );
}
