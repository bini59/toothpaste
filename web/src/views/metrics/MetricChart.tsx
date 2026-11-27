import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { Select } from "../../components/Select";
import { useThemeVersion } from "../../theme/theme";
import { Card } from "../../ui";

export type ChartPoint = { at: number; tick: string; label: string; values: (number | null)[]; notes?: string[] };
type ChartSeries = { label: string; format: (value: number | null) => string };
type MarkerLine = { at: number; active: boolean };
const colors = ["primary", "secondary", "success-700", "warning-700"];
const integerIncrements = Array.from({ length: 12 }, (_, power) => [1, 2, 5].map((n) => n * 10 ** power)).flat();

export function MetricChart({ title, points, series, start, end, axis, percent, integerTicks, bars, markers,
  periodLabel = "차트 집계 구간" }: {
  title: string; points: ChartPoint[]; series: ChartSeries[]; start: number; end: number;
  axis: (value: number) => string; percent?: boolean; integerTicks?: boolean; bars?: boolean; markers?: MarkerLine[]; periodLabel?: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  const theme = useThemeVersion();
  const [chosen, setChosen] = useState<number | null>(null);
  const point = points.find((p) => p.at === chosen) ?? points.at(-1);
  const hasData = points.some((p) => p.values.some((v) => v !== null));
  const showPlot = hasData || !!markers?.length;

  useEffect(() => {
    const el = container.current;
    if (!el || !showPlot) return;
    const style = getComputedStyle(el);
    const color = (name: string) => style.getPropertyValue(`--color-${name}`).trim();
    const font = `11px ${style.fontFamily}`;
    const data: uPlot.AlignedData = [points.map((p) => p.at), ...series.map((_, i) => points.map((p) => p.values[i]))];
    const ticks = new Map(points.map((p) => [p.at, p.tick]));
    let plot: uPlot | undefined;
    const resize = new ResizeObserver(([entry]) => {
      const width = Math.floor(entry.contentRect.width);
      const height = Math.floor(entry.contentRect.height);
      if (width < 100 || height < 70) return;
      if (plot) {
        plot.setSize({ width, height });
        return;
      }
      plot = new uPlot({
        width, height, padding: [10, 20, 0, 0],
        legend: { show: false },
        cursor: { drag: { x: false, y: false }, points: { show: false } },
        scales: {
          x: { time: false, range: [start, end] },
          y: { range: (_u, min, max) => percent ? [0, 1]
            : uPlot.rangeNum(Math.min(0, min || 0), Math.max(min < 0 ? 0 : 1, max || 0), 0.1, true) },
        },
        axes: [
          { stroke: color("text-muted"), font, size: 27, grid: { show: false }, ticks: { show: false },
            splits: (u) => {
              const available = Math.max(2, Math.floor((u.width - 65) / 65));
              const step = Math.max(1, Math.ceil((points.length - 1) / (available - 1)));
              return points.filter((_, i) => (points.length - 1 - i) % step === 0).map((p) => p.at);
            },
            values: (_u, values) => values.map((v) => ticks.get(v) ?? "") },
          { stroke: color("text-muted"), font, size: 63, grid: { stroke: color("border"), width: 1 }, ticks: { show: false },
            incrs: integerTicks ? integerIncrements : undefined, values: (_u, values) => values.map(axis) },
        ],
        series: [{}, ...series.map((s, i): uPlot.Series => ({
          label: s.label, stroke: color(colors[i]), width: 2, spanGaps: false,
          points: { show: !bars, size: 5 },
          ...(bars ? { fill: color(colors[i]), paths: uPlot.paths.bars!({ size: [0.7, 36] }) } : {}),
        }))],
        hooks: {
          draw: [(u) => {
            const { ctx, bbox } = u;
            ctx.save();
            for (const mark of markers ?? []) {
              const x = u.valToPos(mark.at, "x", true);
              ctx.setLineDash([4, 3]);
              ctx.strokeStyle = color(mark.active ? "primary" : "grey-400");
              ctx.beginPath();
              ctx.moveTo(x, bbox.top);
              ctx.lineTo(x, bbox.top + bbox.height);
              ctx.stroke();
            }
            ctx.restore();
          }],
          ready: [(u) => {
            u.over.addEventListener("click", () => {
              const index = u.cursor.idx;
              if (index != null) setChosen(points[index].at);
            });
          }],
        },
      }, data, el);
    });
    resize.observe(el);
    return () => { resize.disconnect(); plot?.destroy(); };
  }, [axis, bars, end, integerTicks, markers, percent, points, series, showPlot, start, theme]);

  return (
    <Card className="flex min-h-0 min-w-0 flex-1 flex-col gap-2 p-3 sm:p-4">
      <h3 className="text-sm font-bold text-text-main">{title}</h3>
      <div className="relative min-h-24 flex-1">
        <div ref={container} aria-hidden="true" className="absolute inset-0 overflow-hidden" />
        {!hasData && <p className="pointer-events-none absolute inset-0 grid place-items-center text-sm text-text-muted">기록이 없어요</p>}
      </div>
      {point && (
        <div className="shrink-0 border-t border-border pt-2">
          <Select aria-label={`${title} · ${periodLabel}`} size="sm" className="max-w-full"
            value={point.at} onChange={(e) => setChosen(Number(e.target.value))}>
            {points.map((p) => <option key={p.at} value={p.at}>{p.label}</option>)}
          </Select>
          <ul aria-live="polite" className="mt-2 flex min-h-9 flex-wrap content-start gap-x-4 gap-y-1 text-xs text-text-muted">
            {series.map((s, i) => (
              <li key={s.label} className="flex items-baseline gap-1.5">
                <span aria-hidden="true" className="inline-block size-2 shrink-0 rounded-full"
                  style={{ backgroundColor: `var(--color-${colors[i]})` }} />
                {series.length > 1 && <span>{s.label}</span>}
                <strong className="tabular-nums text-text-main">{s.format(point.values[i])}</strong>
                {point.notes?.[i] && <span>· {point.notes[i]}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
