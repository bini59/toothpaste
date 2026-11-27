import { forwardRef, lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { FolderGit2, type LucideIcon, type LucideProps, LayoutGrid, RefreshCw, Search, Star } from "lucide-react";
import { Button, Header, Sidebar, cn, type BreadcrumbItem, type SidebarEntry, type SidebarItem } from "./ui";
import { MONITORING, MONITORING_TABS, PALETTE_TABS, SECTIONS, TABS, parseProjectSub, projectHref, projectOfRepo, type Tab } from "./app/navigation";
import { useAgentUsage, useDashboardProjects, useDashboardState } from "./app/useDashboardData";
import { useCommandPalette, useHashNavigation } from "./app/useNavigation";
import { CommandPalette } from "./components/CommandPalette";
import { UsageCard } from "./components/UsageCard";
import { ago, deployable, indexState, milestones } from "./lib";
import type { DeployRequest, LanMe } from "./types";
import { Board } from "./views/Board";
import { Help } from "./views/Help";
import { Inbox } from "./views/Inbox";
import { logout } from "./views/Lan";
import { Log } from "./views/Log";
import { Milestones } from "./views/Milestones";
import { Monitoring } from "./views/Monitoring";
import { Office } from "./views/Office";
import { Projects } from "./views/Projects";
import { Release } from "./views/Release";
import { RepoMap } from "./views/RepoMap";
import { Reviews } from "./views/Reviews";
import { Settings } from "./views/Settings";
import { SystemJobs } from "./views/SystemJobs";
import { SettingsHistory } from "./views/SettingsHistory";
import { Tickets } from "./views/Tickets";
import { Start } from "./views/Start";
import { Update } from "./views/Update";

const Metrics = lazy(() => import("./views/Metrics").then(
  (module) => ({ default: module.Metrics }),
  () => ({ default: MetricsLoadError }),
));

function MetricsLoadError() {
  return (
    <div className="space-y-3">
      <p role="alert" className="text-sm text-danger-700">지표 화면을 불러오지 못했어요.</p>
      <Button size="sm" variant="secondary" onClick={() => location.reload()}>페이지 새로고침</Button>
    </div>
  );
}

/** A sidebar icon with a dot in its top-right corner: that project's agent is writing a reply. */
const withDot = (Icon: LucideIcon) => forwardRef<SVGSVGElement, LucideProps>((p, ref) => (
  <Icon ref={ref} {...p} aria-hidden><circle key="dot" cx="20" cy="4" r="4" className="fill-primary stroke-surface" strokeWidth={2} /></Icon>
));
const RunningStar = withDot(Star);
const RunningFolder = withDot(FolderGit2);

function Logo() {
  return <img src="/icon.svg" alt="toothpaste (치약)" title="toothpaste (치약)" className="h-8 w-8 shrink-0" />;
}

export default function App({ me }: { me: LanMe }) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("tp.collapsed") === "1");
  const [mobileOpen, setMobileOpen] = useState(false);
  const closeMobile = useCallback(() => setMobileOpen(false), []);
  const { projects, projectsError, nAlerts, loadProjects } = useDashboardProjects();
  const { tab, sub } = useHashNavigation(projects, closeMobile);
  const { s, error, syncing, load, act, refresh } = useDashboardState(tab);
  const { usage, loadUsage } = useAgentUsage();
  const { open: palette, setOpen: setPalette, isMac } = useCommandPalette();
  const [starred, setStarred] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem("tp.projects.starred") || "[]"); } catch { return []; }
  });
  useEffect(() => localStorage.setItem("tp.collapsed", collapsed ? "1" : ""), [collapsed]);
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

  const I = useMemo(() => (s ? indexState(s) : null), [s]);
  const leaf = (k: Tab) => ({ label: TABS[k].label, icon: TABS[k].icon, href: `#${k}`, active: tab === k, onClick: closeMobile });
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
        label: `${p.name}${p.unread ? ` · ${p.unread}` : ""}`, status: p.running ? "답 쓰는 중" : undefined, href: projectHref(p.id), active: tab === "projects" && proj.id === p.id, onClick: closeMobile,
        icon: p.running ? (star ? RunningStar : RunningFolder) : (star ? Star : FolderGit2),
      };
    }),
    // until /api/projects answers: blank rows where the shortcuts will be, so the groups below do not jump (TP-174)
    ...(projects ? [] : Array.from({ length: starred.length || 5 }, (_, i) => ({ label: `loading ${i}`, icon: FolderGit2, href: "", placeholder: true }))),
    { label: "전체 보기", icon: LayoutGrid, href: "#projects", active: tab === "projects" && !shortcuts.some((p) => p.id === proj.id), onClick: closeMobile },
  ];
  const nUnread = (projects ?? []).reduce((n, p) => n + (p.unread || 0), nAlerts);
  const group = TABS[tab].group;
  const items: SidebarEntry[] = [
    { ...leaf("inbox"), label: nUnread ? `수신함 · ${nUnread}` : "수신함" },
    leaf("board"),
    leaf("milestones"),
    leaf("office"),
    { ...leaf("release"), label: nReady ? `배포 · ${nReady}` : "배포" },
    { label: "프로젝트", items: projectItems },
    { label: "티켓", items: [leaf("tickets"), leaf("map")] },
    // until the first project exists (afterwards 설정 and the ⌘K palette still open it)
    ...(projects && !projects.length || tab === "start" ? [leaf("start")] : []),
    // only when tp tick saw a newer release (or while on the page, so it stays after updating)
    ...(s?.meta.version.update || tab === "update"
      ? [{ ...leaf("update"), label: s?.meta.version.update ? `업데이트 ${s.meta.version.update}` : "업데이트" }] : []),
  ];
  const pinnedItems: SidebarItem[] = [
    { ...leaf("monitoring"), active: group === MONITORING ? "location" : tab === "monitoring" },
    leaf("settings"),
    leaf("help"),
  ];
  const subLabel = tab === "milestones" && s ? (milestones(s).find((m) => m.gid === sub)?.name ?? sub) : sub;
  const hasSub = (tab === "milestones" || tab === "jobs" || tab === "history") && !!sub;
  const crumbs: BreadcrumbItem[] = [
    { label: "toothpaste", href: "#board" },
    ...(group ? [{ label: group, href: group === MONITORING ? "#monitoring" : undefined }] : []),
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
        pinnedItems={pinnedItems}
        collapsed={collapsed}
        onCollapsedChange={setCollapsed}
        mobileOpen={mobileOpen}
        onMobileClose={closeMobile}
        footer={<UsageCard usage={usage} collapsed={collapsed} />}
      />
      <CommandPalette open={palette} onClose={() => setPalette(false)} tabs={PALETTE_TABS} />
      {/* the office room fits the viewport height: the column is exactly one screen tall and main scrolls (only when the list is open) */}
      <div className={cn("flex min-w-0 flex-1 flex-col lg:transition-[margin] lg:duration-300", tab === "metrics" ? "h-dvh overflow-hidden" : tab === "office" || chatFull ? "h-dvh" : "min-h-screen", collapsed ? "lg:ml-[72px]" : "lg:ml-[260px]")}>
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
              <button type="button" onClick={() => setPalette(true)} aria-label={`검색 (${isMac ? "⌘" : "Ctrl "}K)`}
                className="flex h-8 items-center gap-2 rounded-lg border border-border bg-surface px-2 text-sm text-text-muted hover:bg-grey-50 sm:w-56 sm:px-3">
                <Search className="h-4 w-4 shrink-0" />
                <span className="hidden flex-1 text-left sm:inline">검색…</span>
                <kbd className="hidden rounded border border-border px-1.5 font-sans text-[11px] sm:inline">{isMac ? "⌘K" : "Ctrl K"}</kbd>
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
        <main className={cn("min-w-0 flex-1", chatFull ? "flex min-h-0 flex-col" : "p-4 sm:p-6", tab === "office" && "flex min-h-0 flex-col overflow-y-auto",
          tab === "metrics" && "flex min-h-0 flex-col")}>
          {tab === "monitoring" ? (
            <Monitoring title={MONITORING} items={MONITORING_TABS} />
          ) : tab === "metrics" ? (
            <Suspense fallback={<p role="status" className="text-sm text-text-muted">지표를 불러오는 중…</p>}>
              <Metrics sub={sub} />
            </Suspense>
          ) : !s || !I ? (
            error ? <p className="text-sm text-danger-700">데이터를 불러오지 못했어요: {error}</p>
              : <p className="text-sm text-text-muted">불러오는 중…</p>
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
          ) : tab === "history" ? (
            <SettingsHistory sub={sub} />
          ) : tab === "reviews" ? (
            <>
              {/* above the log: the log ends with the automations table, which loads late and would push this down (TP-174) */}
              <section className="mb-8">
                <h2 className="mb-3 text-base font-bold text-text-main sm:text-lg">리뷰 결과</h2>
                <Reviews s={s} syncing={syncing} />
              </section>
              <Log s={s} role="reviews" syncing={syncing} />
            </>
          ) : tab === "dispatch" || tab === "deploy" || tab === "cleanup" ? (
            <Log s={s} role={tab} syncing={syncing} />
          ) : (
            <Log s={s} syncing={syncing} />
          )}
        </main>
      </div>
    </div>
  );
}
