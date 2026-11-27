import { useEffect, useState } from "react";
import { FolderGit2, LayoutDashboard, List, MessageSquare, Milestone, Plus, Settings as SettingsIcon, Star } from "lucide-react";
import { Badge, Button, Card, cn } from "../ui";
import { SECTIONS, parseProjectSub, projectHref, type ProjectSection } from "../app/navigation";
import { ago, type Index } from "../lib";
import type { Project, State } from "../types";
import { Board, type BoardActions } from "./Board";
import { List as MilestoneList } from "./Milestones";
import { Onboard } from "./Onboard";
import { ProjectChat } from "./ProjectChat";
import { ProjectSettings, RepoSettings, SOURCE_LABEL } from "./Settings";
import { Tickets } from "./Tickets";

export { SECTIONS, parseProjectSub, projectHref, projectOfRepo } from "../app/navigation";

function Meta({ p }: { p: Project }) {
  return (
    <span className="mt-0.5 flex flex-wrap gap-1 text-[11px] font-normal text-text-muted">
      {p.agent}
      {p.conversations > 0 && <> · 대화 {p.conversations}</>}
      {p.running > 0 && <span className="text-primary-700"> · 답 쓰는 중</span>}
      {p.repo && !p.path_ok && <span className="text-danger-700"> · 경로 없음</span>}
    </span>
  );
}

function SourceBadge({ p }: { p: Project }) {
  if (p.unsorted) return <Badge status="warning">미분류</Badge>;
  return p.source && p.source !== "none" ? <Badge status="draft">{SOURCE_LABEL[p.source]}</Badge> : null;
}

function StarButton({ id, label, on, onStar, className }: { id: string; label: string; on: boolean; onStar: (id: string) => void; className?: string }) {
  return (
    <button type="button" aria-pressed={on} aria-label={on ? `${label} 별표 빼기` : `${label} 별표하기`} title={on ? "별표 빼기" : "별표하면 사이드바에 보여요"}
      onClick={() => onStar(id)}
      className={cn("shrink-0 rounded p-1 transition-colors hover:bg-grey-50", on ? "text-warning-500" : "text-text-muted hover:text-text-main", className)}>
      <Star className={cn("h-4 w-4", on && "fill-current")} />
    </button>
  );
}

type Props = {
  s: State; I: Index; sub: string; onChanged: () => void; actions: BoardActions;
  /** /api/projects, loaded by App (the sidebar lists the starred projects too) */
  list: Project[] | null; error: string; onReload: () => Promise<unknown>; starred: string[]; onStar: (id: string) => void;
};

export function Projects({ s, I, sub, onChanged, actions, list, error, onReload: load, starred, onStar }: Props) {
  const { id, section, conv, part } = parseProjectSub(sub);
  const [adding, setAdding] = useState<"" | "project" | "repo">("");
  useEffect(() => setAdding(""), [id, section]); // leaving through a project link closes the forms
  const cur = list?.find((p) => p.id === id);
  if (!list) return <p className="text-sm text-text-muted">{error ? `프로젝트를 불러오지 못했어요: ${error}` : "불러오는 중…"}</p>;

  // 프로젝트 추가 wizard (TP-93): a new project, or only its 레포 step for an existing project / 미분류
  const saved = async (pid: string) => { await load(); setAdding(""); onChanged(); location.hash = projectHref(pid, "settings"); };
  if (adding === "repo" || (!cur && (adding === "project" || !list.length))) {
    return (
      <Onboard projectId={adding === "repo" ? cur?.id ?? "unsorted" : undefined}
        onCancel={list.length ? () => setAdding("") : undefined} onSaved={saved} />
    );
  }
  if (!cur) {
    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h1 className="text-xl font-bold text-text-main sm:text-2xl">프로젝트</h1>
            <p className="mt-1 text-sm text-text-muted">
              {id ? <span className="text-danger-700">설정에 없는 프로젝트예요: {id}. </span> : null}
              프로젝트마다 에이전트와 채팅하고, 티켓을 보고, 설정을 바꿔요. 프로젝트는 레포 여러 개를 묶어요.
            </p>
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="secondary" onClick={() => setAdding("repo")}><Plus className="h-4 w-4" /> 레포 추가</Button>
            <Button size="sm" onClick={() => setAdding("project")}><Plus className="h-4 w-4" /> 새 프로젝트</Button>
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {list.map((p) => (
            // the star is a sibling of the link (a button may not sit inside an <a>)
            <div key={p.id} className="relative">
              <a href={projectHref(p.id)} className="block h-full">
                <Card className="h-full p-4 pr-11 transition-colors hover:border-primary">
                  <div className="flex items-center gap-2">
                    <FolderGit2 className="h-4 w-4 shrink-0 text-text-muted" />
                    <span className="truncate text-sm font-bold text-text-main">{p.name}</span>
                    <SourceBadge p={p} />
                    {p.running > 0 && <Badge status="info">답 쓰는 중</Badge>}
                  </div>
                  <p className="mt-1 line-clamp-2 text-xs text-text-muted">{p.description || p.github || "설명 없음"}</p>
                  <p className="mt-1 truncate font-mono text-[11px] text-text-muted" title={p.repos.map((r) => r.name).join(", ")}>
                    {p.repos.length ? p.repos.map((r) => r.name).join(" · ") : "레포 없음"}
                  </p>
                  <Meta p={p} />
                  {p.last_at && <p className="mt-1 text-[11px] text-text-muted">마지막 대화 {ago(p.last_at)}</p>}
                </Card>
              </a>
              <StarButton id={p.id} label={p.name} on={starred.includes(p.id)} onStar={onStar} className="absolute right-3 top-3" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  const tab = (k: ProjectSection, Icon: typeof MessageSquare) => (
    <a href={projectHref(cur.id, k)} aria-current={section === k ? "page" : undefined}
      className={cn("-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 pb-2 text-sm transition-colors",
        section === k ? "border-primary font-semibold text-primary-700" : "border-transparent text-text-muted hover:text-text-main")}>
      <Icon className="h-4 w-4" /> {SECTIONS[k]}
    </a>
  );
  // 설정: the repo whose settings show below the project's own (#projects/<id>/settings/<repo>), default repo first
  const repo = cur.repos.find((r) => r.name === conv)?.name ?? cur.repo;
  const small = [cur.description, cur.repos.map((r) => r.name).join(", "), cur.path].filter(Boolean);
  return (
    <div className={cn("min-w-0", section === "chat" ? "flex min-h-0 flex-1 flex-col" : "space-y-4")}>
      {/* one line: name + star + tabs; description and repos stay as small print (full text in the tooltip) */}
      <div className={cn("flex flex-wrap items-end gap-x-4 gap-y-1 border-b border-border", section === "chat" && "px-4 pt-4 sm:px-6")}>
        <div className="flex min-w-0 items-center gap-1 pb-2">
          <h1 className="truncate text-lg font-bold text-text-main">{cur.name}</h1>
          <SourceBadge p={cur} />
          <StarButton id={cur.id} label={cur.name} on={starred.includes(cur.id)} onStar={onStar} />
        </div>
        <nav className="flex gap-1">
          {tab("chat", MessageSquare)}
          {tab("board", LayoutDashboard)}
          {tab("milestones", Milestone)}
          {tab("tickets", List)}
          {tab("settings", SettingsIcon)}
        </nav>
        <p title={small.join("\n")} className="min-w-0 flex-1 truncate pb-2 text-xs text-text-muted sm:text-right">
          {!cur.repo ? <span className="text-danger-700">레포가 없어서 채팅할 수 없어요 · </span>
            : !cur.path_ok && <span className="text-danger-700">기본 레포 체크아웃 경로가 없어서 채팅할 수 없어요 · </span>}
          {small.join(" · ")}
        </p>
      </div>
      {section === "settings" ? (
        <div className="space-y-6">
          {cur.unsorted ? (
            <p className="text-sm text-text-muted">어느 프로젝트에도 속하지 않은 레포예요. 새 프로젝트를 만들거나 기존 프로젝트 설정에서 레포를 추가하면 여기서 빠져요.</p>
          ) : (
            <>
              <div className="flex justify-end">
                <Button size="sm" variant="secondary" onClick={() => setAdding("repo")}><Plus className="h-4 w-4" /> 새 레포 등록</Button>
              </div>
              <ProjectSettings id={cur.id} onChanged={() => { load(); onChanged(); }} onDeleted={() => { location.hash = "#projects"; }} />
            </>
          )}
          {repo && (
            <section className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-base font-bold text-text-main">레포별 설정</h2>
                {cur.repos.map((r) => (
                  <a key={r.name} href={projectHref(cur.id, "settings", r.name, part)} aria-current={r.name === repo ? "page" : undefined}
                    className={cn("rounded-lg border px-2.5 py-1 font-mono text-xs transition-colors",
                      r.name === repo ? "border-primary bg-primary-50 text-primary-700" : "border-border text-text-muted hover:text-text-main")}>
                    {r.name}{r.default && " (기본)"}
                  </a>
                ))}
              </div>
              <RepoSettings name={repo} s={s} section={part} href={(k) => projectHref(cur.id, "settings", repo, k)} onChanged={() => { load(); onChanged(); }} />
            </section>
          )}
        </div>
      ) : section === "board" ? (
        <Board s={s} I={I} project={cur} {...actions} />
      ) : section === "milestones" ? (
        <MilestoneList s={s} I={I} project={cur} />
      ) : section === "tickets" ? (
        <Tickets s={s} I={I} project={cur} />
      ) : (
        <ProjectChat key={cur.id} project={cur} conv={conv} onChanged={load} />
      )}
    </div>
  );
}
