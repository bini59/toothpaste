import { useEffect, useState } from "react";
import { Button, Card } from "../ui";
import { Select } from "../components/Select";
import { TicketCard } from "../components/TicketCard";
import { BOARD, DONE_KEEP_DAYS, byDue, inProject, isOverdue, onBoard, openTickets, staleDone, type Index } from "../lib";
import { projectHref } from "./Projects";
import type { Project, State, Ticket } from "../types";

export interface BoardActions {
  onAssign: (gid: string, repo: string) => void;
  onSkip: (gid: string, skip: boolean) => void;
  onApprove: (gid: string, cancel: boolean, override?: string, overrideReview?: string) => void;
  onOverride: (gid: string, reason: string) => void;
  onReply: (gid: string, text: string) => void;
  onDrop: (gid: string) => void;
}
interface Props extends BoardActions {
  s: State;
  I: Index;
  /** 프로젝트 page: only this project's tickets (inProject), no project filter; cards show their repo */
  project?: Project;
}

// 충돌 대기 / 수정 대기 go first: tp dispatch takes them ahead of new tickets
const first = (t: { stage: string }) => (t.stage === "conflict" ? 2 : t.stage === "rework" ? 1 : 0);
const projOf = (t: Ticket) => t.project_name || "(없음)";

export function Board({ s, I, project, onAssign, onSkip, onApprove, onOverride, onReply, onDrop }: Props) {
  const [proj, setProj] = useState(() => localStorage.getItem("tp.board.proj") || "");
  const splitKey = project ? "tp.repoboard.split" : "tp.board.split"; // the project boards keep #board's toggle untouched
  const [split, setSplit] = useState(() => localStorage.getItem(splitKey) === "1");
  useEffect(() => { if (!project) localStorage.setItem("tp.board.proj", proj); }, [proj, project]);
  useEffect(() => localStorage.setItem(splitKey, split ? "1" : ""), [split, splitKey]);
  const projs = [...new Set([...openTickets(s), ...s.tickets.filter((t) => t.stage !== "open")].map(projOf))].sort();
  const sel = proj && projs.includes(proj) ? proj : ""; // a saved project that no longer has tickets falls back to 전체
  const mine = (t: Ticket) => (project ? inProject(s, I, t, project) : !sel || projOf(t) === sel);
  const tickets = s.tickets.filter((t) => mine(t) && onBoard(t));
  const hidden = s.tickets.filter((t) => mine(t) && !onBoard(t) && t.stage !== "open" && !t.completed).length;
  const tagged = tickets.filter((t) => t.stage !== "open");
  const inc = openTickets(s).filter((t) => mine(t) && onBoard(t));
  const overdue = inc.filter((t) => isOverdue(t.due_on)).length;
  // one bar segment per BOARD column; any stage without a column (today only skipped) counts as 완료 so the bar sums to tagged
  const groups = BOARD.map((col, i) => ({
    ...col,
    n: tagged.filter((t) => col.stages.includes(t.stage) || (i === BOARD.length - 1 && !BOARD.some((b) => b.stages.includes(t.stage)))).length,
  }));
  const blocked = groups.find((g) => g.stages.includes("blocked"))?.n ?? 0;
  const pct = (n: number) => Math.round((n / tagged.length) * 100);
  const columns = (rows: Ticket[]) => (
    <div className="flex items-start gap-3 overflow-x-auto pb-2">
      {BOARD.map((col) => {
        const all = rows.filter((t) => col.stages.includes(t.stage));
        const items = all
          .filter((t) => !staleDone(t))
          .sort((a, b) => first(b) - first(a) || byDue(a, b));
        return (
          <div key={col.title} className="min-w-[260px] max-w-[300px] flex-1 rounded-xl bg-grey-100 p-3">
            <h3 className="mx-1 mb-3 flex items-center justify-between text-sm font-semibold text-text-main">
              <span>{col.title}</span>
              <span className="text-xs text-text-muted">{items.length}</span>
            </h3>
            <div className="flex flex-col gap-2">
              {items.length ? (
                items.map((t) => <TicketCard key={t.gid} t={t} s={s} I={I} repoBadge={!!project} onAssign={onAssign} onSkip={onSkip} onApprove={onApprove} onOverride={onOverride} onReply={onReply} onDrop={onDrop} />)
              ) : (
                <p className="p-5 text-center text-sm text-text-muted">없음</p>
              )}
            </div>
            {all.length > items.length && (
              <p className="mx-1 mt-3 text-xs text-text-muted">
                {DONE_KEEP_DAYS}일 지난 완료 {all.length - items.length}개 숨김
                {project && (
                  <>
                    {" · "}
                    <a href={projectHref(project.id, "tickets")} className="font-medium text-primary hover:text-primary-700 hover:underline">
                      전체 티켓에서 보기 →
                    </a>
                  </>
                )}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
  const lanes = [...new Set(tagged.map(projOf))].sort();
  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {!project && (
          <Select value={sel} onChange={(e) => setProj(e.target.value)} aria-label="프로젝트">
            <option value="">모든 프로젝트</option>
            {projs.map((p) => (
              <option key={p}>{p}</option>
            ))}
          </Select>
        )}
        <Button size="sm" variant="secondary" aria-pressed={split} onClick={() => setSplit(!split)}>
          {split ? "한 보드로 보기" : "프로젝트별로 나눠 보기"}
        </Button>
        {hidden > 0 && (
          <a href="#milestones" className="text-xs font-medium text-primary hover:text-primary-700 hover:underline">
            마일스톤 하위작업 {hidden}개는 마일스톤 화면에서 →
          </a>
        )}
      </div>
      <Card className="mb-6 flex flex-col gap-5 px-5 py-4 md:flex-row md:items-center">
        <dl className="flex shrink-0 flex-wrap gap-x-6 gap-y-3">
          {([
            ["가져온 미완료 티켓", inc.length, ""],
            ["막힘", blocked, "text-danger-700"],
            ["기한 지남", overdue, "text-warning-700"],
          ] as const).map(([label, v, hot]) => (
            <div key={label} className="flex flex-col-reverse">
              <dt className="mt-1.5 whitespace-nowrap text-xs font-medium text-text-muted">{label}</dt>
              <dd className={`text-2xl font-extrabold leading-tight tracking-tight sm:text-3xl ${v > 0 && hot ? hot : "text-text-main"}`}>{v}</dd>
            </div>
          ))}
        </dl>
        <div className="min-w-0 flex-1">
          <div className="flex h-3 overflow-hidden rounded-full bg-grey-100" aria-hidden="true">
            {groups.map((g) => g.n > 0 && <div key={g.title} className={g.color} style={{ width: `${(g.n / tagged.length) * 100}%` }} title={`${g.title} ${g.n}개 (${pct(g.n)}%)`} />)}
          </div>
          <ul className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-muted" aria-label="단계별 추적 티켓">
            <li className="font-semibold text-text-main">추적 중 {tagged.length}</li>
            {groups.map((g) => (
              <li key={g.title} className="flex items-center gap-1.5">
                <span className={`size-2 rounded-full ${g.color}`} aria-hidden="true" />
                {g.title} <span className="font-semibold text-text-main">{g.n}</span>
              </li>
            ))}
          </ul>
        </div>
      </Card>
      {!s.tickets.some((t) => t.stage !== "open") && (
        <p className="p-5 text-center text-sm text-text-muted">
          아직 작업 대기열에 들어온 티켓이 없어요. 프로젝트의 티켓 출처와 작업 태그를 확인하세요.
        </p>
      )}
      {split
        ? lanes.map((p) => (
            <section key={p} className="mb-6">
              <h2 className="mx-1 mb-2 text-sm font-semibold text-text-main">
                {p} <span className="text-xs font-normal text-text-muted">{tagged.filter((t) => projOf(t) === p).length}</span>
              </h2>
              {columns(tagged.filter((t) => projOf(t) === p))}
            </section>
          ))
        : columns(tagged)}
    </>
  );
}
