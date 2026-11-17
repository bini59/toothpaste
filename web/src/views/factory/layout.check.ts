// self-check for layout.ts: `npm run check` (node strips the types)
import { IDLE_NAME, OPEN_NAME, canvas, feet, layout, type Item, type Repo, type Scene } from "./layout.ts";

const ok = (c: unknown, m: string) => { if (!c) throw new Error("factory layout check failed: " + m); };
const E = 1e-6;
const overlap = (a: number[], b: number[]) => a[0] < b[2] - E && b[0] < a[2] - E && a[1] < b[3] - E && b[1] < a[3] - E;

const repo = (i: number): Repo => ({ name: "r" + i, tool: i % 2 ? "codex" : "claude", agent: "claude·opus", gates: ["lint", "test"],
  train: i % 3 === 0, modes: ["direct"], merge: "merge", maxRework: 3 });
const crew = (["dispatch", "review", "diff", "release", "cleanup"] as const).map((key) => ({ key, name: key, status: "skip" as const, note: "" }));
const SPOTS: Item["at"][] = ["agent_done", "pr_open", "reviewed", "approving", "rework", "conflict", "milestone", "ms_hold", "collecting", "staged", "merged", "deploying"];

for (let nb = 1; nb <= 8; nb++)
  for (let nr = 1; nr <= 8; nr++)
    for (const busy of [0, 1, nr]) { // no repo busy, one, all
      const repos = Array.from({ length: nr }, (_, i) => repo(i));
      const items = repos.slice(0, busy).flatMap((r, i) => SPOTS.map((at, k) => ({ gid: `${r.name}-${k}`, key: `${r.name}-${k}`, repo: r.name, at: SPOTS[(k + i) % SPOTS.length] ?? at })));
      const s: Scene = {
        maxRuns: nb, release: { days: "평일", at: "10:00", window: 120, next: "", nextDay: "", hold: false }, repos, crew, items, stray: 0, strayGids: [],
        benches: Array.from({ length: Math.min(nb, 3) }, (_, i) => ({ gid: "T" + i, key: "T" + i, repo: "r0", tool: "claude", agent: "claude", stage: "in_progress" as const })),
        crates: [{ reason: "queued", n: 5, gids: [] }, { reason: "waiting_dep", n: 1, gids: [] }, { reason: "needs_repo", n: 2, gids: [] }, { reason: "unmapped", n: 1, gids: [] }],
      };
      const L = layout(s);
      const tag = `benches ${nb}, repos ${nr}, busy ${busy}`;
      ok(L.benches.length === nb && L.lanes.length === nr, tag + ": one bench per slot, one line per repo");
      ok(L.lanes.filter((l) => l.open).length === busy, tag + ": only busy repos open");
      const f = feet(L);
      for (const [n, r] of f) ok(r[0] >= 0 && r[1] >= 0 && r[2] <= L.W && r[3] <= L.H && r[0] < r[2] && r[1] < r[3], `${tag}: ${n} on the floor`);
      for (let i = 0; i < f.length; i++)
        for (let j = i + 1; j < f.length; j++) ok(!overlap(f[i][1], f[j][1]), `${tag}: ${f[i][0]} overlaps ${f[j][0]}`);
      // the whole room (floor corners up to the wall top) lands inside the canvas
      const C = canvas(L);
      for (const [gx, gy] of [[0, 0], [L.W, 0], [0, L.H], [L.W, L.H]])
        for (const z of [-8, 0, 58]) {
          const [x, y] = C.at(gx, gy, z);
          ok(x >= 0 && y >= 0 && x <= C.w && y <= C.h, `${tag}: corner ${gx},${gy},${z} inside the canvas`);
        }
      // an open line's name (runs right from its anchor, descent 0.3 em) stays above the next idle belt's name, which
      // runs up-left along its belt from the belt's end. F: label font in art px (11 CSS px * DPR / fit, 11 at DPR 2,
      // fit 2); past ~13 the idle names (0.8 apart = 12.8 px) touch each other anyway
      const F = 13;
      L.lanes.forEach((l, i) => {
        const next = L.lanes[i + 1];
        if (!l.open || !next || next.open) return;
        const [x, y] = C.at(l.belt[0] + OPEN_NAME[0], l.c + OPEN_NAME[1]);
        const [xa, ya] = C.at(next.belt[2] - IDLE_NAME[0], next.c + IDLE_NAME[1]);
        ok(x > xa || ya - (xa - x) / 2 - F > y + 1.3 * F, `${tag}: ${l.repo.name}'s name clears ${next.repo.name}'s`);
      });
    }
console.log("factory layout ok");
