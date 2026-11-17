// 작업실 as a pixel-art factory (TP-127): the scene is rasterized at native resolution (pixel.ts) and the canvas is
// scaled up by the largest integer that fits, without smoothing. Labels sit on top as SVG so they stay readable.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useThemeVersion } from "../../theme/theme";
import { Frame, box, face, shadow, type Layer, type Paint, type Shade } from "./pixel";
import { IDLE_NAME, OPEN_NAME, WALL, canvas, layout, queueAt, type CrewKey, type Item, type Layout, type Proj, type Reason, type Rect, type Scene, type Spot, type Target } from "./layout";

// ---- sprites, 16 px wide (own outline: committed without the auto outline)
const WORKER = [
  "......oooo......",
  "....ookkHHoo....",
  "...okkHHHHHHo...",
  "...oHHHHHHHHo...",
  "..ohhhhhhhhhho..",
  "...osssssssso...",
  "...ossessesso...",
  "...osSssssSso...",
  "....oSSSSSSo....",
  "......oSSo......",
  "...ooCCCCCCoo...",
  "..olCCCCCCCCco..",
  "..olrrrrrrrrco..",
  ".olCoCCCCCCocCo.",
  ".olCoCCCCCCocCo.",
  ".olCorrrrrrocCo.",
  ".ogg.oCCCCo.ggo.",
  ".ogg.occcco.ggo.",
  "..oo.oppppo.oo..",
  ".....oppppo.....",
  "....opPooPpo....",
  "....opPo.oPpo...",
  "....opPo.oPpo...",
  "...obbBo.obBbo..",
  "...obbbo.obbbo..",
  "...ooooo.ooooo..",
];
const LEGS = WORKER.slice(16);
// blocked: right arm up, two wave frames
const WAVE = [[
  "......oooo......",
  "....ookkHHoooo..",
  "...okkHHHHHHogg.",
  "...oHHHHHHHHogg.",
  "..ohhhhhhhhhooo.",
  "...ossssssssoClo",
  "...ossessessoClo",
  "...osSssssSsoClo",
  "....oSSSSSSooClo",
  "......oSSo..oClo",
  "...ooCCCCCCooClo",
  "..olCCCCCCCCoClo",
  "..olrrrrrrrrco..",
  ".olCoCCCCCCo....",
  ".olCoCCCCCCo....",
  ".olCorrrrrro....",
  ".ogg.oCCCCoo....",
  ".ogg.occccoo....",
  "..oo.oppppo.....",
  ...LEGS.slice(3),
], [
  "......oooo......",
  "....ookkHHoo.oo.",
  "...okkHHHHHHoogg",
  "...oHHHHHHHHoogg",
  "..ohhhhhhhhhhooo",
  "...ossssssssooCl",
  "...ossessessooCl",
  "...osSssssSsooCl",
  "....oSSSSSSo.oCl",
  "......oSSo...oCl",
  "...ooCCCCCCoooCl",
  "..olCCCCCCCCcoCl",
  "..olrrrrrrrrco..",
  ".olCoCCCCCCo....",
  ".olCoCCCCCCo....",
  ".olCorrrrrro....",
  ".ogg.oCCCCoo....",
  ".ogg.occccoo....",
  "..oo.oppppo.....",
  ...LEGS.slice(3),
]];
const ROBOT = [
  ".......LL.......",
  "......oLLo......",
  ".......aa.......",
  "...oooooooooo...",
  "..okkBBBBBBBBo..",
  "..okvvvvvvvvBo..",
  "..oBvEEvvEEvBo..",
  "..oBvEEvvEEvBo..",
  "..oBvvvvvvvvBo..",
  "..oBBBBBBBBBbo..",
  ".oaoBBdddBBboao.",
  ".oaobBBBBBbboao.",
  "..o.obbbbbbo.o..",
  "...oooooooooo...",
  "..oTTTTTTTTTTo..",
  ".oTtTTtTTtTTtTo.",
  ".oTTTTTTTTTTTTo.",
  "..oooooooooooo..",
];
/** a failed robot lies on its side */
const FALLEN = ROBOT[0].split("").map((_, x) => ROBOT.map((r) => r[x]).reverse().join(""));
const WHEEL = [".oooo.", "oggggo", "ogwwgo", "ogwwgo", "oggggo", ".oooo."];
const HAT: Record<string, Record<string, string>> = {
  claude: { H: "warning-400", k: "warning-200", h: "warning-600" },
  codex: { H: "info-400", k: "info-200", h: "info-600" },
  other: { H: "grey-100", k: "grey-50", h: "grey-300" },
};
const BASE = { o: "grey-900", s: "warning-100", S: "warning-200", e: "grey-900", r: "grey-50", g: "grey-700", p: "info-800", P: "info-900", b: "grey-800", B: "grey-600" };
/** a repo's color family: the worker's overalls and the tape on its cartons */
const FAMILIES = ["success", "secondary", "primary", "info"];
const family = (repo: string) => FAMILIES[[...repo].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7) % FAMILIES.length];
const CREW_COLOR: Record<CrewKey, string> = { dispatch: "warning", review: "info", diff: "secondary", release: "primary", cleanup: "success" };

// ---- shades
const STEEL: Shade = { top: "grey-300", left: "grey-600", right: "grey-500", hi: "grey-100", edge: "grey-400", foot: "grey-700" };
const DARK: Shade = { top: "grey-700", left: "grey-800", right: "grey-700", hi: "grey-600" };
const BOX: Shade = { top: "warning-300", left: "warning-600", right: "warning-500", hi: "warning-200", edge: "warning-400" };
const WOOD: Shade = {
  top: (x, y) => ((x + 2 * y) % 6 === 0 ? "warning-600" : "warning-500"),
  left: (_, y) => (y % 4 === 0 ? "warning-800" : "warning-700"),
  right: (_, y) => (y % 4 === 0 ? "warning-700" : "warning-600"),
  hi: "warning-300",
  edge: "warning-500",
  foot: "warning-900",
};
const flat = (t: string): Shade => ({ top: t, left: t, right: t });
const hazard: Paint = (x, y) => (((x + 2 * y) >> 2) % 2 ? "warning-400" : "grey-900");
const LIGHT = { pass: "success-400", fail: "danger-500", run: "warning-300", none: "grey-500" };
export const TAG: Record<Reason, string> = { queued: "info-400", waiting_dep: "warning-400", needs_repo: "danger-400", unmapped: "danger-400" };
export const REASON: Record<Reason, string> = { queued: "투입 대기", waiting_dep: "선행 작업 대기", needs_repo: "레포 선택 필요", unmapped: "매핑 없음" };

// footprint [x0, y0, x1, y1, layer]: layer breaks ties between overlapping footprints (things on a belt or shelf)
type Foot = [number, number, number, number, number?];
type Draw = [Foot, (L: Layer) => void, string | null];
/** b must be painted before a: b lies wholly behind a on one grid axis, or they overlap and b is lower / farther */
function before([bx0, by0, bx1, by1, bl = 0]: Foot, [ax0, ay0, ax1, ay1, al = 0]: Foot) {
  const e = 1e-6;
  if (ax1 <= bx0 + e || ay1 <= by0 + e) return false;
  if (bx1 <= ax0 + e || by1 <= ay0 + e) return true;
  return bl !== al ? bl < al : bx0 + by0 + bx1 + by1 < ax0 + ay0 + ax1 + ay1;
}
function order(d: Draw[]): Draw[] {
  const left = [...d];
  const out: Draw[] = [];
  while (left.length) {
    let i = left.findIndex((a) => !left.some((b) => b !== a && before(b[0], a[0])));
    if (i < 0) i = 0; // cycle: rare with these shapes, any order is close enough
    out.push(left.splice(i, 1)[0]);
  }
  return out;
}

/** hit: the click area that shows this label on hover / focus; null = always shown (repo names, blocked, release) */
/** along: written on the floor along the belt (ending at gx), not upright and centered; its value is the tooltip */
type Label = [gx: number, gy: number, z: number, text: string, color: string, hit: string | null, along?: string, start?: boolean];
/** a transparent click area over a footprint `h` art px tall (TP-128) */
interface Hit { id: string; foot: Rect; h: number; name: string; t: Target }
const SHELF_SPOTS: Spot[] = ["staged", "merged"];
const MS_SPOTS: Spot[] = ["milestone", "ms_hold", "collecting"];
const MS_TAPE: Record<string, string> = { milestone: "success-500", ms_hold: "warning-500", collecting: "info-500" };

/** the whole scene for animation frame f: pixels and labels */
function render(s: Scene, L: Layout, at: Proj, w: number, h: number, f: number) {
  const d: Draw[] = [];
  const labels: Label[] = [];
  const hits: Hit[] = [];
  // the name is the id: it stays the same across polls, so a hovered label does not jump to another object
  const hit = (foot: Rect, h: number, name: string, t: Target) => {
    hits.push({ id: name, foot, h, name, t });
    return name;
  };
  const tickets = (title: string, list: { gid: string }[]): Target => ({ kind: "tickets", title, gids: list.map((x) => x.gid) });
  const blink = f % 4 < 2;
  const { W, H } = L;

  const carton = (Y: Layer, gx: number, gy: number, z: number, tape: string, sz = 0.36) => {
    const [x0, y0, x1, y1] = [gx - sz / 2, gy - sz / 2, gx + sz / 2, gy + sz / 2];
    box(Y, at, x0, y0, x1, y1, z, Math.round(sz * 25), BOX);
    const top = z + Math.round(sz * 25);
    Y.poly([at(gx - 0.035, y0, top), at(gx + 0.035, y0, top), at(gx + 0.035, y1, top), at(gx - 0.035, y1, top)], tape);
    face(Y, at, [gx - 0.035, y1], [gx + 0.035, y1], top - 5, top, tape);
  };
  const belt = (Y: Layer, x0: number, y0: number, x1: number, y1: number, h: number, back = false) => {
    const alongX = x1 - x0 > y1 - y0;
    const dir = back ? -1 : 1;
    const top: Paint = (x, y) => {
      const k = alongX ? x + 2 * y - dir * f * 2 : x - 2 * y + dir * f * 2;
      const m = ((k % 12) + 12) % 12;
      return m < 2 ? (back ? "danger-900" : "grey-900") : m < 3 ? (back ? "danger-500" : "grey-600") : back ? "danger-700" : "grey-800";
    };
    box(Y, at, x0, y0, x1, y1, 0, h, { top, left: back ? "danger-600" : "grey-500", right: back ? "danger-500" : "grey-400" });
    if (alongX) face(Y, at, [x0, y1], [x1, y1], h - 2, h, back ? "danger-400" : "warning-400");
    else face(Y, at, [x1, y0], [x1, y1], h - 2, h, "warning-400");
  };

  // 입고장: one crate pile per reason, at most 6 crates drawn (the label has the count)
  for (const { crate, r } of L.crates) {
    const n = Math.min(crate.n, 6);
    for (let i = 0; i < n; i++) {
      const j = i % 4;
      const [x, y, z] = [r[0] + 0.03 + (j % 2) * 0.55, r[1] + 0.03 + Math.floor(j / 2) * 0.55, i >= 4 ? 12 : 0];
      d.push([[x, y, x + 0.5, y + 0.5, z ? 1 : 0], (Y) => {
        box(Y, at, x, y, x + 0.5, y + 0.5, z, 12, WOOD);
        face(Y, at, [x + 0.5, y + 0.15], [x + 0.5, y + 0.35], z + 4, z + 8, TAG[crate.reason]);
      }, "grey-900"]);
    }
    const text = `${REASON[crate.reason]} ${crate.n}`;
    labels.push([(r[0] + r[2]) / 2, r[3], 0, text, crate.reason === "queued" ? "grey-50" : crate.reason === "waiting_dep" ? "warning-200" : "danger-200",
      hit(r, 24, `입고장 ${text}`, { kind: "tickets", title: `입고장 · ${REASON[crate.reason]}`, gids: crate.gids })]);
  }
  if (!L.crates.length) labels.push([1, 1.4, 0, "입고장 · 비어 있음", "grey-300", null]);
  if (s.stray) {
    const [x, y] = [L.collector[0], L.collector[1]];
    labels.push([x, y, 30, `미등록 레포 ${s.stray}`, "danger-200",
      hit([x - 0.3, y - 0.3, x + 0.3, y + 0.3], 40, `미등록 레포 티켓 ${s.stray}`, { kind: "tickets", title: "미등록 레포", gids: s.strayGids })]);
  }

  // 작업대: one per dispatch slot; worker = live run (hard hat = agent tool, overalls = repo)
  labels.push([3.5, 0.3, 30, `작업대 ${s.benches.length}/${s.maxRuns}`, "grey-50", null]);
  for (const { bench: b, r, worker } of L.benches) {
    const [x0, y0, x1, y1] = r;
    const blocked = b?.stage === "blocked";
    if (b) {
      const [wx, wy] = at((worker[0] + worker[2]) / 2, (worker[1] + worker[3]) / 2);
      const rows = blocked ? WAVE[f % 2] : WORKER;
      const fam = family(b.repo);
      const pal = { ...BASE, ...(HAT[b.tool] ?? HAT.other), C: `${fam}-400`, c: `${fam}-600`, l: `${fam}-300` };
      d.push([worker, (Y) => Y.sprite(rows, pal, wx, wy + (b.stage === "in_progress" && f % 2 ? -1 : 0)), null]);
    }
    d.push([r, (Y) => {
      box(Y, at, x0, y0, x1, y1, 0, 12, STEEL);
      face(Y, at, [x0 + 0.1, y1], [x1 - 0.1, y1], 2, 10, "grey-700");
      face(Y, at, [x0 + 0.45, y1], [x1 - 0.45, y1], 5, 7, "warning-400");
      box(Y, at, x0 + 0.15, y0 + 0.55, x0 + 0.25, y0 + 0.95, 12, 6, DARK); // laptop lid, low so the worker shows
      if (b) carton(Y, x0 + 0.85, y0 + 0.3, 12, `${family(b.repo)}-500`);
      box(Y, at, x1 - 0.12, y0 + 0.06, x1 - 0.06, y0 + 0.12, 13, 15, DARK);
      const lamp = !b ? "grey-500" : blocked ? (blink ? "danger-400" : "danger-700") : "success-400";
      box(Y, at, x1 - 0.16, y0 + 0.02, x1 - 0.02, y0 + 0.16, 28, 4, { ...flat(lamp), hi: b && !(blocked && !blink) ? "grey-50" : undefined });
    }, "grey-900"]);
    if (!b) continue; // an empty bench needs no label: the 작업대 n/m sign counts it
    const text = `${b.key} · ${b.agent}${blocked ? " · 막힘" : b.stage === "dispatched" ? " · 투입됨" : ""}`;
    const id = hit([worker[0], y0, x1, y1], 32, `작업대 ${text}`, tickets(`작업대 · ${b.key}`, [b]));
    labels.push([(x0 + x1) / 2, y1 + 0.15, 0, text, blocked ? "danger-200" : "grey-50", blocked ? null : id]);
  }

  // collector belt, then one line per repo
  const [cx0, cy0, cx1, cy1] = L.collector;
  d.push([L.collector, (Y) => belt(Y, cx0, cy0, cx1, cy1, 6), "grey-900"]);
  for (const lane of L.lanes) {
    const { repo, c } = lane;
    const tape = `${family(repo.name)}-500`;
    const [bx0, by0, bx1, by1] = lane.belt;
    if (!lane.open) {
      d.push([lane.belt, (Y) => belt(Y, bx0, by0, bx1, by1, 3), "grey-900"]);
      // idle belts are only 0.8 deep: upright names of neighbouring belts overlap, so the name runs along its belt;
      // long names are cut to the belt's length, the full name is the text's tooltip (no button: a diagonal belt's
      // bounding box would cover the line behind it)
      const name = repo.name.length > 16 ? repo.name.slice(0, 15) + "…" : repo.name;
      labels.push([bx1 - IDLE_NAME[0], c + IDLE_NAME[1], 0, name, "grey-300", null, `${repo.name} 라인 (대기 중)`]);
      continue;
    }
    const mine = (...at: Spot[]) => s.items.filter((it) => it.repo === repo.name && at.includes(it.at));
    d.push([lane.belt, (Y) => belt(Y, bx0, by0, bx1, by1, 6), "grey-900"]);
    // starts at the anchor and runs right, away from the idle names that climb up-left below it
    labels.push([bx0 + OPEN_NAME[0], c + OPEN_NAME[1], 0, `${repo.name} · ${repo.agent}`, "grey-50", null, undefined, true]);
    // cartons riding a line: at most 3 drawn per spot, the rest counted on the last label
    // the last carton drawn opens the ones not drawn too, so every ticket is one click away; a stack (step 0) is one hit
    const ride = (list: Item[], x: number, gy: number, z: number, text: (it: Item) => string, color: string, step = 0.42) =>
      list.slice(0, 3).forEach((it, k) => {
        const [gx] = queueAt(x, gy, k, step);
        const foot: Rect = [gx - 0.15, gy - 0.15, gx + 0.15, gy + 0.15];
        d.push([[...foot, 1], (Y) => carton(Y, gx, gy, z, tape, 0.3), "grey-900"]);
        if (!step && k) return; // a stack: the bottom carton's hit and label speak for all of it
        const rest = !step ? list : k === 2 ? list.slice(2) : [it];
        const more = rest.length > 1 ? ` +${rest.length - 1}` : "";
        const id = hit(foot, z + 10, `${repo.name} ${text(it)}${more}`, tickets(`${repo.name} · ${text(it)}${more}`, rest));
        labels.push([gx, gy, z + 14 + (k % 2) * 6, text(it) + more, color, id]);
      });
    // 반송 벨트 (red, runs back toward the benches)
    const [rx0, ry0, rx1, ry1] = lane.rework!;
    d.push([lane.rework!, (Y) => belt(Y, rx0, ry0, rx1, ry1, 4, true), "grey-900"]);
    ride(mine("rework", "conflict"), rx0 + 0.5, (ry0 + ry1) / 2, 4,
      (it) => (it.at === "conflict" ? `${it.key} 충돌` : `${it.key} ${it.round ?? 0}/${repo.maxRework || "∞"}`), "danger-200", 0.7);
    // 검수 게이트: one lamp per check, plus CI
    ride(mine("agent_done"), bx0 + 0.3, c, 6, (it) => `${it.key} PR 준비`, "grey-200");
    const checking = mine("pr_open");
    ride(checking, bx0 + 1.1, c, 6, (it) => it.key, "grey-50", 0.36);
    const [p0, p1] = lane.posts!;
    const lights = checking[0]?.lights ?? [];
    d.push([[p0[0], p0[1], p1[2], p1[3], 1], (Y) => {
      box(Y, at, p0[0], p0[1], p0[2], p0[3], 0, 22, DARK);
      box(Y, at, p1[0], p1[1], p1[2], p1[3], 0, 22, DARK);
      box(Y, at, p0[0] - 0.02, p0[1], p1[2] + 0.02, p1[3], 20, 6, { top: "warning-400", left: "warning-600", right: "warning-500", hi: "warning-200" });
      repo.gates.forEach((_, i) => {
        const gy = p0[1] + 0.1 + ((i + 0.5) * (p1[3] - p0[1] - 0.2)) / repo.gates.length;
        const l = lights[i] ?? "none";
        face(Y, at, [p1[2] + 0.02, gy - 0.05], [p1[2] + 0.02, gy + 0.05], 22, 25, l === "run" && !blink ? "warning-600" : LIGHT[l]);
      });
    }, "grey-900"]);
    labels.push([p0[2], c, 40, repo.gates.length ? repo.gates.join(" · ") : "검사 없음", repo.gates.length ? "success-300" : "grey-300",
      hit([p0[0] - 0.1, p0[1], p1[2] + 0.1, p1[3]], 30, `${repo.name} 검수 게이트`, { kind: "gate", repo: repo.name, gids: checking.map((it) => it.gid) })]);
    // AI 리뷰 통과 behind the gate; approving moves on toward the warehouse
    ride(mine("reviewed"), 7.5, c, 6, (it) => `${it.key} 통과`, "success-200", 0.36);
    ride(mine("approving"), 8.55, c, 6, (it) => `${it.key} 입고 중`, "info-200", 0);
    // 마일스톤 팔레트
    if (lane.pallet) {
      const [mx0, my0, mx1, my1] = lane.pallet;
      const ms = mine(...MS_SPOTS);
      d.push([lane.pallet, (Y) => {
        box(Y, at, mx0, my0, mx1, my1, 0, 4, { top: "warning-600", left: "warning-800", right: "warning-700", hi: "warning-400" });
        ms.slice(0, 6).forEach((it, i) => carton(Y, mx0 + 0.22 + (i % 3) * 0.24, my0 + 0.17 + (Math.floor(i / 3) % 2) * 0.28, 4, MS_TAPE[it.at], 0.22));
      }, "grey-900"]);
      const n = (k: Spot) => ms.filter((it) => it.at === k).length;
      const txt = [["마일스톤", n("milestone")], ["완료 대기", n("ms_hold")], ["수집 중", n("collecting")]].filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(" · ");
      labels.push([(mx0 + mx1) / 2, my1 + 0.1, 0, txt, "grey-50", hit(lane.pallet, 16, `${repo.name} 마일스톤 팔레트`, tickets(`${repo.name} · 마일스톤`, ms))]);
    }
    // 창고 선반: staged below, merged (waiting for deploy) on top
    const [sx0, sy0, sx1, sy1] = lane.shelf!;
    const [staged, merged] = SHELF_SPOTS.map((k) => mine(k));
    d.push([lane.shelf!, (Y) => {
      const post = (x: number, y: number) => box(Y, at, x, y, x + 0.06, y + 0.06, 0, 30, { top: "info-500", left: "info-700", right: "info-600" });
      post(sx0, sy0);
      post(sx1 - 0.06, sy0);
      box(Y, at, sx0, sy0, sx1, sy1, 0, 2, { top: "grey-400", left: "grey-600", right: "grey-500" });
      box(Y, at, sx0, sy0, sx1, sy1, 15, 2, { top: "grey-400", left: "info-700", right: "info-600", hi: "grey-200" });
      staged.slice(0, 4).forEach((_, i) => carton(Y, sx0 + 0.3 + (i % 2) * 0.45, sy0 + 0.35 + Math.floor(i / 2) * 0.5, 2, tape));
      merged.slice(0, 4).forEach((_, i) => carton(Y, sx0 + 0.3 + (i % 2) * 0.45, sy0 + 0.35 + Math.floor(i / 2) * 0.5, 17, "success-500"));
      post(sx0, sy1 - 0.06);
      post(sx1 - 0.06, sy1 - 0.06);
    }, "grey-900"]);
    const shelfText = `창고 ${staged.length}${merged.length ? ` · 머지 ${merged.length}` : ""}`;
    labels.push([(sx0 + sx1) / 2, c, 44, shelfText, "grey-50", hit(lane.shelf!, 32, `${repo.name} ${shelfText}`, tickets(`${repo.name} · ${shelfText}`, [...staged, ...merged]))]);
    // 출하 도크: train repos load a truck, the others a quick van
    const deploying = mine("deploying");
    const [dx0, dy0, dx1, dy1] = lane.dock!;
    const len = dx1 - dx0 - 0.5;
    const vh = repo.train ? 22 : 15;
    d.push([lane.dock!, (Y) => {
      box(Y, at, dx0, dy0, dx0 + len, dy1, 4, vh, repo.train ? { top: "grey-100", left: "grey-200", right: "grey-100", hi: "grey-50" } : { top: "warning-300", left: "warning-500", right: "warning-400", hi: "warning-200" });
      face(Y, at, [dx0 + 0.08, dy1], [dx0 + len - 0.08, dy1], 10, 13, repo.train ? "primary-500" : "warning-700");
      box(Y, at, dx0 + len + 0.04, dy0 + 0.05, dx1, dy1 - 0.05, 4, vh - 6, { top: "primary-400", left: "primary-700", right: "primary-600", hi: "primary-300" });
      face(Y, at, [dx1, dy0 + 0.15], [dx1, dy1 - 0.15], vh - 10, vh - 4, "info-200");
      face(Y, at, [dx0 + len + 0.12, dy1 - 0.05], [dx1 - 0.08, dy1 - 0.05], vh - 10, vh - 4, "info-300");
      deploying.slice(0, 3).forEach((_, i) => carton(Y, dx0 + 0.25 + i * 0.32, (dy0 + dy1) / 2, 4 + vh, tape, 0.28));
    }, "grey-900"]);
    for (const wx of [dx0 + 0.25, dx0 + len - 0.2, dx1 - 0.2]) {
      const [sx, sy] = at(wx, dy1, 0);
      d.push([[wx - 0.1, dy1 - 0.1, wx + 0.1, dy1 + 0.05, 1], (Y) => Y.sprite(WHEEL, { o: "grey-900", g: "grey-800", w: "grey-400" }, sx, sy + 1), null]);
    }
    labels.push([(dx0 + dx1) / 2, dy1 + 0.15, 0,
      deploying.length ? `출하 중 ${deploying.length}` : repo.train ? "트레인" : "퀵 배송",
      deploying.length ? "success-200" : "grey-50", hit(lane.dock!, vh + 4, `${repo.name} ${repo.train ? "트럭" : "퀵 배송"}`, { kind: "truck", repo: repo.name })]);
  }

  // 로봇 (automations): run = bobbing, green lamp; skip = blue lamp; fail = lies on its side; off = grey
  for (const { crew, r } of L.crew) {
    const [gx, gy] = [(r[0] + r[2]) / 2, (r[1] + r[3]) / 2];
    const [sx, sy] = at(gx, gy);
    const col = crew.status === "off" ? "grey" : CREW_COLOR[crew.key];
    const lamp = crew.status === "run" ? (blink ? "success-300" : "success-500") : crew.status === "skip" ? "info-300" : crew.status === "fail" ? "danger-500" : "grey-700";
    const pal = { o: "grey-900", L: lamp, a: "grey-700", B: `${col}-400`, k: `${col}-200`, b: `${col}-600`, d: `${col}-700`, v: "grey-900", E: crew.status === "off" ? "grey-600" : "info-200", T: "grey-700", t: "grey-500" };
    const bob = crew.status === "run" && f % 4 === 0 ? -1 : 0;
    d.push([r, (Y) => Y.sprite(crew.status === "fail" ? FALLEN : ROBOT, pal, sx, sy + bob), null]);
    const text = `${crew.name} · ${crew.note}`;
    labels.push([gx, gy + 0.4, 0, text, crew.status === "fail" ? "danger-200" : crew.status === "run" ? "success-200" : "grey-300",
      hit(r, 24, `${text} 로봇`, { kind: "robot", key: crew.key })]);
  }

  // ---- paint: shell, floor marks, then objects in painter order
  const F = new Frame(w, h);
  F.fill("grey-900");
  const S = F.layer();
  face(S, at, [0, 0], [0, H], 0, WALL, (x) => (x % 4 === 0 ? "grey-600" : "grey-500"));
  face(S, at, [0, 0], [W, 0], 0, WALL, (x) => (x % 4 === 0 ? "grey-500" : "grey-400"));
  face(S, at, [0, 0], [W, 0], WALL - 3, WALL, "grey-700");
  face(S, at, [0, 0], [0, H], WALL - 3, WALL, "grey-800");
  for (let x = 0.5; x + 0.8 < BOARD[0]; x += 2) {
    face(S, at, [x, 0], [x + 0.8, 0], WALL - 22, WALL - 9, "grey-700");
    face(S, at, [x + 0.06, 0], [x + 0.74, 0], WALL - 21, WALL - 10, (px, py) => (px % 7 === 0 ? "grey-600" : py % 5 === 0 ? "info-100" : "info-200"));
  }
  // roller door the tickets come in through
  face(S, at, [0, 0.75], [0, 2.6], 0, 34, (_, y) => (y % 3 === 0 ? "grey-600" : "grey-400"));
  face(S, at, [0, 0.75], [0, 2.6], 0, 5, hazard);
  // 출하 전광판 on the back wall (its text is an SVG label)
  face(S, at, [BOARD[0], 0], [BOARD[1], 0], 12, 54, "grey-700");
  face(S, at, [BOARD[0] + 0.1, 0], [BOARD[1] - 0.1, 0], 14, 52, "grey-900");
  face(S, at, [BOARD[0] + 0.2, 0], [BOARD[1] - 0.2, 0], 16, 17, s.release.hold ? "danger-700" : "success-700");
  S.poly([at(0, 0), at(W, 0), at(W, H), at(0, H)], "grey-400");
  for (let x = 0; x < W; x += 2)
    for (let y = 0; y < H; y += 2) {
      const base = (x + y) % 4 ? "grey-300" : "grey-200";
      const [x1, y1] = [Math.min(W, x + 2) - 0.06, Math.min(H, y + 2) - 0.06];
      S.poly([at(x + 0.06, y + 0.06), at(x1, y + 0.06), at(x1, y1), at(x + 0.06, y1)], (px, py) => ((px * 7 + py * 13) % 29 === 0 ? "grey-400" : base));
    }
  face(S, at, [0, H], [W, H], -8, 0, "grey-600");
  face(S, at, [W, 0], [W, H], -8, 0, "grey-500");
  S.commit(null);
  const M = F.layer();
  for (const l of L.lanes) if (l.dock) M.poly([at(l.dock[0] - 0.1, l.dock[1] - 0.2), at(W - 0.05, l.dock[1] - 0.2), at(W - 0.05, l.dock[3] + 0.2), at(l.dock[0] - 0.1, l.dock[3] + 0.2)], hazard);
  for (const [[x0, y0, x1, y1, layer]] of d) if (!layer) shadow(M, at, x0, y0, x1, y1);
  M.commit(null);
  for (const [, draw, outline] of order(d)) {
    const Y = F.layer();
    draw(Y);
    Y.commit(outline);
  }
  return { F, labels, hits };
}
/** back-wall span (grid x) of the 출하 전광판 */
const BOARD = [6.3, 12.5];
/** board text size in art px: it scales with the board so it always fits (3 lines over z 52..17, ~96 art px wide) */
const BOARD_FONT = 8;

const DPR = () => window.devicePixelRatio || 1;

/** what the scene shows, for screen readers */
function summary(s: Scene) {
  const n = (k: Spot) => s.items.filter((it) => it.at === k).length;
  const blocked = s.benches.filter((b) => b.stage === "blocked").length;
  return [
    `작업실 공장: 작업대 ${s.benches.length}/${s.maxRuns}${blocked ? ` (막힘 ${blocked})` : ""}`,
    ...s.crates.map((c) => `${REASON[c.reason]} ${c.n}`),
    `리뷰 대기 ${n("pr_open")}, 리뷰 통과 ${n("reviewed")}, 수정 대기 ${n("rework")}, 충돌 ${n("conflict")}`,
    `창고 ${n("staged") + n("merged")}, 출하 중 ${n("deploying")}, 마일스톤 ${n("milestone") + n("ms_hold") + n("collecting")}`,
    ...(s.stray ? [`미등록 레포의 티켓 ${s.stray}`] : []),
    `레포 라인: ${s.repos.map((r) => r.name).join(", ")}`,
    `다음 출하 ${s.release.next}${s.release.hold ? " (보류)" : ""}`,
    ...s.crew.map((c) => `${c.name} 로봇: ${c.note}`),
  ].join(". ");
}

/** screen box (canvas art px) of a footprint `h` px tall */
function bbox(at: Proj, [x0, y0, x1, y1]: Rect, h: number) {
  const [l] = at(x0, y1);
  const [r] = at(x1, y0);
  const [, t] = at(x0, y0, h);
  const [, b] = at(x1, y1);
  return [l, t, r - l, b - t];
}

export function Factory({ s, onPick }: { s: Scene; onPick: (t: Target) => void }) {
  const L = useMemo(() => layout(s), [s]);
  const C = useMemo(() => canvas(L), [L]);
  const wrap = useRef<HTMLDivElement>(null);
  const ref = useRef<HTMLCanvasElement>(null);
  const [fit, setFit] = useState(0); // measured in a layout effect before the first paint: no frame at a guessed size (TP-175, layout shift)
  const [{ labels, hits }, setOver] = useState<{ labels: Label[]; hits: Hit[] }>({ labels: [], hits: [] });
  const [hot, setHot] = useState<string | null>(null);
  const theme = useThemeVersion(); // a theme change repaints (pixel.ts re-reads the colors)
  const frame = useRef(0); // kept across polls so belts and lamps do not jump
  // the largest integer scale (in device pixels) that fits; never below 1 (the wrapper scrolls then)
  useLayoutEffect(() => {
    const el = wrap.current!;
    const measure = () => setFit(Math.max(1, Math.floor(Math.min((el.clientWidth * DPR()) / C.w, (el.clientHeight * DPR()) / C.h))));
    measure(); // before the first paint
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [C]);
  useEffect(() => {
    const ctx = ref.current!.getContext("2d")!;
    const tick = () => {
      const { F, labels, hits } = render(s, L, C.at, C.w, C.h, frame.current++);
      F.draw(ctx);
      return { labels, hits };
    };
    setOver(tick());
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const t = setInterval(tick, 180);
    return () => clearInterval(t);
  }, [s, L, C, theme]);
  const [cw, ch] = [(C.w * fit) / DPR(), (C.h * fit) / DPR()];
  const font = 11 * (C.w / (cw || 1)); // 11 CSS px whatever the scale
  const [bx, by] = C.at(BOARD[0] + 0.3, 0, 44);
  return (
    <div ref={wrap} className="grid h-full w-full place-items-center overflow-auto">
      <div className="relative" style={{ width: cw, height: ch }}>
        <canvas ref={ref} width={C.w} height={C.h} role="img" aria-label={summary(s)} className="absolute inset-0 h-full w-full" style={{ imageRendering: "pixelated" }} />
        <svg viewBox={`0 0 ${C.w} ${C.h}`} className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden>
          <g transform={`matrix(1 0.5 0 1 ${bx} ${by})`} className="font-bold" style={{ fontSize: BOARD_FONT }}>
            <text className={s.release.hold ? "fill-danger-300" : "fill-success-300"}>{`출하 · ${s.release.days} ${s.release.at}`}</text>
            <text y={BOARD_FONT * 1.2} className="fill-warning-300">{s.release.hold ? "보류 중" : `다음 ${s.release.nextDay}`}</text>
            <text y={BOARD_FONT * 2.4} className="fill-grey-400">{`${s.release.window}분 창`}</text>
          </g>
          {labels.map(([gx, gy, z, t, c, h, along, start], i) => {
            if (h && h !== hot) return null;
            const [x, y] = C.at(gx, gy, z);
            return (
              <text key={i} {...(along ? { transform: `matrix(1 0.5 0 1 ${x} ${y})`, textAnchor: "end", pointerEvents: "auto" } : { x, y: z ? y : y + font, textAnchor: start ? "start" : "middle" })} strokeWidth={font / 4} strokeLinejoin="round"
                style={{ fontSize: font, paintOrder: "stroke", fill: `var(--color-${c})` }} className="stroke-grey-900 font-bold">{along && <title>{along}</title>}{t}</text>
            );
          })}
        </svg>
        {hits.map((h) => {
          const [x, y, w, hh] = bbox(C.at, h.foot, h.h);
          return (
            <button key={h.id} type="button" aria-label={`${h.name} (자세히 보기)`} title={h.name} onClick={() => onPick(h.t)}
              onMouseEnter={() => setHot(h.id)} onMouseLeave={() => setHot(null)} onFocus={() => setHot(h.id)} onBlur={() => setHot(null)}
              className="absolute cursor-pointer rounded-sm outline-none hover:ring-2 hover:ring-warning-300 focus-visible:ring-2 focus-visible:ring-warning-300"
              style={{ left: `${(x / C.w) * 100}%`, top: `${(y / C.h) * 100}%`, width: `${(w / C.w) * 100}%`, height: `${(hh / C.h) * 100}%` }} />
          );
        })}
      </div>
    </div>
  );
}
