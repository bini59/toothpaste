import { Card } from "../ui";
import { openTickets } from "../lib";
import type { ProjectMapRow, State } from "../types";

// role colours: white-safe steps of the theme scales (literal class names so Tailwind emits them)
const ROLE: Record<string, { stroke: string; text: string }> = {
  fe: { stroke: "stroke-info-600", text: "text-info-600" },
  be: { stroke: "stroke-success-700", text: "text-success-700" },
  admin: { stroke: "stroke-warning-700", text: "text-warning-700" },
  app: { stroke: "stroke-secondary-500", text: "text-secondary-500" },
};

export function RepoMap({ s }: { s: State }) {
  const inc = openTickets(s);
  type P = { gid: string; name: string; conf: string | null; note?: string | null; links: ProjectMapRow[] };
  const projs: Record<string, P> = {};
  for (const m of s.project_map) {
    const k = m.project_gid ?? m.project_id; // TP-91: a project without Asana is keyed by its id (no Asana tickets)
    projs[k] ??= { gid: k, name: m.project_name, conf: m.confidence, note: m.note, links: [] };
    if (m.repo) projs[k].links.push(m);
  }
  for (const t of inc) if (t.project_gid && !projs[t.project_gid]) projs[t.project_gid] = { gid: t.project_gid, name: t.project_name || "", conf: "none", links: [] };
  const P = Object.values(projs)
    .map((p) => ({ ...p, open: inc.filter((t) => t.project_gid === p.gid).length, tag: inc.filter((t) => t.project_gid === p.gid && t.has_tag).length }))
    .sort((a, b) => Number(b.links.length > 0) - Number(a.links.length > 0) || b.open - a.open);
  const R = [...s.repos].sort((a, b) => {
    const f = (n: string) => P.findIndex((p) => p.links.some((l) => l.repo === n));
    return f(a.name) - f(b.name);
  });
  const rowH = 46, W = 980, lx = 20, lw = 300, rx = W - 340, rw = 320;
  const h = Math.max(P.length, R.length) * rowH + 30;
  const py = (i: number) => 20 + i * rowH;
  const ry = (i: number) => 20 + i * ((P.length * rowH) / Math.max(R.length, 1));

  return (
    <>
      <div className="mb-4">
        <h1 className="text-xl font-bold text-text-main sm:text-2xl">Asana 프로젝트 → 로컬 레포</h1>
        <p className="mt-1 text-sm text-text-muted">
          굵은 선 = 기본 레포 · 점선 = 확신도 낮음 · 색:{" "}
          {Object.entries(ROLE).map(([k, c]) => (
            <span key={k} className={`mr-1.5 font-semibold ${c.text}`}>
              ■ {k}
            </span>
          ))}
          · 수정:{" "}
          <a href="#projects" className="font-semibold text-primary hover:text-primary-700 hover:underline">
            프로젝트
          </a>{" "}
          (레포를 누르면 그 레포의 설정)
        </p>
      </div>
      <Card className="overflow-x-auto p-3">
        <svg viewBox={`0 0 ${W} ${h}`} width="100%" className="block min-w-[760px] [&_text]:fill-text-main [&_text]:text-xs [&_text]:font-medium">
          {P.flatMap((p, i) =>
            p.links.map((l) => {
              const j = R.findIndex((r) => r.name === l.repo);
              if (j < 0) return null;
              const y1 = py(i) + 18, y2 = ry(j) + 18, x1 = lx + lw, x2 = rx, c = ROLE[l.role || ""]?.stroke ?? "stroke-grey-400";
              return (
                <g key={`${p.gid}-${l.repo}`}>
                  <path d={`M${x1},${y1} C${x1 + 140},${y1} ${x2 - 140},${y2} ${x2},${y2}`} className={c} strokeWidth={l.is_default ? 2.4 : 1.3} fill="none" opacity={0.75} strokeDasharray={p.conf === "low" ? "4 4" : undefined} />
                  <text x={x2 - 8} y={y2 - 4} textAnchor="end" className="!fill-text-muted !text-[11px] !font-normal">{l.role}</text>
                </g>
              );
            }),
          )}
          {P.map((p, i) => (
            <g key={p.gid} opacity={p.links.length ? 1 : 0.55}>
              <rect x={lx} y={py(i)} width={lw} height={36} rx={8} className="fill-grey-50 stroke-grey-200" />
              <text x={lx + 10} y={py(i) + 16}>{p.name}</text>
              <text x={lx + 10} y={py(i) + 30} className="!fill-text-muted !text-[11px] !font-normal">
                미완료 {p.open} · {s.meta.settings.asana_tag} {p.tag} · 확신도 {p.conf || "-"}
                {p.note ? ` · ${p.note.slice(0, 30)}` : ""}
              </text>
            </g>
          ))}
          {R.map((r, j) => (
            <a key={r.name} href={`#projects/${encodeURIComponent(r.name)}/settings`} className="cursor-pointer [&:hover_rect]:stroke-primary">
              <title>{`${r.name} 설정 열기`}</title>
              <rect x={rx} y={ry(j)} width={rw} height={36} rx={8} className="fill-grey-50 stroke-grey-200" />
              <text x={rx + 10} y={ry(j) + 16}>
                {r.name} <tspan className="!fill-text-muted !font-normal">{r.github || ""}</tspan>
              </text>
              <text x={rx + 10} y={ry(j) + 30} className="!fill-text-muted !text-[11px] !font-normal">{(r.description || "").slice(0, 48)}</text>
            </a>
          ))}
        </svg>
      </Card>
    </>
  );
}
