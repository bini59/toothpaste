// State (/api/state) + the tick snapshot (/api/automations) -> the factory Scene (TP-127). Settings are only read.
import { LIVE_RELEASE, PENDING, indexState, latestPr, ticketRepos } from "../../lib";
import type { AutomationsResponse, CheckRow, State } from "../../types";
import type { Bench, BenchStage, Crate, Crew, CrewKey, Item, Light, Reason, Repo, Scene, Spot } from "./layout";

const BENCH: string[] = ["dispatched", "in_progress", "blocked"] satisfies BenchStage[];
const SPOTS: string[] = ["agent_done", "pr_open", "reviewed", "approving", "rework", "conflict", "milestone", "ms_hold",
  "collecting", "staged", "merged", "deploying"] satisfies Spot[];
const DAY = ["월", "화", "수", "목", "금", "토", "일"]; // release.weekdays: Python weekday(), 0 = Monday

const days = (w: number[]) => (w.join() === "0,1,2,3,4" ? "평일" : w.length === 7 ? "매일" : w.map((d) => DAY[d]).join("·"));
const fmt = (v: string, timeZone?: string) => new Date(v).toLocaleString("ko-KR", { timeZone, month: "numeric", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit" });

/** one check row (or all CI rows) -> gate lamp */
function light(rows: CheckRow[]): Light {
  if (!rows.length) return "none";
  if (rows.some((r) => ["fail", "timeout", "error"].includes(r.status))) return "fail";
  if (rows.some((r) => ["running", "ci_pending"].includes(r.status))) return "run";
  return "pass";
}

/** `tp tick`'s last result for an automation (cli.cmd_tick lastRunStatus) */
function crewOf(snap: AutomationsResponse | null, key: CrewKey, name: string): Crew {
  const a = snap?.automations?.find((x) => x.id === key);
  const st = snap?.items?.find((x) => x.automationId === key)?.usageSummary?.lastRunStatus ?? "";
  const last = a?.lastRunAt;
  if (!a || !a.enabled) return { key, name, status: "off", note: a ? "꺼짐" : "기록 없음", last };
  if (/fail|error/i.test(st)) return { key, name, status: "fail", note: "실패", last };
  if (st === "running") return { key, name, status: "run", note: "실행 중", last };
  return { key, name, status: "skip", last, note: (st === "skipped_precheck" ? "할 일 없음" : st === "completed" ? "완료" : st || "대기") };
}

export function buildScene(s: State, snap: AutomationsResponse | null): Scene {
  const I = indexState(s);
  const set = s.meta.settings;
  const rel = (set.release ?? {}) as { weekdays?: number[]; at?: string; window_min?: number; timezone?: string };
  const next = fmt(s.meta.next_release_at, rel.timezone); // release.at is in the train's time zone, so is this
  const repos: Repo[] = s.repos.map((r) => ({
    name: r.name,
    tool: r.agent?.tool ?? "",
    agent: [r.agent?.tool, r.agent?.model].filter(Boolean).join("·"),
    gates: [...(r.checks ?? []), ...(r.ci ? ["CI"] : [])],
    train: !!r.deploy?.train,
    modes: r.deploy?.modes ?? [],
    merge: r.deploy?.merge ?? "",
    maxRework: r.max_rework ?? 0,
  }));
  const byName = Object.fromEntries(repos.map((r) => [r.name, r]));
  const runOf = (gid: string) => {
    const runs = I.runBy[gid] ?? [];
    return runs.filter((r) => r.state === "active").at(-1) ?? runs.at(-1);
  };
  const repoOf = (t: State["tickets"][number]) => runOf(t.gid)?.repo ?? latestPr(s, t.gid)?.repo ?? ticketRepos(s, I, t)[0];

  const benches: Bench[] = s.tickets
    .filter((t) => BENCH.includes(t.stage) && runOf(t.gid))
    .sort((a, b) => (runOf(a.gid)!.created_at > runOf(b.gid)!.created_at ? 1 : -1))
    .map((t) => {
      const repo = runOf(t.gid)!.repo;
      return { gid: t.gid, key: t.key, repo, tool: byName[repo]?.tool ?? "", agent: byName[repo]?.agent ?? "", stage: t.stage as BenchStage };
    });

  const items: Item[] = [];
  const strayGids: string[] = [];
  for (const t of s.tickets) {
    if (!SPOTS.includes(t.stage)) continue;
    const repo = repoOf(t);
    if (!repo || !byName[repo]) {
      strayGids.push(t.gid); // no line to draw it on: counted on the floor instead
      continue;
    }
    const it: Item = { gid: t.gid, key: t.key, repo, at: t.stage as Spot };
    const pr = latestPr(s, t.gid);
    if (t.stage === "pr_open") {
      const rows = pr?.checks?.rows ?? [];
      it.lights = byName[repo].gates.map((g) => light(g === "CI" ? rows : rows.filter((r) => r.name === g)));
    }
    if (t.stage === "rework" && pr) it.round = pr.rework_count;
    items.push(it);
  }

  const crates: Crate[] = (PENDING as Reason[])
    .map((reason) => {
      const gids = s.tickets.filter((t) => t.stage === reason).map((t) => t.gid);
      return { reason, n: gids.length, gids };
    })
    .filter((c) => c.n);

  // the release / deploy robot has no tick automation: it follows the train and dashboard deploys (releases table)
  const live = s.releases.some((r) => LIVE_RELEASE.includes(r.status));
  // a failure lies the robot down for a day, then it is history (the 배포 page keeps it)
  const last = s.releases[0]; // newest first
  const lastAt = last && (last.finished_at || last.started_at || last.requested_at);
  const base = { key: "release", name: "출하", last: lastAt || undefined } as const;
  const release: Crew = s.meta.release_hold ? { ...base, status: "off", note: "보류" }
    : live ? { ...base, status: "run", note: "출하 중" }
    : last && ["failed", "aborted"].includes(last.status) && lastAt && Date.now() - new Date(lastAt).getTime() < 864e5 ? { ...base, status: "fail", note: `${last.repo} 실패` }
    : { ...base, status: "skip", note: `다음 ${next}` };

  return {
    maxRuns: Number(set.max_active_runs) || 0,
    release: {
      days: days(rel.weekdays ?? []),
      at: rel.at ?? "",
      window: rel.window_min ?? 0,
      next,
      // the board shows the day only: its first line already has the train's time
      nextDay: new Date(s.meta.next_release_at).toLocaleDateString("ko-KR", { timeZone: rel.timezone, month: "numeric", day: "numeric", weekday: "short" }),
      hold: s.meta.release_hold,
    },
    repos,
    benches,
    crates,
    items,
    stray: strayGids.length,
    strayGids,
    crew: [
      crewOf(snap, "dispatch", "배차"),
      crewOf(snap, "review", "리뷰"),
      crewOf(snap, "diff", "staging diff"),
      release,
      crewOf(snap, "cleanup", "정리"),
    ],
  };
}
