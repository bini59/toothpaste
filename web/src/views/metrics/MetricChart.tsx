import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { Select } from "../../components/Select";
import { useThemeVersion } from "../../theme/theme";
import { Button, Card, cn } from "../../ui";

export type ChartPoint = { at: number; tick: string; label: string; values: (number | null)[]; notes?: string[] };
type ChartSeries = { label: string; format: (value: number | null) => string };
type MarkerLine = { id: number; at: number; label: string; date: string; details: string };
const colors = ["primary", "secondary", "success-700", "warning-700"];
const integerIncrements = Array.from({ length: 12 }, (_, power) => [1, 2, 5].map((n) => n * 10 ** power)).flat();

function layoutMarkers(plot: uPlot, markers: MarkerLine[], fontFamily: string) {
  const { offsetLeft, clientWidth } = plot.over;
  const maxWidth = Math.min(172, Math.max(108, clientWidth / 3), clientWidth);
  const rowHeight = 32;
  const laneLeft: number[] = [];
  plot.ctx.save();
  plot.ctx.font = `500 12px ${fontFamily}`;
  const positions = markers.map((marker) => {
    const textWidth = plot.ctx.measureText(marker.label).width + plot.ctx.measureText(marker.date).width;
    // Leave room for button padding, the date gap and differences in font metrics.
    const width = Math.min(Math.ceil(textWidth) + 26, maxWidth);
    const x = plot.valToPos(marker.at, "x");
    const left = offsetLeft + Math.max(0, Math.min(x - width / 2, clientWidth - width));
    return { marker, width, left };
  });
  plot.ctx.restore();
  const labels = positions.sort((a, b) => b.left - a.left).map((position) => {
    const freeLane = laneLeft.findIndex((edge) => position.left + position.width + 4 <= edge);
    const lane = freeLane === -1 ? laneLeft.length : freeLane;
    laneLeft[lane] = position.left;
    return { ...position, top: lane * rowHeight + 2 };
  });
  return { labels, height: laneLeft.length * rowHeight };
}

export function MetricChart({ title, points, series, start, end, axis, percent, integerTicks, bars, markers,
  selectedMarker, onMarkerSelect, periodLabel = "차트 집계 구간" }: {
  title: string; points: ChartPoint[]; series: ChartSeries[]; start: number; end: number;
  axis: (value: number) => string; percent?: boolean; integerTicks?: boolean; bars?: boolean; periodLabel?: string;
  markers?: MarkerLine[]; selectedMarker?: number | null; onMarkerSelect?: (id: number) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const theme = useThemeVersion();
  const [chosen, setChosen] = useState<number | null>(null);
  const [markerLayout, setMarkerLayout] = useState<ReturnType<typeof layoutMarkers>>();
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
          setSize: [(u) => {
            if (markers?.length) setMarkerLayout(layoutMarkers(u, markers, style.fontFamily));
          }],
          draw: [(u) => {
            const { ctx, bbox } = u;
            ctx.save();
            for (const mark of markers ?? []) {
              const x = u.valToPos(mark.at, "x", true);
              ctx.setLineDash([4, 3]);
              ctx.strokeStyle = color(mark.id === selectedMarker ? "primary" : "grey-400");
              ctx.beginPath();
              ctx.moveTo(x, bbox.top);
              ctx.lineTo(x, bbox.top + bbox.height);
              ctx.stroke();
            }
            ctx.restore();
          }],
          ready: [(u) => {
            function markerAt(event: MouseEvent) {
              if (!markers?.length) return;
              const x = event.clientX - u.over.getBoundingClientRect().left;
              let nearest: MarkerLine | undefined;
              let distance = 7;
              for (const marker of markers) {
                const gap = Math.abs(x - u.valToPos(marker.at, "x"));
                if (gap < distance) {
                  nearest = marker;
                  distance = gap;
                }
              }
              return nearest;
            }
            if (markers?.length) {
              u.over.addEventListener("pointermove", (event) => {
                const marker = markerAt(event);
                u.over.style.cursor = marker ? "pointer" : "";
                u.over.title = marker?.details ?? "";
              });
            }
            u.over.addEventListener("click", (event) => {
              const marker = markerAt(event);
              if (marker && onMarkerSelect) {
                onMarkerSelect(marker.id);
                return;
              }
              const index = u.cursor.idx;
              if (index != null) setChosen(points[index].at);
            });
          }],
        },
      }, data, el);
    });
    resize.observe(el);
    return () => { resize.disconnect(); plot?.destroy(); };
  }, [axis, bars, end, integerTicks, markers, onMarkerSelect, percent, points, selectedMarker, series, showPlot, start, theme]);

  return (
    <Card className="flex min-h-0 min-w-0 flex-1 flex-col gap-2 p-3 sm:p-4">
      <h3 className="text-sm font-bold text-text-main">{title}</h3>
      {!!markers?.length && markerLayout && onMarkerSelect && (
        <div role="group" aria-label="차트 시점" className="relative max-h-16 shrink-0 overflow-y-auto"
          style={{ height: markerLayout.height }}>
          {markerLayout.labels.map(({ marker, width, left, top }) => (
            <Button key={marker.id} size="sm" variant="secondary" aria-label={marker.details} title={marker.details}
              aria-pressed={selectedMarker === marker.id} onClick={() => onMarkerSelect(marker.id)}
              className={cn("absolute h-7 justify-between gap-1 px-1.5",
                selectedMarker === marker.id && "border-primary bg-primary-50 text-primary")}
              style={{ left, top, width }}>
              <span className="truncate">{marker.label}</span>
              <span className="shrink-0 whitespace-nowrap tabular-nums text-text-muted">{marker.date}</span>
            </Button>
          ))}
        </div>
      )}
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
          <ul aria-live="polite" className={cn("mt-2 flex flex-wrap content-start gap-x-4 gap-y-1 text-xs text-text-muted",
            series.length > 1 && "min-h-9")}>
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
