import { forwardRef, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Armchair, ChartNoAxesCombined, CircleHelp, Flag, ArrowUpCircle, Brush, Inbox as InboxIcon, ClipboardList, FolderGit2, GitPullRequest, History as HistoryIcon, LayoutDashboard, ListChecks, Milestone, Minimize2, type LucideIcon, type LucideProps, LayoutGrid, Package, RefreshCw, Rocket, ScrollText, Search, Settings as SettingsIcon, Star, Timer, Workflow } from "lucide-react";
import { Button, Header, Sidebar, cn, type BreadcrumbItem, type SidebarEntry } from "./ui";
import { fetchState, getJson, post, postJson } from "./api";
import { CommandPalette } from "./components/CommandPalette";
import { UsageCard } from "./components/UsageCard";
import { ROLES, ago, deployable, indexState, milestones } from "./lib";
import type { AgentUsage, DeployRequest, InboxNotification, LanMe, Project, State } from "./types";
import { Board } from "./views/Board";
import { ChatCompactions } from "./views/ChatCompactions";
import { Help } from "./views/Help";
import { Inbox } from "./views/Inbox";
import { logout } from "./views/Lan";
import { Log } from "./views/Log";
import { Metrics } from "./views/Metrics";
import { Milestones } from "./views/Milestones";
import { Office } from "./views/Office";
import { Projects, SECTIONS, parseProjectSub, projectHref, projectOfRepo } from "./views/Projects";
import { Release } from "./views/Release";
import { RepoMap } from "./views/RepoMap";
import { Reviews } from "./views/Reviews";
import { Settings } from "./views/Settings";
import { SystemJobs } from "./views/SystemJobs";
import { SettingsHistory } from "./views/SettingsHistory";
import { Tickets } from "./views/Tickets";
import { Start } from "./views/Start";
import { Update } from "./views/Update";

const TABS = {
  inbox: { label: "수신함", icon: InboxIcon, group: null },
  board: { label: "보드", icon: LayoutDashboard, group: null },
  milestones: { label: "마일스톤", icon: Milestone, group: null },
  office: { label: "작업실", icon: Armchair, group: null },
  release: { label: "배포", icon: Rocket, group: null },
  projects: { label: "프로젝트", icon: FolderGit2, group: null },
  tickets: { label: "전체 티켓", icon: ListChecks, group: "티켓" },
  map: { label: "관계도", icon: Workflow, group: "티켓" },
  dispatch: { label: ROLES.dispatch.label, icon: ClipboardList, group: "모니터링" },
  reviews: { label: ROLES.reviews.label, icon: GitPullRequest, group: "모니터링" },
  deploy: { label: ROLES.deploy.label, icon: Package, group: "모니터링" },
  cleanup: { label: ROLES.cleanup.label, icon: Brush, group: "모니터링" },
  log: { label: "전체 로그", icon: ScrollText, group: "모니터링" },
  jobs: { label: "시스템 자동화", icon: Timer, group: "모니터링" },
  metrics: { label: "지표", icon: ChartNoAxesCombined, group: "모니터링" },
  history: { label: "설정 이력", icon: HistoryIcon, group: "모니터링" },
  "chat-compactions": { label: "채팅 압축", icon: Minimize2, group: "모니터링" },
  settings: { label: "설정", icon: SettingsIcon, group: null },
  update: { label: "업데이트", icon: ArrowUpCircle, group: null },
  start: { label: "시작하기", icon: Flag, group: null },
  help: { label: "도움말", icon: CircleHelp, group: null },
} as const;
type Tab = keyof typeof TABS;
// #tab or #tab/<sub> (프로젝트: #projects/<project id>/<chat|board|tickets|settings>/<conversation | repo>, 마일스톤: #milestones/<parent gid>,
// 시스템 자동화: #jobs/<job id>, 설정: #settings/<section>, 설정 이력: #history/<scope>).
const readTab = (): Tab => {
  const h = location.hash.slice(1).split("/")[0];
  return h in TABS ? (h as Tab) : "board";
};
const PALETTE_TABS = Object.entries(TABS).map(([key, t]) => ({ key, ...t }));
const MAC = /Mac|iPhone|iPad/.test(navigator.platform);
const readSub = () => decodeURIComponent(location.hash.slice(1).split("/").slice(1).join("/"));

/** A sidebar icon with a dot in its top-right corner: that project's agent is writing a reply. */
const withDot = (Icon: LucideIcon) => forwardRef<SVGSVGElement, LucideProps>((p, ref) => (
  <Icon ref={ref} {...p} aria-hidden><circle key="dot" cx="20" cy="4" r="4" className="fill-primary stroke-surface" strokeWidth={2} /></Icon>
));
const RunningStar = withDot(Star);
const RunningFolder = withDot(FolderGit2);

function Logo() {
  return <img src="/icon.svg" alt="toothpaste (치약)" title="toothpaste (치약)" className="h-8 w-8 shrink-0" />;
}

/** Browser notifications for new 수신함 alerts (TP-134): every alert with an id above the last one shown (kept in
 * localStorage, shared by all tabs; `tag` also stops two tabs from showing the same one twice). On the first load the
 * alerts already there only move that mark. */
function notifyNew(baseline: boolean, onRead: () => void) {
  return getJson<{ notifications: InboxNotification[] }>("/api/chat/inbox").then(({ notifications }) => {
    const seen = Number(localStorage.getItem("tp.notified") || 0);
    const fresh = notifications.filter((a) => a.id > seen).sort((a, b) => a.id - b.id);
    if (!fresh.length) return;
    localStorage.setItem("tp.notified", String(fresh[fresh.length - 1].id));
    if (baseline || typeof Notification === "undefined" || Notification.permission !== "granted") return;
    for (const a of fresh) {
      let note: Notification;
      try { note = new Notification(a.title, { body: a.body, icon: "/icon.svg", tag: `tp-${a.id}` }); } catch { return; } // Android Chrome: page notifications need a service worker
      note.onclick = () => {
        note.close();
        window.focus();
        if (/^https?:/.test(a.href)) window.open(a.href, "_blank", "noreferrer");
        else location.hash = a.href.replace(/^#/, "");
        postJson("/api/notifications/read", { id: a.id }).catch(() => {}).finally(onRead);
      };
    }
  });
}

export default function App({ me }: { me: LanMe }) {
  const [tab, setTab] = useState<Tab>(readTab);
  const [sub, setSub] = useState(readSub);
  const [s, setS] = useState<State | null>(null);
  const [usage, setUsage] = useState<AgentUsage>({});
  const usageRequest = useRef(0);
  const [error, setError] = useState("");
  const [syncing, setSyncing] = useState(false);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("tp.collapsed") === "1");
  const [mobileOpen, setMobileOpen] = useState(false);
  // the projects of the 프로젝트 page; the sidebar lists the starred ones (or the 5 last chatted in) on every page
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectsError, setProjectsError] = useState("");
  const [nAlerts, setNAlerts] = useState(0); // unread 수신함 alerts (TP-105)
  const [starred, setStarred] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem("tp.projects.starred") || "[]"); } catch { return []; }
  });
  const [palette, setPalette] = useState(false);

  const load = useCallback(() => fetchState().then((d) => { setS(d); setError(""); }).catch((e) => setError(String(e))), []);
  const loadUsage = useCallback(async () => {
    const request = ++usageRequest.current;
    const next = await getJson<AgentUsage>("/api/agents/usage").catch(() => ({}));
    if (request === usageRequest.current) setUsage(next);
  }, []);
  useEffect(() => {
    void loadUsage();
    const id = setInterval(loadUsage, 60000);
    window.addEventListener("focus", loadUsage);
    return () => {
      clearInterval(id);
      window.removeEventListener("focus", loadUsage);
    };
  }, [loadUsage]);
  useEffect(() => {
    load();
    // the office room shows people walking between stages, so it polls faster
    const id = setInterval(load, tab === "office" ? 15000 : 60000);
    return () => clearInterval(id);
  }, [load, tab]);
  useEffect(() => {
    // a repo's settings sit below the project's own: changing their section (#projects/<id>/settings/<repo>/<section>) keeps the scroll position
    const page = () => location.hash.split("/").slice(0, 3).join("/");
    let last = page();
    // tab visits, two levels deep (projects/<id>, settings/repos), for pruning unused pages (TP-136, remove ~2026-11-26)
    const view = () => {
      const [h, s] = location.hash.slice(1).split("/"), t = readTab();
      return h === t && s ? `${t}/${s}` : t;
    };
    let lastView = view();
    post("/api/view", { tab: lastView }).catch(() => {});
    const on = () => {
      if (view() !== lastView) { lastView = view(); post("/api/view", { tab: lastView }).catch(() => {}); }
      setTab(readTab()); setSub(readSub()); setMobileOpen(false);
      if (!(page() === last && /\/settings$/.test(last))) window.scrollTo(0, 0);
      last = page();
    };
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  useEffect(() => {
    // window-level so it also fires inside the chat composer's textarea; only Cmd/Ctrl+K is swallowed
    const on = (e: KeyboardEvent) => {
      // e.code: with the Korean input source e.key can be "ㅏ"; Mac uses Cmd only (Ctrl+K is a textarea line-kill there)
      if ((MAC ? e.metaKey : e.ctrlKey) && !e.altKey && !e.shiftKey && (e.code === "KeyK" || e.key.toLowerCase() === "k")) {
        e.preventDefault();
        if (!e.repeat) setPalette((v) => !v);
      }
    };
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, []);
  useEffect(() => localStorage.setItem("tp.collapsed", collapsed ? "1" : ""), [collapsed]);
  const baselined = useRef(false); // the alerts there on load are marked as shown, not shown
  const loadProjects = useCallback(() => getJson<{ projects: Project[]; notifications_unread: number }>("/api/projects")
    .then((r) => {
      const n = r.notifications_unread || 0;
      setProjects(r.projects); setNAlerts(n); setProjectsError("");
      // every poll with unread alerts, not only when the count rises: one read + one new in 15s keeps it equal
      if (n) notifyNew(!baselined.current, loadProjects).then(() => { baselined.current = true; }, () => {});
      else baselined.current = true;
    }).catch((e) => setProjectsError(String(e))), []);
  useEffect(() => {
    loadProjects();
    const id = setInterval(loadProjects, 15000);
    return () => clearInterval(id);
  }, [loadProjects]);
  useEffect(() => {
    // a new install (no project yet) opened without an address goes to 시작하기 (TP-144)
    if (projects && !projects.length && !location.hash.slice(1)) location.replace("#start");
  }, [projects]);
  const saveStars = (next: string[]) => { localStorage.setItem("tp.projects.starred", JSON.stringify(next)); return next; };
  const onStar = (id: string) => setStarred((cur) => saveStars(cur.includes(id) ? cur.filter((n) => n !== id) : [...cur, id]));
  useEffect(() => {
    // stars held repo names before TP-91: each becomes the project with that repo
    if (!projects) return;
    setStarred((cur) => {
      const next = [...new Set(cur.map((n) => (projects.some((p) => p.id === n) ? n : projectOfRepo(projects, n)?.id ?? n)))];
      return next.join() === cur.join() ? cur : saveStars(next);
    });
  }, [projects]);

  const act = async (path: string, body: unknown) => {
    try { await post(path, body); } catch (e) { alert((e as Error).message); }
    await load();
  };
  const onAssign = (gid: string, repo: string) => act("/api/assign", { ticket: gid, repo });
  const onSkip = (gid: string, skip: boolean) => act("/api/skip", { ticket: gid, skip });
  const onApprove = (gid: string, cancel: boolean, override?: string, overrideReview?: string) =>
    act("/api/approve", { ticket: gid, cancel, override, override_review: overrideReview });
  const onOverride = (gid: string, reason: string) => act("/api/check-override", { ticket: gid, reason });
  const onReply = (gid: string, text: string) => act("/api/reply", { ticket: gid, text });
  const onDrop = (gid: string) => act("/api/drop", { ticket: gid });
  const actions = { onAssign, onSkip, onApprove, onOverride, onReply, onDrop };
  const onMilestoneDone = (parent: string, cancel: boolean) => act("/api/milestone-done", { parent, cancel });
  const onHold = (hold: boolean) => act("/api/release-hold", { hold });
  const onDeploy = (request: DeployRequest) => act("/api/deploy", request);
  const onCancelDeploy = (id: number) => act("/api/deploy-cancel", { id });
  const refresh = async () => { setSyncing(true); await act("/api/refresh", {}); setSyncing(false); };

  const I = useMemo(() => (s ? indexState(s) : null), [s]);
  const leaf = (k: Tab) => ({ label: TABS[k].label, icon: TABS[k].icon, href: `#${k}`, active: tab === k, onClick: () => setMobileOpen(false) });
  const nReady = s ? deployable(s).length : 0;
  const proj = parseProjectSub(sub);
  // 프로젝트 채팅 (TP-101): no page padding, the conversation list and the chat fill the rest of the viewport
  const chatFull = !!s && tab === "projects" && proj.section === "chat" && !!projects?.some((p) => p.id === proj.id);
  const stars = (projects ?? []).filter((p) => starred.includes(p.id));
  // no star yet: the 5 repos chatted in last (ISO timestamps sort as strings; never chatted in: settings order, last)
  const shortcuts = stars.length ? stars
    : [...(projects ?? [])].sort((a, b) => (b.last_at ?? "").localeCompare(a.last_at ?? "")).slice(0, 5);
  const projectItems = [
    ...shortcuts.map((p) => {
      const star = starred.includes(p.id);
      return {
        // the Sidebar has no badge prop: the unread count goes in the label, like 배포 · N (TP-103)
        label: `${p.name}${p.unread ? ` · ${p.unread}` : ""}`, status: p.running ? "답 쓰는 중" : undefined, href: projectHref(p.id), active: tab === "projects" && proj.id === p.id, onClick: () => setMobileOpen(false),
        icon: p.running ? (star ? RunningStar : RunningFolder) : (star ? Star : FolderGit2),
      };
    }),
    // until /api/projects answers: blank rows where the shortcuts will be, so the groups below do not jump (TP-174)
    ...(projects ? [] : Array.from({ length: starred.length || 5 }, (_, i) => ({ label: `loading ${i}`, icon: FolderGit2, href: "", placeholder: true }))),
    { label: "전체 보기", icon: LayoutGrid, href: "#projects", active: tab === "projects" && !shortcuts.some((p) => p.id === proj.id), onClick: () => setMobileOpen(false) },
  ];
  const nUnread = (projects ?? []).reduce((n, p) => n + (p.unread || 0), nAlerts);
  const items: SidebarEntry[] = [
    { ...leaf("inbox"), label: nUnread ? `수신함 · ${nUnread}` : "수신함" },
    leaf("board"),
    leaf("milestones"),
    leaf("office"),
    { ...leaf("release"), label: nReady ? `배포 · ${nReady}` : "배포" },
    { label: "프로젝트", items: projectItems },
    { label: "티켓", items: [leaf("tickets"), leaf("map")] },
    { label: "모니터링", items: [leaf("metrics"), leaf("dispatch"), leaf("reviews"), leaf("deploy"), leaf("cleanup"), leaf("log"), leaf("jobs"), leaf("history"), leaf("chat-compactions")] },
    leaf("settings"),
    leaf("help"),
    // until the first project exists (afterwards 설정 and the ⌘K palette still open it)
    ...(projects && !projects.length || tab === "start" ? [leaf("start")] : []),
    // only when tp tick saw a newer release (or while on the page, so it stays after updating)
    ...(s?.meta.version.update || tab === "update"
      ? [{ ...leaf("update"), label: s?.meta.version.update ? `업데이트 ${s.meta.version.update}` : "업데이트" }] : []),
  ];
  const subLabel = tab === "milestones" && s ? (milestones(s).find((m) => m.gid === sub)?.name ?? sub) : sub;
  const hasSub = (tab === "milestones" || tab === "jobs" || tab === "history") && !!sub;
  const crumbs: BreadcrumbItem[] = [
    { label: "toothpaste", href: "#board" },
    ...(TABS[tab].group ? [{ label: TABS[tab].group! }] : []),
    ...(tab === "projects" && proj.id
      ? [{ label: TABS.projects.label, href: "#projects" },
         { label: projects?.find((p) => p.id === proj.id)?.name ?? proj.id, href: projectHref(proj.id) },
         { label: SECTIONS[proj.section] }]
      : [hasSub ? { label: TABS[tab].label, href: `#${tab}` } : { label: TABS[tab].label }, ...(hasSub ? [{ label: subLabel }] : [])]),
  ];

  return (
    <div className="flex min-h-screen bg-background">
      <Sidebar
        serviceName="toothpaste"
        version={(s?.meta.version.current ?? "").replace(/^v/, "")}
        logo={<Logo />}
        items={items}
        collapsed={collapsed}
        onCollapsedChange={setCollapsed}
        mobileOpen={mobileOpen}
        onMobileClose={() => setMobileOpen(false)}
        footer={<UsageCard usage={usage} collapsed={collapsed} />}
      />
      <CommandPalette open={palette} onClose={() => setPalette(false)} tabs={PALETTE_TABS} />
      {/* the office room fits the viewport height: the column is exactly one screen tall and main scrolls (only when the list is open) */}
      <div className={cn("flex min-w-0 flex-1 flex-col lg:transition-[margin] lg:duration-300", tab === "office" || chatFull ? "h-dvh" : "min-h-screen", collapsed ? "lg:ml-[72px]" : "lg:ml-[260px]")}>
        <Header
          breadcrumbs={crumbs}
          onMenuClick={() => setMobileOpen(true)}
          actions={
            <>
              {/* first, so its width (방금 <-> 12분 전) never moves the buttons to its right; always there, so nothing jumps when state arrives (TP-174) */}
              <span className="hidden min-w-[16rem] whitespace-nowrap text-right text-xs font-medium tabular-nums text-text-muted sm:inline">
                {s && <>티켓 동기화 {ago(s.meta.last_sync)} · GitHub 스캔 {ago(s.meta.last_scan)}</>}
              </span>
              {/* input-looking trigger; below sm only the magnifier shows */}
              <button type="button" onClick={() => setPalette(true)} aria-label={`검색 (${MAC ? "⌘" : "Ctrl "}K)`}
                className="flex h-8 items-center gap-2 rounded-lg border border-border bg-surface px-2 text-sm text-text-muted hover:bg-grey-50 sm:w-56 sm:px-3">
                <Search className="h-4 w-4 shrink-0" />
                <span className="hidden flex-1 text-left sm:inline">검색…</span>
                <kbd className="hidden rounded border border-border px-1.5 font-sans text-[11px] sm:inline">{MAC ? "⌘K" : "Ctrl K"}</kbd>
              </button>
              {me.login && (
                <>
                  <span className="whitespace-nowrap font-mono text-xs font-semibold text-text-main" title="GitHub 로그인 (로컬망 공유)">{me.login}</span>
                  <Button size="sm" variant="secondary" onClick={logout}>로그아웃</Button>
                </>
              )}
              <Button size="sm" variant="secondary" onClick={refresh} disabled={!s} loading={syncing}>
                <RefreshCw className="h-4 w-4" />
                지금 동기화
              </Button>
            </>
          }
        />
        <main className={cn("min-w-0 flex-1", chatFull ? "flex min-h-0 flex-col" : "p-4 sm:p-6", tab === "office" && "flex min-h-0 flex-col overflow-y-auto")}>
          {error && !s && <p className="text-sm text-danger-700">데이터를 불러오지 못했어요: {error}</p>}
          {!s || !I ? (
            !error && <p className="text-sm text-text-muted">불러오는 중…</p>
          ) : tab === "inbox" ? (
            <Inbox onChanged={loadProjects} />
          ) : tab === "board" ? (
            <Board s={s} I={I} {...actions} />
          ) : tab === "milestones" ? (
            <Milestones s={s} I={I} sub={sub} {...actions} onMilestoneDone={onMilestoneDone} onReload={load} />
          ) : tab === "office" ? (
            <Office s={s} />
          ) : tab === "release" ? (
            <Release s={s} onHold={onHold} onDeploy={onDeploy} onCancelDeploy={onCancelDeploy} />
          ) : tab === "tickets" ? (
            <Tickets s={s} />
          ) : tab === "map" ? (
            <RepoMap s={s} />
          ) : tab === "settings" ? (
            <Settings sub={sub} onChanged={() => { load(); loadProjects(); void loadUsage(); }} />
          ) : tab === "projects" ? (
            <Projects s={s} I={I} sub={sub} onChanged={load} actions={actions} list={projects} error={projectsError}
              onReload={loadProjects} starred={starred} onStar={onStar} />
          ) : tab === "start" ? (
            <Start projects={projects} onChanged={() => { load(); loadProjects(); void loadUsage(); }} />
          ) : tab === "help" ? (
            <Help />
          ) : tab === "update" ? (
            <Update onDone={load} />
          ) : tab === "jobs" ? (
            <SystemJobs sub={sub} />
          ) : tab === "metrics" ? (
            <Metrics />
          ) : tab === "history" ? (
            <SettingsHistory sub={sub} />
          ) : tab === "chat-compactions" ? (
            <ChatCompactions />
          ) : tab === "reviews" ? (
            <>
              {/* above the log: the log ends with the automations table, which loads late and would push this down (TP-174) */}
              <section className="mb-8">
                <h2 className="mb-3 text-base font-bold text-text-main sm:text-lg">리뷰 결과</h2>
                <Reviews s={s} />
              </section>
              <Log s={s} role="reviews" />
            </>
          ) : tab === "dispatch" || tab === "deploy" || tab === "cleanup" ? (
            <Log s={s} role={tab} />
          ) : (
            <Log s={s} />
          )}
        </main>
      </div>
    </div>
  );
}
