import {
  Activity, Armchair, ArrowUpCircle, Brush, ChartNoAxesCombined, CircleHelp, ClipboardList, Flag,
  FolderGit2, GitPullRequest, History as HistoryIcon, Inbox as InboxIcon, LayoutDashboard, ListChecks,
  Milestone, Minimize2, Package, Rocket, ScrollText, Settings as SettingsIcon, Timer, Workflow,
} from "lucide-react";
import { ROLES } from "../lib";
import type { Project } from "../types";

const COMPACTIONS_ROUTE = "metrics/chat-compactions";
export const MONITORING = "모니터링";
export const TABS = {
  inbox: { label: "수신함", icon: InboxIcon, group: null },
  board: { label: "보드", icon: LayoutDashboard, group: null },
  milestones: { label: "마일스톤", icon: Milestone, group: null },
  office: { label: "작업실", icon: Armchair, group: null },
  release: { label: "배포", icon: Rocket, group: null },
  projects: { label: "프로젝트", icon: FolderGit2, group: null },
  tickets: { label: "전체 티켓", icon: ListChecks, group: "티켓" },
  map: { label: "관계도", icon: Workflow, group: "티켓" },
  monitoring: { label: MONITORING, icon: Activity, group: null },
  metrics: { label: "지표", icon: ChartNoAxesCombined, group: MONITORING },
  dispatch: { label: ROLES.dispatch.label, icon: ClipboardList, group: MONITORING },
  reviews: { label: ROLES.reviews.label, icon: GitPullRequest, group: MONITORING },
  deploy: { label: ROLES.deploy.label, icon: Package, group: MONITORING },
  cleanup: { label: ROLES.cleanup.label, icon: Brush, group: MONITORING },
  log: { label: "전체 로그", icon: ScrollText, group: MONITORING },
  jobs: { label: "시스템 자동화", icon: Timer, group: MONITORING },
  history: { label: "설정 이력", icon: HistoryIcon, group: MONITORING },
  settings: { label: "설정", icon: SettingsIcon, group: null },
  update: { label: "업데이트", icon: ArrowUpCircle, group: null },
  start: { label: "시작하기", icon: Flag, group: null },
  help: { label: "도움말", icon: CircleHelp, group: null },
} as const;
export type Tab = keyof typeof TABS;

export const PALETTE_TABS = [
  ...Object.entries(TABS).map(([key, t]) => ({ key, ...t })),
  { key: COMPACTIONS_ROUTE, label: "채팅 압축", icon: Minimize2, group: `${MONITORING} → ${TABS.metrics.label}` },
];
export const MONITORING_TABS = PALETTE_TABS.filter((t) => t.group === MONITORING);

/** #tab or #tab/<sub>. Keep the original hash for bookmarks and browser history. */
export function parseHash(hash: string) {
  const route = hash === "#chat-compactions" ? COMPACTIONS_ROUTE : hash.slice(1);
  const [key, ...segments] = route.split("/");
  const tab = Object.hasOwn(TABS, key) ? (key as Tab) : "board";
  const view = key === tab && segments[0] ? `${tab}/${segments[0]}` : tab;
  const rawSub = segments.join("/");
  let sub = rawSub;
  try { sub = decodeURIComponent(rawSub); } catch { /* keep malformed bookmarks navigable */ }
  // A repo's settings sit below the project's own. Keep scroll when switching repo settings sections.
  const scrollScope = hash.split("/").slice(0, 3).join("/");
  return { hash, tab, sub, view, scrollScope };
}

/** #projects/<project id>/<chat|board|milestones|tickets|settings>/<conversation id | repo (settings)>/<section of the repo's settings>: one project's chat
 * with an agent, its ticket board, its milestones, all its tickets (완료 포함) and its settings (TP-91: a project has 1..N repos) */
export const SECTIONS = { chat: "채팅", board: "보드", milestones: "마일스톤", tickets: "전체 티켓", settings: "설정" } as const;
export type ProjectSection = keyof typeof SECTIONS;
export const parseProjectSub = (sub: string) => {
  const [id = "", section = "", conv = "", part = ""] = sub.split("/");
  return { id, section: Object.hasOwn(SECTIONS, section) ? (section as ProjectSection) : ("chat" as const), conv, part };
};
export const projectHref = (id: string, section: ProjectSection = "chat", conv = "", part = "") =>
  `#projects/${encodeURIComponent(id)}/${section}${conv ? `/${encodeURIComponent(conv)}${part ? `/${part}` : ""}` : ""}`;
/** before TP-91 the address held a repo name: the project with that repo (a real one before 미분류) */
export const projectOfRepo = (list: Project[], repo: string) =>
  [...list].sort((a, b) => +a.unsorted - +b.unsorted).find((p) => p.repos.some((r) => r.name === repo));

export function routeReplacement({ hash, tab, sub }: ReturnType<typeof parseHash>, projects: Project[] | null) {
  if (tab === "settings" && sub === "history") return "#history"; // TP-168
  if (!hash && projects?.length === 0) return "#start"; // TP-144
  if (tab !== "projects" || !projects) return;
  const { id, section, conv } = parseProjectSub(sub);
  if (!id || projects.some((p) => p.id === id)) return;
  const project = projectOfRepo(projects, id);
  if (project) return projectHref(project.id, section, section === "settings" ? id : conv);
}
