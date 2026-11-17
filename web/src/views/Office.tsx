import { Button, Card, cn } from "../ui";
import { useEffect, useMemo, useRef, useState } from "react";
import { fetchAutomations } from "../api";
import { CheckBadges } from "../components/CheckBadges";
import { StageBadge } from "../components/StageBadge";
import { ago, latestPr } from "../lib";
import type { AutomationsResponse, State } from "../types";
import { Factory } from "./factory/Factory";
import type { CrewKey, Item, Scene, Spot, Target } from "./factory/layout";
import { buildScene } from "./factory/scene";

/** where each robot's run history lives (App tabs) */
const HISTORY: Record<CrewKey, string> = { dispatch: "#dispatch", review: "#reviews", diff: "#deploy", release: "#release", cleanup: "#cleanup" };
const SHIP: Spot[] = ["staged", "merged", "deploying"];
const PHONE = "(max-width: 639px)";
const A = "text-primary hover:text-primary-700 hover:underline";

function usePhone() {
  const [m, setM] = useState(() => matchMedia(PHONE).matches);
  useEffect(() => {
    const q = matchMedia(PHONE);
    const on = () => setM(q.matches);
    q.addEventListener("change", on);
    return () => q.removeEventListener("change", on);
  }, []);
  return m;
}

/** ticket key/name, PR, stage, latest review, repo/worktree (what the old 작업실 WorkerInfo showed) */
function TicketInfo({ s, gid, checks }: { s: State; gid: string; checks?: boolean }) {
  const t = s.tickets.find((x) => x.gid === gid);
  if (!t) return null;
  const r = s.runs.filter((x) => x.ticket_gid === gid).at(-1);
  const pr = latestPr(s, gid);
  const review = pr && s.reviews.filter((v) => v.pr_url === pr.url).sort((a, b) => (a.created_at > b.created_at ? 1 : -1)).at(-1);
  return (
    <div className="flex flex-col gap-1.5 border-b border-border py-2.5 last:border-b-0">
      <div className="flex flex-wrap items-center gap-1.5 text-xs font-medium text-text-muted">
        <a href={t.url} target="_blank" rel="noreferrer" className={A}>{t.key}</a>
        {pr && <a href={pr.url} target="_blank" rel="noreferrer" className={A}>PR #{pr.number}</a>}
        <StageBadge stage={t.stage} />
        {r && <span>· {ago(r.created_at)} 시작</span>}
      </div>
      <div className="text-sm font-semibold leading-snug text-text-main">{t.name}</div>
      {r?.ws_comment && <p className={cn("text-xs", t.stage === "blocked" ? "font-semibold text-danger-700" : "text-text-muted")}>{r.ws_comment}</p>}
      {review && <p className="text-xs text-text-muted">리뷰: {review.summary}</p>}
      {checks && pr && <div className="flex flex-wrap items-center gap-1"><CheckBadges checks={pr.checks} /></div>}
      {r && (
        <div className="truncate text-xs text-text-muted" title={r.worktree_path || undefined}>
          <b className="font-semibold text-text-main">{r.repo}</b>/{r.worktree_name}
        </div>
      )}
    </div>
  );
}

function Detail({ s, scene, t }: { s: State; scene: Scene; t: Target }) {
  const list = (gids: string[], checks = false) =>
    gids.length ? gids.map((g) => <TicketInfo key={g} s={s} gid={g} checks={checks} />) : <p className="py-2 text-xs text-text-muted">없음</p>;
  if (t.kind === "tickets") return <><h3 className="text-sm font-bold text-text-main">{t.title}</h3>{list(t.gids)}</>;
  if (t.kind === "gate") {
    const repo = scene.repos.find((r) => r.name === t.repo);
    return (
      <>
        <h3 className="text-sm font-bold text-text-main">{t.repo} · 검수 게이트</h3>
        <p className="text-xs text-text-muted">검사: {repo?.gates.join(", ") || "없음"} · 배지를 누르면 로그가 열려요</p>
        {list(t.gids, true)}
      </>
    );
  }
  if (t.kind === "robot") {
    const c = scene.crew.find((x) => x.key === t.key)!;
    return (
      <>
        <h3 className="text-sm font-bold text-text-main">{c.name} 로봇</h3>
        <p className="text-sm text-text-main">마지막 결과: <b className={cn(c.status === "fail" && "text-danger-700")}>{c.note}</b></p>
        <p className="text-xs text-text-muted">마지막 실행 {c.last ? ago(c.last) : "-"}</p>
        <a href={HISTORY[c.key]} className={cn(A, "text-sm")}>실행 기록 보기</a>
      </>
    );
  }
  const repo = scene.repos.find((r) => r.name === t.repo);
  const load = scene.items.filter((it) => it.repo === t.repo && SHIP.includes(it.at));
  return (
    <>
      <h3 className="text-sm font-bold text-text-main">{t.repo} · {repo?.train ? "트레인 트럭" : "퀵 배송"}</h3>
      <p className="text-xs text-text-muted">
        {repo?.train ? (scene.release.hold ? "트레인 보류 중" : `다음 출발 ${scene.release.next}`) : "트레인 없음: 배포 페이지에서 요청하면 출발해요"}
        {" · "}<a href="#release" className={A}>배포 페이지</a>
      </p>
      {list(load.map((it) => it.gid))}
    </>
  );
}

type Flag = { it: { gid: string; key: string }; why: string };
/** what needs the owner: blocked runs (incl. the rework cap, which asks them via BLOCKED), conflicts, rework at the cap */
function flags(s: State, scene: Scene): Flag[] {
  const comment = (gid: string) => s.runs.filter((r) => r.ticket_gid === gid).at(-1)?.ws_comment ?? "";
  const cap = (it: Item) => scene.repos.find((r) => r.name === it.repo)?.maxRework ?? 0;
  return [
    ...scene.benches.filter((b) => b.stage === "blocked").map((it) => ({ it, why: /고친 뒤에도/.test(comment(it.gid)) ? "재작업 상한" : "막힘" })),
    ...scene.items.filter((it) => it.at === "conflict").map((it) => ({ it, why: "충돌" })),
    ...scene.items.filter((it) => it.at === "rework" && cap(it) && (it.round ?? 0) >= cap(it)).map((it) => ({ it, why: "재작업 상한" })),
  ];
}

/** phone: the scene as lists, in flow order */
function PhoneList({ s, scene }: { s: State; scene: Scene }) {
  const group = (title: string, gids: string[], n: string = String(gids.length)) => (
    <Card key={title} className="p-4">
      <h3 className="text-sm font-bold text-text-main">{title} <span className="font-normal text-text-muted">{n}</span></h3>
      {gids.map((g) => <TicketInfo key={g} s={s} gid={g} />)}
    </Card>
  );
  const ids = (list: { gid: string }[]) => list.map((x) => x.gid);
  const lines = scene.repos.map((r) => [r.name, scene.items.filter((it) => it.repo === r.name && !SHIP.includes(it.at))] as const).filter(([, l]) => l.length);
  return (
    <div className="flex flex-col gap-3">
      {group("입고장", scene.crates.flatMap((c) => c.gids))}
      {group("작업대", ids(scene.benches), `${scene.benches.length}/${scene.maxRuns}`)}
      {lines.map(([name, l]) => group(`라인 · ${name}`, ids(l)))}
      {scene.stray > 0 && group("미등록 레포", scene.strayGids)}
      {group(`출하 · ${scene.release.hold ? "보류 중" : `다음 ${scene.release.next}`}`, ids(scene.items.filter((it) => SHIP.includes(it.at))))}
    </div>
  );
}

/** 작업실 (TP-127): the pipeline as a factory floor, laid out from the settings (benches = max_active_runs, one line
 * per repo with its checks and deploy dock) and filled from the state; robots are the `tp tick` automations.
 * TP-128: click (or tab to) anything for its details, the owner's to-dos collected on top, a list on phones. */
export function Office({ s }: { s: State }) {
  const [snap, setSnap] = useState<AutomationsResponse | null>(null);
  // refetch whenever App's poll refreshes state
  useEffect(() => {
    fetchAutomations().then(setSnap).catch((e) => setSnap({ error: String(e) }));
  }, [s]);
  const scene = useMemo(() => buildScene(s, snap), [s, snap]);
  const phone = usePhone();
  const [pick, setPick] = useState<Target | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const panel = useRef<HTMLDivElement>(null);
  const open = (t: Target) => {
    opener.current = document.activeElement as HTMLElement | null;
    setPick(t);
  };
  const close = () => {
    setPick(null);
    opener.current?.focus();
  };
  useEffect(() => {
    if (!pick) return;
    panel.current?.focus();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && close();
    addEventListener("keydown", esc);
    return () => removeEventListener("keydown", esc);
  }, [pick]);
  const todo = flags(s, scene);
  // desktop: a panel over the scene's corner; phone (the to-do buttons): a sheet over the list
  const sheet = pick && (
    <Card ref={panel} tabIndex={-1} role="dialog" aria-label="상세"
      className={cn("z-20 flex flex-col gap-1 overflow-auto p-4 outline-none",
        phone ? "fixed inset-x-2 bottom-2 max-h-[70dvh]" : "absolute right-2 top-2 max-h-[calc(100%-1rem)] w-[min(24rem,calc(100%-1rem))]")}>
      <Button size="sm" variant="secondary" className="self-end" onClick={close}>닫기 (Esc)</Button>
      <Detail s={s} scene={scene} t={pick} />
    </Card>
  );
  return (
    // App gives this tab exactly the viewport height: the factory takes what the header row leaves
    <div className="flex flex-1 flex-col">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-base font-bold text-text-main sm:text-lg">작업실</h2>
        <p className="text-xs text-text-muted">
          입고장의 대기 티켓이 작업대(에이전트)를 거쳐 레포별 라인의 검수 게이트, 창고, 출하 도크로 가요. 무엇이든 누르면(또는 Tab으로 고르면) 자세히 보여요. 로봇은 자동화예요:{" "}
          {!snap ? "불러오는 중…" : snap.error || (snap.captured_at ? `tp tick 마지막 갱신 ${ago(snap.captured_at)}` : "-")}
        </p>
      </div>
      {todo.length > 0 && (
        <section aria-label="확인 필요" className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-danger-300 bg-danger-50 px-3 py-2">
          <h3 className="text-sm font-bold text-danger-700">확인 필요 {todo.length}</h3>
          {todo.map(({ it, why }) => (
            <Button key={it.gid + why} size="sm" variant="secondary" onClick={() => open({ kind: "tickets", title: `${it.key} · ${why}`, gids: [it.gid] })}>
              {it.key} · {why}
            </Button>
          ))}
        </section>
      )}
      {phone ? (
        <PhoneList s={s} scene={scene} />
      ) : (
        <div className="relative flex min-h-0 flex-1 flex-col">
          <Card className="min-h-0 flex-1 overflow-hidden bg-grey-900 p-0">
            <Factory s={scene} onPick={open} />
          </Card>
          {sheet}
        </div>
      )}
      {phone && sheet}
    </div>
  );
}
