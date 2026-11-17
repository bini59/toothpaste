// 작업실 (factory) scene model and layout (TP-127). Pure (no React, no DOM) so `npm run check` can run it in node.
// Flow, back-left -> front-right: 입고장 (pending tickets) -> 작업대 (agents, count = max_active_runs) -> collector belt
// -> one line per repo: 검수 게이트 (checks / CI) -> AI 리뷰 통과 -> 창고 (staging) -> 출하 도크 (train truck / quick van).
// Grid units: x along the flow, y across the lines; 1 grid unit = one 32 x 16 art px iso tile (pixel.ts).

export type Light = "pass" | "fail" | "run" | "none";
export interface Repo {
  name: string;
  tool: string;
  /** "tool·model" for the name plate */
  agent: string;
  /** one lamp each, plus "CI" for a CI repo */
  gates: string[];
  train: boolean;
  modes: string[];
  merge: string;
  maxRework: number;
}
export type BenchStage = "dispatched" | "in_progress" | "blocked";
export interface Bench { gid: string; key: string; repo: string; tool: string; agent: string; stage: BenchStage }
/** where a ticket sits on its repo's line (one per pipeline stage after the bench) */
export type Spot = "agent_done" | "pr_open" | "reviewed" | "approving" | "rework" | "conflict"
  | "milestone" | "ms_hold" | "collecting" | "staged" | "merged" | "deploying";
export interface Item { gid: string; key: string; repo: string; at: Spot; lights?: Light[]; round?: number }
export type Reason = "queued" | "waiting_dep" | "needs_repo" | "unmapped";
export interface Crate { reason: Reason; n: number; gids: string[] }
export type CrewKey = "dispatch" | "review" | "diff" | "release" | "cleanup";
export type CrewStatus = "run" | "skip" | "fail" | "off";
export interface Crew { key: CrewKey; name: string; status: CrewStatus; note: string; last?: string | number }
/** what a click on the scene opens (TP-128): tickets (a worker, cartons, a crate pile, a shelf), a gate, a robot, a truck */
export type Target =
  | { kind: "tickets"; title: string; gids: string[] }
  | { kind: "gate"; repo: string; gids: string[] }
  | { kind: "robot"; key: CrewKey }
  | { kind: "truck"; repo: string };
export interface Scene {
  maxRuns: number;
  release: { days: string; at: string; window: number; next: string; nextDay: string; hold: boolean };
  repos: Repo[];
  benches: Bench[];
  crates: Crate[];
  items: Item[];
  crew: Crew[];
  /** tickets in a drawn stage whose repo is not registered (no line to put them on) */
  stray: number;
  strayGids: string[];
}

/** floor footprint [x0, y0, x1, y1] in grid units */
export type Rect = [number, number, number, number];

export const W = 12.8;
const TOP = 0.8; // first row starts this far from the back wall
const CRATE_STEP = 1.5;
const BENCH_STEP = 1.4;
const OPEN = 2.8; // lane depth of a repo with work on its line
const SHUT = 0.8; // idle repo: a thin belt and its name
const BELT_X = 5.1; // where the repo lines leave the collector belt
/** where repo names sit: an open line's [x after belt start, y after line centre], an idle belt's [x before belt end, y] */
export const OPEN_NAME = [0.4, 0.65] as const;
export const IDLE_NAME = [0.3, 0.1] as const;

export interface Lane {
  repo: Repo;
  c: number;
  open: boolean;
  belt: Rect;
  rework?: Rect;
  posts?: [Rect, Rect];
  pallet?: Rect;
  shelf?: Rect;
  dock?: Rect;
}

const sq = (x: number, y: number, r: number): Rect => [x - r, y - r, x + r, y + r];

export function layout(s: Scene) {
  const crates = s.crates.map((crate, i) => ({ crate, r: [0.4, TOP + i * CRATE_STEP, 1.5, TOP + 1.1 + i * CRATE_STEP] as Rect }));
  // a bench per dispatch slot; more live runs than slots (the setting was lowered) still get one each
  const n = Math.max(1, s.maxRuns, s.benches.length);
  const benches = Array.from({ length: n }, (_, i) => {
    const y = TOP + i * BENCH_STEP;
    return { bench: s.benches[i] as Bench | undefined, r: [2.8, y, 4.2, y + 1] as Rect, worker: [2.3, y + 0.3, 2.7, y + 0.7] as Rect };
  });
  // busy repos first, so the idle thin belts trail at the front
  const busy = (r: Repo) => s.items.some((it) => it.repo === r.name);
  const repos = [...s.repos.filter(busy), ...s.repos.filter((r) => !busy(r))];
  let y = TOP;
  const lanes: Lane[] = repos.map((repo, i) => {
    const open = busy(repo);
    // the first idle belt's name climbs up-left toward the open line's name: room for it (checked in layout.check.ts)
    if (!open && i > 0 && busy(repos[i - 1])) y += 0.8;
    const c = y + (open ? 1.3 : SHUT / 2);
    y += open ? OPEN : SHUT;
    if (!open) return { repo, c, open, belt: [BELT_X, c - 0.12, 10.2, c + 0.12] };
    const ms = s.items.some((it) => it.repo === repo.name && ["milestone", "ms_hold", "collecting"].includes(it.at));
    return {
      repo, c, open,
      belt: [BELT_X, c - 0.25, 8.85, c + 0.25],
      rework: [5.4, c - 0.9, 7.7, c - 0.62],
      posts: [[7.1, c - 0.52, 7.2, c - 0.42], [7.1, c + 0.42, 7.2, c + 0.52]],
      pallet: ms ? [7.6, c + 0.5, 8.5, c + 1.1] : undefined,
      shelf: [8.95, c - 0.6, 10.05, c + 0.6],
      dock: [10.3, c - 0.45, repo.train ? 12.4 : 11.9, c + 0.45],
    };
  });
  const lastBench = benches.at(-1)!.r;
  const lastLane = lanes.at(-1);
  const collector: Rect = [4.6, TOP - 0.2, BELT_X, Math.max(lastBench[1] + 0.5, lastLane ? lastLane.c : 0) + 0.25];
  // robots stand in a staggered column on the front-left floor, below the crates and benches
  const top = Math.max(lastBench[3], crates.at(-1)?.r[3] ?? 0) + 0.9;
  const crew = s.crew.map((c, i) => ({ crew: c, r: sq(2.6, top + 0.6 + i * 1.8, 0.22) }));
  const bottom = Math.max(collector[3], y, ...crew.map((c) => c.r[3] + 0.6));
  return { W, H: bottom + 0.3, crates, benches, collector, lanes, crew };
}
export type Layout = ReturnType<typeof layout>;

/** every floor footprint, for the overlap check (things on a belt or shelf ride on it and are not listed) */
export function feet(L: Layout): [string, Rect][] {
  const out: [string, Rect][] = [["collector", L.collector]];
  L.crates.forEach((c, i) => out.push(["crates" + i, c.r]));
  L.benches.forEach((b, i) => out.push(["bench" + i, b.r], ["worker" + i, b.worker]));
  for (const l of L.lanes)
    for (const k of ["belt", "rework", "pallet", "shelf", "dock"] as const) if (l[k]) out.push([l.repo.name + "." + k, l[k]!]);
  for (const l of L.lanes) l.posts?.forEach((p, i) => out.push([l.repo.name + ".post" + i, p]));
  L.crew.forEach((c) => out.push(["crew." + c.crew.key, c.r]));
  return out;
}

// ---- iso projection at native scale: a tile is 32 x 16 art px
export const TW = 32;
export const TH = 16;
export const WALL = 58; // wall height, art px
const PAD = 12;
export type Pt = [number, number];
/** canvas size and projection for a layout: grid (gx, gy, z art px up) -> canvas art px */
export function canvas({ W, H }: { W: number; H: number }) {
  const ox = H * (TW / 2) + PAD;
  const oy = WALL + PAD;
  const at = (gx: number, gy: number, z = 0): Pt => [ox + ((gx - gy) * TW) / 2, oy + ((gx + gy) * TH) / 2 - z];
  return { w: Math.ceil((W + H) * (TW / 2) + 2 * PAD), h: Math.ceil(oy + (W + H) * (TH / 2) + PAD + 8), at };
}
export type Proj = ReturnType<typeof canvas>["at"];

/** k-th box of the same kind along a line: steps along x, wraps into a second row */
export const queueAt = (x: number, c: number, k: number, step = 0.42, per = 3): Pt =>
  [x + (k % per) * step, c + (Math.floor(k / per) % 2 ? 0.12 : -0.05)];
