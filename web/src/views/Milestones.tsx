import { Button, Card } from "../ui";
import { ConfirmButton } from "../components/ConfirmButton";
import { DevServerPanel } from "../components/DevServer";
import { StageBadge } from "../components/StageBadge";
import { TicketCard } from "../components/TicketCard";
import { MILESTONE_COLS, ago, busySubtasks, byDue, inProject, milestoneRepo, milestones, type Index, type Milestone } from "../lib";
import type { Project, State } from "../types";

interface Props {
  s: State;
  I: Index;
  sub: string;
  onAssign: (gid: string, repo: string) => void;
  onSkip: (gid: string, skip: boolean) => void;
  onApprove: (gid: string, cancel: boolean, override?: string, overrideReview?: string) => void;
  onOverride: (gid: string, reason: string) => void;
  onReply: (gid: string, text: string) => void;
  onDrop: (gid: string) => void;
  onMilestoneDone: (parent: string, cancel: boolean) => void;
  onReload: () => void;
}

const colOf = (stage: string) => MILESTONE_COLS.findIndex((c) => c.stages.includes(stage));
const counts = (m: Milestone) => MILESTONE_COLS.map((_, i) => m.subtasks.filter((t) => colOf(t.stage) === i).length);
const finished = (m: Milestone) => m.ms.length > 0 && m.ms.every((t) => t.stage === "done");

function Progress({ m }: { m: Milestone }) {
  const n = counts(m);
  const total = m.subtasks.length;
  const done = n[MILESTONE_COLS.length - 1];
  return (
    <div>
      <div className="h-2 overflow-hidden rounded-full bg-grey-200">
        <div className="h-full rounded-full bg-success-500" style={{ width: total ? `${(done / total) * 100}%` : "0%" }} />
      </div>
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-text-muted">
        {MILESTONE_COLS.map((c, i) => (
          <span key={c.title} className={i === 2 && n[i] ? "font-semibold text-danger-700" : ""}>
            {c.title} <b className="font-semibold text-text-main">{n[i]}</b>
          </span>
        ))}
      </div>
    </div>
  );
}

function RepoBadges({ m }: { m: Milestone }) {
  if (!m.ms.length) return <span className="text-xs text-text-muted">아직 투입된 하위작업이 없어요</span>;
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs font-medium text-text-muted">
      {m.ms.map((t) => (
        <span key={t.gid} className="flex items-center gap-1">
          {milestoneRepo(t.gid).split("_").at(-1)} <StageBadge stage={t.stage} />
        </span>
      ))}
    </div>
  );
}

/** #milestones: every milestone. With project (프로젝트 마일스톤 탭, TP-88/TP-91): those with a ticket in that project
 * (same test as 전체 티켓) */
export function List({ s, I, project }: { s: State; I?: Index; project?: Project }) {
  const list = milestones(s)
    .filter((m) => !project || !I || [...m.ms, ...m.subtasks].some((t) => inProject(s, I, t, project)))
    .sort((a, b) => Number(finished(a)) - Number(finished(b)) || a.name.localeCompare(b.name));
  if (!list.length)
    return (
      <p className="p-5 text-center text-sm text-text-muted">
        {project ? "이 프로젝트의 마일스톤이 없어요." : "아직 마일스톤이 없어요."}{" "}
        {(!project || project.source === "asana") && <>Asana에서는 부모 태스크에 <b>{String(s.meta.settings.milestone_tag || "tp-milestone")}</b> 태그를 붙이세요. </>}
        {(!project || project.source === "github") && <>GitHub에서는 <b>{String(s.meta.settings.milestone_tag || "tp-milestone")}</b> 라벨이 있는 부모 이슈에 하위 이슈를 연결하세요. </>}
        {(!project || project.source === "notion") && "Notion에서는 프로젝트 설정에서 마일스톤 DB를 고르고 작업에 마일스톤 행을 연결하세요."}
      </p>
    );
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {list.map((m) => (
        <a key={m.gid} href={`#milestones/${encodeURIComponent(m.gid)}`} className="block rounded-xl focus:outline-none focus-visible:ring-2 focus-visible:ring-primary">
          <Card className="h-full p-4 transition-colors hover:border-primary">
            <div className="text-xs font-medium text-text-muted">{m.project || "-"}</div>
            <div className="my-1.5 text-base font-bold leading-snug text-text-main">{m.name}</div>
            <div className="mb-3">
              <RepoBadges m={m} />
            </div>
            <Progress m={m} />
          </Card>
        </a>
      ))}
    </div>
  );
}

/** 마일스톤 작업 완료: until the owner presses it the milestone waits in 대기 (no PR, review or staging approval) */
function DoneButton({ m, I, onMilestoneDone }: { m: Milestone; I: Index; onMilestoneDone: Props["onMilestoneDone"] }) {
  const open = m.ms.filter((t) => !t.completed);
  if (!open.length) return null;
  const doneAt = open.find((t) => t.ms_done_at)?.ms_done_at;
  const busy = busySubtasks(m, I);
  if (doneAt)
    return (
      <div className="flex flex-wrap items-center gap-2 text-xs text-text-muted">
        <span>
          작업 완료 {ago(doneAt)} · staging으로 가는 마일스톤 PR이 열리고 리뷰를 받아요. 하위작업이 다시 진행되면 자동으로 취소돼요.
        </span>
        <Button size="sm" variant="secondary" onClick={() => onMilestoneDone(m.gid, true)}>
          취소
        </Button>
      </div>
    );
  if (busy.length)
    return (
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled>
          마일스톤 작업 완료
        </Button>
        <span className="text-xs text-text-muted">하위작업 {busy.length}개가 아직 진행 중이에요</span>
      </div>
    );
  return (
    <ConfirmButton
      size="sm"
      message={
        <>
          하위작업을 다 끝냈나요? 누르면 레포마다 staging으로 가는 마일스톤 PR이 열리고 AI 리뷰를 받아요. staging 머지는 그
          다음에 따로 승인해요. 나중에 하위작업이 다시 진행되면 이 표시는 자동으로 취소되고 대기로 돌아가요.
        </>
      }
      confirmLabel="작업 완료"
      onConfirm={() => onMilestoneDone(m.gid, false)}
    >
      마일스톤 작업 완료
    </ConfirmButton>
  );
}

function Detail({ m, ...p }: Props & { m: Milestone }) {
  const { s, I } = p;
  const branch = "milestone/" + (m.gid.startsWith("gh:") ? m.gid.replace(/[:/#]/g, "-").replaceAll("..", "+") : m.gid.replace(/^notion:/, ""));
  const card = (t: Milestone["subtasks"][number]) => (
    <TicketCard key={t.gid} t={t} s={s} I={I} onAssign={p.onAssign} onSkip={p.onSkip} onApprove={p.onApprove}
      onOverride={p.onOverride} onReply={p.onReply} onDrop={p.onDrop} inMilestoneView />
  );
  return (
    <>
      <a href="#milestones" className="text-sm font-medium text-primary hover:text-primary-700 hover:underline">← 마일스톤 목록</a>
      <Card className="my-4 p-5">
        <div className="text-xs font-medium text-text-muted">
          {m.project || "-"} · <code>{branch}</code>
        </div>
        <h2 className="my-1.5 text-lg font-bold text-text-main sm:text-xl">
          {m.url ? (
            <a href={m.url} target="_blank" rel="noreferrer" className="hover:text-primary hover:underline">{m.name}</a>
          ) : (
            m.name
          )}
        </h2>
        <div className="mb-4">
          <RepoBadges m={m} />
        </div>
        <Progress m={m} />
        <div className="mt-4">
          <DoneButton m={m} I={I} onMilestoneDone={p.onMilestoneDone} />
        </div>
      </Card>
      {m.ms.length > 0 && (
        <section className="mb-6">
          <h3 className="mx-1 mb-2 text-sm font-semibold text-text-main">staging으로 가는 마일스톤 PR</h3>
          <p className="mx-1 mb-3 text-xs text-text-muted">
            '마일스톤 작업 완료'를 누르면 레포마다 PR이 열려요. 리뷰가 끝나면 여기서 staging 머지를 승인해요.
          </p>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{m.ms.map(card)}</div>
        </section>
      )}
      {m.ms.some((t) => !t.completed) && (
        <section className="mb-6">
          <h3 className="mx-1 mb-2 text-sm font-semibold text-text-main">dev 서버</h3>
          <p className="mx-1 mb-3 text-xs text-text-muted">
            레포마다 <code>{branch}</code> 브랜치로 dev 서버를 띄워 하위작업을 합쳐서 확인해요. 띄우기 전에 .env와 포트를 점검해요.
          </p>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {m.ms.filter((t) => !t.completed).map((t) => <DevServerPanel key={t.gid} t={t} s={s} onReload={p.onReload} />)}
          </div>
        </section>
      )}
      <div className="flex items-start gap-3 overflow-x-auto pb-2">
        {MILESTONE_COLS.map((col) => {
          const items = m.subtasks.filter((t) => col.stages.includes(t.stage)).sort((a, b) => a.name.localeCompare(b.name) || byDue(a, b));
          return (
            <div key={col.title} className="min-w-[260px] max-w-[320px] flex-1 rounded-xl bg-grey-100 p-3">
              <h3 className="mx-1 mb-3 flex items-center justify-between text-sm font-semibold text-text-main">
                <span>{col.title}</span>
                <span className="text-xs text-text-muted">{items.length}</span>
              </h3>
              <div className="flex flex-col gap-2">
                {items.length ? items.map(card) : <p className="p-5 text-center text-sm text-text-muted">없음</p>}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

export function Milestones(p: Props) {
  if (!p.sub) return <List s={p.s} />;
  const m = milestones(p.s).find((x) => x.gid === p.sub);
  if (!m)
    return (
      <p className="p-5 text-center text-sm text-text-muted">
        이 마일스톤을 찾지 못했어요. <a href="#milestones" className="text-primary hover:underline">목록으로</a>
      </p>
    );
  return <Detail m={m} {...p} />;
}
