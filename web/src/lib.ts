import type { Project, State, Ticket } from "./types";

export type Status = "draft" | "warning" | "danger" | "success" | "info";

export const STAGES: Record<string, [string, Status]> = {
  conflict: ["충돌 대기", "danger"],
  rework: ["수정 대기", "warning"],
  queued: ["투입 대기", "info"],
  needs_repo: ["레포 선택 필요", "warning"],
  unmapped: ["매핑 없음", "warning"],
  waiting_dep: ["선행 작업 대기", "warning"],
  ms_hold: ["작업 완료 대기", "warning"],
  collecting: ["하위작업 진행 중", "info"],
  dispatched: ["투입됨", "info"],
  in_progress: ["작업 중", "info"],
  blocked: ["막힘", "danger"],
  agent_done: ["에이전트 완료", "success"],
  pr_open: ["리뷰 대기", "info"],
  reviewed: ["리뷰 완료", "success"],
  approving: ["staging 머지 중", "info"],
  milestone: ["마일스톤에 합침", "success"],
  deploying: ["배포 중", "info"],
  staged: ["배포 대기", "info"],
  merged: ["배포 대기", "info"],
  done: ["완료", "success"],
  closed: ["닫힘", "draft"],
  skipped: ["건너뜀", "draft"],
  open: ["미투입", "draft"],
};
export const PENDING = ["queued", "needs_repo", "unmapped", "waiting_dep"];
/** stages with a live, unmerged run; mirrors pipeline.DROPPABLE */
export const DROPPABLE = ["conflict", "rework", "dispatched", "in_progress", "blocked", "agent_done", "pr_open", "reviewed", "approving"];
/** color: the column's segment in the board summary bar (TP-106) */
export const BOARD: { title: string; color: string; stages: string[] }[] = [
  { title: "대기", color: "bg-grey-300", stages: ["conflict", "rework", ...PENDING, "ms_hold"] },
  { title: "작업 중", color: "bg-primary-500", stages: ["collecting", "dispatched", "in_progress"] },
  { title: "막힘", color: "bg-danger-500", stages: ["blocked"] },
  { title: "PR / 리뷰 대기", color: "bg-warning-500", stages: ["agent_done", "pr_open"] },
  { title: "리뷰 완료", color: "bg-success-500", stages: ["reviewed", "approving"] },
  { title: "배포 대기", color: "bg-info-300", stages: ["milestone", "deploying", "staged", "merged"] },
  { title: "완료", color: "bg-grey-500", stages: ["done", "closed"] },
];
/** the board's 완료 column drops tickets finished longer ago than this (TP-81); 전체 티켓 still lists them */
export const DONE_KEEP_DAYS = 7;
export const staleDone = (t: Ticket) =>
  ["done", "closed"].includes(t.stage) && !!t.done_at && Date.now() - new Date(t.done_at).getTime() > DONE_KEEP_DAYS * 864e5;
// milestone subtasks live on the 마일스톤 page; boards show their milestone ticket (ms:<gid>:<repo>) instead
export const onBoard = (t: Ticket) => !(t.in_milestone && t.parent_gid);
export const VERDICT: Record<string, [string, Status]> = {
  approve: ["괜찮음", "success"],
  changes: ["수정 필요", "warning"],
  blocked: ["막힘", "danger"],
};

/** repos whose staging holds something production lacks (diffs.py) */
export const pendingItems = (d: { items_json: string | null }) =>
  (JSON.parse(d.items_json || "[]") as { kind: string; live?: boolean }[]).filter((i) => !i.live && i.kind !== "merge");
export const deployable = (s: State) => (s.staging_diffs ?? []).filter((d) => d.verdict === "ready" && pendingItems(d).length > 0);

export const LIVE_RELEASE = ["requested", "running", "pending", "deploying"];
export const latestPr = (s: State, gid: string) => s.prs.filter((p) => p.ticket_gid === gid).sort((a, b) => b.number - a.number)[0];
/** the dashboard deploy / train run that currently includes this ticket */
export const liveReleaseOf = (s: State, gid: string) =>
  s.releases.find(
    (r) => LIVE_RELEASE.includes(r.status) && (r.ticket_gid === gid || (r.prs_json || "").includes(`"ticket_gid": "${gid}"`)),
  );

export const today = () => new Date().toISOString().slice(0, 10);

/** monitoring roles: one Orca automation each (matched by name); events by meaning, not by which automation emitted them.
 * `stage` events are noisy and belong to nobody, so they only show in the full log. */
export const ROLES = {
  dispatch: {
    label: "배차 담당",
    re: /dispatch/i,
    kinds: ["tagged", "assign", "dispatch", "adopt", "rework", "rework_cap", "conflict_rework", "unblock", "abandoned",
      "agent_exit", "agent_timeout", "violation", "asana_write"],
    err: /^cmd_\w*dispatch|github issues|worktree create failed|launch failed/,
  },
  reviews: { label: "리뷰어", re: /review/i, kinds: ["review", "conflict", "retarget"], err: /^(cmd_\w*review|gh pr list|retarget)\b/ },
  deploy: {
    label: "배포 담당",
    re: /staging|deploy|release/i,
    kinds: ["approve", "merge", "deploy", "release", "diff"],
    // anchored: other messages mention e.g. "deploy.yml" (cleanup) without being a deploy error
    err: /^(cmd_\w*diff|approvals|track|release|deploys?)\b|staging merge failed/,
  },
  cleanup: { label: "청소부", re: /cleanup/i, kinds: ["cleanup"], err: /^(cmd_\w*cleanup|cleanup)\b/ },
} as const;
export type Role = keyof typeof ROLES;
export const ofRole = (role: Role, e: { kind: string; message: string }) =>
  (ROLES[role].kinds as readonly string[]).includes(e.kind) || (e.kind === "error" && ROLES[role].err.test(e.message));
export const isOverdue = (d: string | null) => !!d && d < today();

export function ago(v?: string | number | null): string {
  if (!v) return "-";
  const m = Math.round((Date.now() - new Date(v).getTime()) / 60000);
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  if (m < 1440) return `${Math.round(m / 60)}시간 전`;
  return `${Math.round(m / 1440)}일 전`;
}

export const byDue = (a: Ticket, b: Ticket) => ((a.due_on || "9999") > (b.due_on || "9999") ? 1 : -1);
export const openTickets = (s: State) => s.tickets.filter((t) => !t.completed && t.assigned);
export const reposFor = (s: State, projectGid: string | null) =>
  // Asana and Notion have project keys. A null key must never match an unrelated project's null key.
  s.project_map.filter((m) => projectGid != null && m.project_gid === projectGid && m.repo);

export function indexState(s: State) {
  const runBy: Record<string, State["runs"]> = {};
  const prBy: Record<string, State["prs"]> = {};
  const revBy: Record<string, State["reviews"]> = {};
  for (const r of s.runs) (runBy[r.ticket_gid] ??= []).push(r);
  for (const p of s.prs) (prBy[p.ticket_gid] ??= []).push(p);
  for (const v of s.reviews) (revBy[v.pr_url] ??= []).push(v);
  return { runBy, prBy, revBy };
}
export type Index = ReturnType<typeof indexState>;

/** the repos a ticket belongs to (TP-76, 프로젝트 보드): repo_override (chosen, GitHub issue, milestone ticket) →
 * its project's only mapped repo → repos it ran in or opened PRs in → every mapped candidate (레포 선택 필요) →
 * the repo in a milestone gid ms:<parent>:<repo>. Empty = no repo known. */
export function ticketRepos(s: State, I: Index, t: Ticket): string[] {
  if (t.repo_override) return [t.repo_override];
  const mapped = [...new Set(reposFor(s, t.project_gid).map((m) => m.repo!))];
  if (mapped.length === 1 || t.stage === "needs_repo") return mapped; // needs_repo: undecided even if an abandoned run exists
  const used = [...(I.runBy[t.gid] ?? []), ...(I.prBy[t.gid] ?? [])].map((r) => r.repo);
  if (used.length) return [...new Set(used)];
  if (mapped.length) return mapped;
  return isMs(t.gid) ? [t.gid.split(":").slice(2).join(":")] : [];
}

/** a project's tickets (TP-91): a ticket of an Asana project that a tp project links belongs to that project only;
 * any other ticket (GitHub issue, unlinked Asana project) to every project that has one of its repos */
export function inProject(s: State, I: Index, t: Ticket, p: Project): boolean {
  const owners = t.project_gid ? s.project_map.filter((m) => m.project_gid === t.project_gid).map((m) => m.project_id) : [];
  if (owners.length) return owners.includes(p.id);
  const repos = new Set(p.repos.map((r) => r.name));
  return ticketRepos(s, I, t).some((r) => repos.has(r));
}

/** milestone detail columns (2026-10-30) */
export const MILESTONE_COLS: { title: string; stages: string[] }[] = [
  { title: "대기", stages: ["conflict", "rework", ...PENDING, "collecting", "open"] },
  { title: "작업 중", stages: ["dispatched", "in_progress"] },
  { title: "막힘", stages: ["blocked"] },
  { title: "리뷰 중", stages: ["agent_done", "pr_open", "reviewed", "approving"] },
  { title: "반영 완료", stages: ["milestone", "deploying", "staged", "merged", "done", "closed", "skipped"] },
];
/** milestone tickets (milestone.py): gid ms:<parent gid>:<repo>, one per repo */
export const isMs = (gid: string) => gid.startsWith("ms:");
/** subtask stages that no longer hold a milestone back (milestone.FINISHED) */
export const MS_FINISHED = ["milestone", "done", "closed", "skipped", "staged", "merged", "deploying"];
/** tagged subtasks still in progress (milestone.unfinished): 마일스톤 작업 완료 waits for them */
export const busySubtasks = (m: Milestone) =>
  m.subtasks.filter((t) => t.has_tag && t.assigned && !t.completed && !MS_FINISHED.includes(t.stage));
export interface Milestone {
  gid: string;
  name: string;
  url: string | null;
  project: string | null;
  subtasks: Ticket[];
  ms: Ticket[];
}
/** Asana parents tagged milestone_tag, found from their own row, their milestone tickets or their subtasks */
export function milestones(s: State): Milestone[] {
  const tag = String(s.meta.settings.milestone_tag || "tp-milestone").toLowerCase();
  const ids = new Set<string>();
  for (const t of s.tickets) {
    if (t.parent_gid && (isMs(t.gid) || t.in_milestone)) ids.add(t.parent_gid);
    else if ((JSON.parse(t.tags || "[]") as string[]).some((x) => x.toLowerCase() === tag)) ids.add(t.gid);
  }
  return [...ids].map((gid) => {
    const parent = s.tickets.find((t) => t.gid === gid);
    const kids = s.tickets.filter((t) => t.parent_gid === gid);
    return {
      gid,
      name: parent?.name ?? kids[0]?.parent_name ?? gid,
      url: parent?.url ?? kids.find((k) => isMs(k.gid))?.url ?? null,
      project: parent?.project_name ?? kids[0]?.project_name ?? null,
      subtasks: kids.filter((k) => !isMs(k.gid)),
      ms: kids.filter((k) => isMs(k.gid)).sort((a, b) => a.gid.localeCompare(b.gid)),
    };
  });
}
